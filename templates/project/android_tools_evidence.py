#!/usr/bin/env python3
"""Private local Android-tool run records with explicit project-memory export."""
from __future__ import annotations

BUNDLE_PROTOCOL = 1

from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid
import zlib
from typing import Any, Iterable, Mapping

MAX_ARTIFACTS = 16
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024
MAX_EXPORT_LOG_BYTES = 128 * 1024
MAX_HELPER_OUTPUT_BYTES = 128 * 1024
DEFAULT_MAX_BYTES = 500_000_000
DEFAULT_MAX_AGE_SECONDS = 604_800
MIN_MAX_BYTES = 1_048_576
MAX_MAX_BYTES = 10_000_000_000
MIN_MAX_AGE_SECONDS = 60
MAX_MAX_AGE_SECONDS = 31_536_000
MAX_RUN_STORE_ENTRIES = 10_000
MAX_RUN_TREE_ENTRIES = 100_000
MAX_EVIDENCE_METADATA_BYTES = 64 * 1024
RESERVATION_METADATA_BYTES = MAX_EVIDENCE_METADATA_BYTES + MAX_EXPORT_LOG_BYTES
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\Z")
_MEDIA = re.compile(r"[a-z0-9.+-]+/[a-z0-9.+-]+\Z")
_TARGET = re.compile(r"[A-Za-z0-9_.:-]+@[A-Za-z0-9_.-]+\Z")
_SERIAL = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_CONTEXT_FIELDS = {
    "source_revision", "source_fingerprint", "target", "device_serial",
    "lease_id", "generation", "package_name", "build_variant",
}
_TRANSPORTS = {"not_started", "completed", "failed", "timed_out", "unknown"}
_EFFECTS = {"not_dispatched", "not_applicable", "observed", "unknown"}
_ERRORS = {
    "unsupported_provider", "unsupported_operation", "invalid_arguments",
    "unsupported_build_launcher", "provider_version_mismatch", "provider_failed",
    "provider_unavailable", "lease_lost",
    "query_timeout", "interrupted", "observation_limit", "drain_unknown",
    "secret_transport_unsupported", "device_serial_required", "source_root_required",
    "staging_directory_required", "invalid_output_path", "output_already_exists",
    "unsupported_target", "artifact_missing", "artifact_limit", "invalid_artifact",
    "invalid_artifact_path", "invalid_component_tree", "preview_failed",
    "preview_result_missing", "invalid_png", "missing_output",
    "invalid_provider_output", "assertion_failed",
}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_DATA_BOOL = {"captured", "completed", "visible"}
_DATA_INT = {"match_count", "duration_ms", "exit_code", "api_level", "http_status"}
_TIME_LOCK = threading.Lock()
_LAST_TIME_NS = 0
_LOCK_REGISTRY_GUARD = threading.Lock()


@dataclass
class _ThreadLockSlot:
    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0


_LOCK_REGISTRY: dict[tuple[int, int], _ThreadLockSlot] = {}
_FD_LOCKS: dict[int, tuple[tuple[int, int], _ThreadLockSlot]] = {}


class EvidenceError(ValueError):
    """Unsafe, malformed, or conflicting evidence."""


class BindingError(EvidenceError):
    """Missing or mismatched project-memory binding."""


class RetentionLimitError(EvidenceError):
    """Configured retention capacity is unavailable without removing protected runs."""


@dataclass
class _RunLease:
    fd: int | None
    guard: threading.RLock = field(default_factory=threading.RLock)


@dataclass(frozen=True)
class RunHandle:
    root: Path
    invocation_id: str
    run_directory: Path
    staging_directory: Path
    manifest_path: Path
    _lease: _RunLease | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class RunOutcome:
    transport: str
    effect: str
    error: str | None = None
    data: Any = None
    artifacts: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "transport": self.transport, "effect": self.effect, "error": self.error,
            "data": self.data, "artifacts": list(self.artifacts),
        }


@dataclass(frozen=True)
class StagedArtifact:
    name: str
    media_type: str


@dataclass(frozen=True)
class ExportResult:
    status: str
    manifest_sha256: str
    attachment_sha256s: tuple[str, ...]
    attempt: int
    reason_code: str = ""


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _time() -> str:
    global _LAST_TIME_NS
    with _TIME_LOCK:
        timestamp_ns = max(time.time_ns(), _LAST_TIME_NS + 1)
        _LAST_TIME_NS = timestamp_ns
    seconds, nanoseconds = divmod(timestamp_ns, 1_000_000_000)
    prefix = datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{prefix}.{nanoseconds:09d}Z"


def _token(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value) or value in {".", ".."}:
        raise EvidenceError(f"invalid {label}")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise EvidenceError(f"invalid {label}")
    return value


def _path(value: str | os.PathLike[str], label: str) -> Path:
    text = os.fspath(value)
    if not isinstance(text, str) or not text or not os.path.isabs(text):
        raise EvidenceError(f"{label} must be absolute")
    result = Path(text)
    if ".." in result.parts or str(result) != text:
        raise EvidenceError(f"invalid {label}")
    try:
        if result.resolve(strict=False) != result:
            raise EvidenceError(f"{label} contains a symlink")
    except OSError as error:
        raise EvidenceError(f"invalid {label}") from error
    current = Path(result.anchor)
    for part in result.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise EvidenceError(f"{label} contains a symlink")
        if current != result and not stat.S_ISDIR(info.st_mode):
            raise EvidenceError(f"invalid {label} parent")
    return result


def _directory(path: Path, *, create: bool = False, private: bool = True) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        if not create:
            raise EvidenceError("evidence directory missing")
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise EvidenceError("cannot create evidence directory") from error
    except OSError as error:
        raise EvidenceError("cannot inspect evidence directory") from error
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o7000
            or (private and info.st_mode & 0o077)):
        raise EvidenceError("unsafe evidence directory")


def _dirfd(path: Path) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise EvidenceError("unsafe evidence directory") from error
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o7000 or info.st_mode & 0o077):
        os.close(fd)
        raise EvidenceError("unsafe evidence directory")
    return fd


def _write_file(directory: Path, name: str, raw: bytes, *,
                limit: int = MAX_RECORD_BYTES) -> None:
    if "/" in name or name in {"", ".", ".."} or len(raw) > limit:
        raise EvidenceError("invalid evidence write")
    parent = _dirfd(directory)
    temp = f".tmp-{uuid.uuid4().hex}"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                     dir_fd=parent)
        try:
            view = memoryview(raw)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise EvidenceError("short evidence write")
                view = view[count:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise EvidenceError("evidence record already exists")
        os.rename(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    except OSError as error:
        raise EvidenceError("cannot write evidence") from error
    finally:
        try:
            os.unlink(temp, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def _read_file(directory: Path, name: str, limit: int) -> bytes:
    if "/" in name or name in {"", ".", ".."}:
        raise EvidenceError("invalid evidence file name")
    parent = _dirfd(directory)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     dir_fd=parent)
    except FileNotFoundError:
        os.close(parent)
        raise
    except OSError as error:
        os.close(parent)
        raise EvidenceError("unsafe or missing evidence file") from error
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.getuid() or before.st_mode & 0o077
                or before.st_mode & 0o7000 or before.st_size > limit):
            raise EvidenceError("unsafe or oversized evidence file")
        chunks, size = [], 0
        while True:
            block = os.read(fd, min(65536, limit + 1 - size))
            if not block:
                break
            size += len(block)
            if size > limit:
                raise EvidenceError("evidence file exceeds size limit")
            chunks.append(block)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise EvidenceError("evidence file changed during read")
        return b"".join(chunks)
    finally:
        os.close(fd)
        os.close(parent)


def _read_json(directory: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(_read_file(directory, name, MAX_RECORD_BYTES))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceError("invalid evidence record") from error
    if not isinstance(value, dict):
        raise EvidenceError("invalid evidence record")
    return value


def _thread_lock(fd: int, *, blocking: bool) -> bool:
    info = os.fstat(fd)
    key = (info.st_dev, info.st_ino)
    with _LOCK_REGISTRY_GUARD:
        slot = _LOCK_REGISTRY.setdefault(key, _ThreadLockSlot())
        slot.users += 1
    if not slot.lock.acquire(blocking=blocking):
        with _LOCK_REGISTRY_GUARD:
            slot.users -= 1
            if slot.users == 0 and _LOCK_REGISTRY.get(key) is slot:
                del _LOCK_REGISTRY[key]
        return False
    with _LOCK_REGISTRY_GUARD:
        _FD_LOCKS[fd] = (key, slot)
    return True


def _release_thread_lock(fd: int) -> None:
    with _LOCK_REGISTRY_GUARD:
        acquired = _FD_LOCKS.pop(fd, None)
        if acquired is None:
            return
        key, slot = acquired
        slot.lock.release()
        slot.users -= 1
        if slot.users == 0 and _LOCK_REGISTRY.get(key) is slot:
            del _LOCK_REGISTRY[key]


def _open_lock_file(parent: int, name: str) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        return os.open(
            name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent,
        )
    except FileExistsError:
        return os.open(name, flags, dir_fd=parent)


def _lock(directory: Path, name: str) -> int:
    parent = _dirfd(directory)
    try:
        fd = _open_lock_file(parent, name)
    finally:
        os.close(parent)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_mode & 0o7000):
        os.close(fd)
        raise EvidenceError("unsafe evidence lock")
    _thread_lock(fd, blocking=True)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as error:
        _release_thread_lock(fd)
        os.close(fd)
        raise EvidenceError("cannot acquire evidence lock") from error
    return fd


def _try_lock(directory: Path, name: str) -> int | None:
    parent = _dirfd(directory)
    try:
        fd = _open_lock_file(parent, name)
    finally:
        os.close(parent)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_mode & 0o7000):
        os.close(fd)
        raise EvidenceError("unsafe evidence lock")
    if not _thread_lock(fd, blocking=False):
        os.close(fd)
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError as error:
        _release_thread_lock(fd)
        os.close(fd)
        if error.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
            return None
        raise EvidenceError("cannot acquire evidence lock") from error


def _unlock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        _release_thread_lock(fd)
        os.close(fd)


def _retention_limits(max_bytes: int, max_age_seconds: int) -> tuple[int, int]:
    if (type(max_bytes) is not int or not MIN_MAX_BYTES <= max_bytes <= MAX_MAX_BYTES
            or type(max_age_seconds) is not int
            or not MIN_MAX_AGE_SECONDS <= max_age_seconds <= MAX_MAX_AGE_SECONDS):
        raise EvidenceError("invalid retention limits")
    return max_bytes, max_age_seconds


def reservation_for_operation(operation: str, output_limit_bytes: int) -> int:
    """Return a conservative logical-byte reservation before provider dispatch.

    Provider staging is written directly to disk, so the output limit validated
    after the provider exits cannot reduce the preview/capture staging reserve.
    """
    operation = _token(operation, "operation")
    if type(output_limit_bytes) is not int or not 4096 <= output_limit_bytes <= MAX_ARTIFACT_BYTES:
        raise EvidenceError("invalid output limit")
    if operation == "preview.render":
        # 16 preview rows, each with an image and component tree, plus final objects.
        return 16 * 2 * MAX_ARTIFACT_BYTES + MAX_TOTAL_ARTIFACT_BYTES + RESERVATION_METADATA_BYTES
    if operation == "device.capture":
        return 2 * MAX_ARTIFACT_BYTES + RESERVATION_METADATA_BYTES
    if operation not in {
        "docs.search", "docs.fetch", "preview.list", "device.tree",
        "device.assert-visible", "device.assert-gone", "device.launch",
        "device.tap", "device.key", "device.swipe",
    }:
        raise EvidenceError("unsupported operation for retention reservation")
    return RESERVATION_METADATA_BYTES


def _timestamp_ns(value: Any) -> int:
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z", value):
        raise EvidenceError("invalid run timestamp")
    seconds_text, _, fraction = value[:-1].partition(".")
    try:
        seconds = int(datetime.strptime(seconds_text, "%Y-%m-%dT%H:%M:%S")
                      .replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, OverflowError) as error:
        raise EvidenceError("invalid run timestamp") from error
    return seconds * 1_000_000_000 + int(fraction.ljust(9, "0") or "0")


def _read_started(run: Path, invocation_id: str) -> tuple[dict[str, Any], int]:
    record = _read_json(run, "started.json")
    required = {
        "schema", "invocation_id", "started_at", "operation", "provider",
        "context", "state", "outcome",
    }
    if (set(record) != required or record.get("schema") != "android-tools-run-start-v1"
            or record.get("invocation_id") != invocation_id
            or record.get("state") != "started"
            or not isinstance(record.get("operation"), str)
            or not isinstance(record.get("provider"), str)
            or not isinstance(record.get("context"), dict)
            or record.get("outcome") != {
                "transport": "unknown", "effect": "unknown", "error": None, "data": {},
            }):
        raise EvidenceError("invalid run start record")
    _token(record["operation"], "operation")
    _token(record["provider"], "provider")
    _context(record["context"])
    return record, _timestamp_ns(record["started_at"])


def _run_manifest(run: Path, started: Mapping[str, Any], invocation_id: str) -> dict[str, Any] | None:
    try:
        manifest = _read_json(run, "manifest.json")
    except FileNotFoundError:
        return None
    required = {
        "schema", "invocation_id", "state", "started_at", "finished_at",
        "operation", "provider", "context", "outcome", "artifacts",
    }
    if (set(manifest) != required or manifest.get("schema") != "android-tools-run-v1"
            or manifest.get("state") != "finished"
            or manifest.get("invocation_id") != invocation_id
            or manifest.get("started_at") != started["started_at"]
            or manifest.get("operation") != started["operation"]
            or manifest.get("provider") != started["provider"]
            or manifest.get("context") != started["context"]
            or not isinstance(manifest.get("outcome"), dict)
            or not isinstance(manifest.get("artifacts"), list)
            or len(manifest["artifacts"]) > MAX_ARTIFACTS):
        raise EvidenceError("invalid run manifest")
    _timestamp_ns(manifest.get("finished_at"))
    outcome = manifest["outcome"]
    if (set(outcome) != {"transport", "effect", "error", "data"}
            or outcome.get("transport") not in _TRANSPORTS
            or outcome.get("effect") not in _EFFECTS):
        raise EvidenceError("invalid run outcome")
    for artifact in manifest["artifacts"]:
        if not isinstance(artifact, dict):
            raise EvidenceError("invalid artifact metadata")
        digest = _digest(artifact.get("sha256"), "artifact digest")
        if artifact.get("local_locator") != f"artifacts/{digest}":
            raise EvidenceError("invalid artifact locator")
    return manifest


def _read_reservation(run: Path, invocation_id: str) -> int | None:
    try:
        record = _read_json(run, "reservation.json")
    except FileNotFoundError:
        return None
    if (set(record) != {"schema", "invocation_id", "reservation_bytes"}
            or record.get("schema") != "android-tools-run-reservation-v1"
            or record.get("invocation_id") != invocation_id
            or type(record.get("reservation_bytes")) is not int
            or not RESERVATION_METADATA_BYTES <= record["reservation_bytes"] <= MAX_MAX_BYTES):
        raise EvidenceError("invalid run reservation")
    return record["reservation_bytes"]


def _tree_size(run: Path) -> tuple[int, int]:
    size, entries = 0, 0
    pending = [run]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as listing:
                for entry in listing:
                    entries += 1
                    if entries > MAX_RUN_TREE_ENTRIES:
                        raise EvidenceError("run exceeds retention metadata-entry limit")
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError as error:
                        raise EvidenceError("cannot inspect run entry") from error
                    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                        if (info.st_uid != os.getuid() or info.st_mode & 0o077
                                or info.st_mode & 0o7000):
                            raise EvidenceError("unsafe run directory")
                        pending.append(Path(entry.path))
                    elif (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                          and info.st_uid == os.getuid() and not info.st_mode & 0o077
                          and not info.st_mode & 0o7000):
                        size += info.st_size
                    else:
                        raise EvidenceError("unsafe run entry")
        except OSError as error:
            raise EvidenceError("cannot scan run directory") from error
    return size, entries


def _validate_run_root_entries(run: Path) -> None:
    allowed = {
        ".active.lock", ".record.lock", ".export.lock", "started.json",
        "reservation.json", "manifest.json", "export.jsonl", "staging", "artifacts",
    }
    try:
        with os.scandir(run) as listing:
            for entry in listing:
                if entry.name not in allowed and not re.fullmatch(
                        r"\.tmp-[0-9a-f]{32}", entry.name):
                    raise EvidenceError("unrecognized run-store entry")
                info = entry.stat(follow_symlinks=False)
                if entry.name in {"staging", "artifacts"}:
                    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                        raise EvidenceError("invalid run-store directory")
                elif (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                      or info.st_nlink != 1):
                    raise EvidenceError("invalid run-store file")
    except OSError as error:
        raise EvidenceError("cannot inspect run-store entries") from error
    artifacts = run / "artifacts"
    try:
        with os.scandir(artifacts) as listing:
            for entry in listing:
                if not (_DIGEST.fullmatch(entry.name)
                        or re.fullmatch(r"\.tmp-[0-9a-f]{32}", entry.name)):
                    raise EvidenceError("unrecognized retained artifact")
                info = entry.stat(follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                        or info.st_nlink != 1):
                    raise EvidenceError("invalid retained artifact")
    except FileNotFoundError:
        pass
    except OSError as error:
        raise EvidenceError("cannot inspect retained artifacts") from error


def _run_info(run: Path, invocation_id: str) -> dict[str, Any]:
    started, started_ns = _read_started(run, invocation_id)
    manifest = _run_manifest(run, started, invocation_id)
    reservation = _read_reservation(run, invocation_id)
    _validate_run_root_entries(run)
    size, entries = _tree_size(run)
    export_size = 0
    try:
        info = (run / "export.jsonl").lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise EvidenceError("cannot inspect export history") from error
    else:
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_nlink != 1 or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_mode & 0o7000
                or info.st_size > MAX_EXPORT_LOG_BYTES):
            raise EvidenceError("unsafe or oversized export history")
        export_size = info.st_size
    state = "finished" if manifest is not None else "started"
    extra = MAX_EXPORT_LOG_BYTES - export_size if manifest is not None else 0
    return {
        "path": run, "started_ns": started_ns, "state": state, "size": size,
        "extra": extra, "reservation": reservation, "entries": entries,
        "contribution": size + extra,
    }


def _remove_run(run: Path) -> None:
    _tree_size(run)
    try:
        shutil.rmtree(run)
    except OSError as error:
        raise EvidenceError("cannot completely remove retained run") from error
    if run.exists() or run.is_symlink():
        raise EvidenceError("run removal did not complete")


def _store_inventory(runs: Path) -> list[dict[str, Any]]:
    result, entries = [], []
    try:
        with os.scandir(runs) as listing:
            for entry in listing:
                entries.append(entry)
                if len(entries) > MAX_RUN_STORE_ENTRIES + 1:
                    raise EvidenceError("run store exceeds retention entry limit")
    except OSError as error:
        raise EvidenceError("cannot scan run store") from error
    for entry in entries:
        if entry.name == ".retention.lock":
            continue
        try:
            parsed = uuid.UUID(entry.name)
        except ValueError as error:
            raise EvidenceError("unexpected run-store entry") from error
        if str(parsed) != entry.name or parsed.version != 4 or entry.is_symlink():
            raise EvidenceError("unsafe run-store entry")
        path = runs / entry.name
        _directory(path)
        _, started_ns = _read_started(path, entry.name)
        activity = _try_lock(path, ".active.lock")
        if activity is None:
            reservation = _read_reservation(path, entry.name)
            if reservation is None:
                raise EvidenceError("active invocation has no reservation")
            result.append({
                "path": path, "started_ns": started_ns, "state": "active",
                "size": reservation, "extra": 0, "reservation": reservation,
                "entries": 1, "contribution": reservation, "active": True,
            })
            continue
        try:
            record_lock = _try_lock(path, ".record.lock")
            if record_lock is None:
                reservation = _read_reservation(path, entry.name)
                if reservation is None:
                    raise EvidenceError("busy invocation has no reservation")
                result.append({
                    "path": path, "started_ns": started_ns, "state": "busy",
                    "size": reservation, "extra": 0, "reservation": reservation,
                    "entries": 1, "contribution": reservation, "active": True,
                })
                continue
            try:
                export_lock = _try_lock(path, ".export.lock")
                if export_lock is None:
                    reservation = _read_reservation(path, entry.name)
                    if reservation is None:
                        raise EvidenceError("busy invocation has no reservation")
                    result.append({
                        "path": path, "started_ns": started_ns, "state": "busy",
                        "size": reservation, "extra": 0, "reservation": reservation,
                        "entries": 1, "contribution": reservation, "active": True,
                    })
                    continue
                try:
                    info = _run_info(path, entry.name)
                    info["active"] = False
                    result.append(info)
                finally:
                    _unlock(export_lock)
            finally:
                _unlock(record_lock)
        finally:
            _unlock(activity)
    if sum(item["entries"] for item in result) > MAX_RUN_TREE_ENTRIES:
        raise EvidenceError("run store exceeds retention metadata-entry limit")
    return result


def _remove_candidate(path: Path, invocation_id: str) -> dict[str, Any] | None:
    _read_started(path, invocation_id)
    activity = _try_lock(path, ".active.lock")
    if activity is None:
        return None
    record_lock = export_lock = None
    try:
        record_lock = _try_lock(path, ".record.lock")
        if record_lock is None:
            return None
        export_lock = _try_lock(path, ".export.lock")
        if export_lock is None:
            return None
        info = _run_info(path, invocation_id)
        _remove_run(path)
        return info
    finally:
        if export_lock is not None:
            _unlock(export_lock)
        if record_lock is not None:
            _unlock(record_lock)
        _unlock(activity)


def _cleanup_locked(runs: Path, *, max_bytes: int, max_age_seconds: int,
                    additional_bytes: int = 0) -> None:
    now_ns = time.time_ns()
    maximum_age_ns = max_age_seconds * 1_000_000_000
    entries = _store_inventory(runs)
    for entry in sorted(entries, key=lambda item: (item["started_ns"], item["path"].name)):
        if entry["started_ns"] <= now_ns - maximum_age_ns and not entry["active"]:
            _remove_candidate(entry["path"], entry["path"].name)

    entries = _store_inventory(runs)
    total = sum(entry["contribution"] for entry in entries)
    for entry in sorted(entries, key=lambda item: (item["started_ns"], item["path"].name)):
        if total + additional_bytes <= max_bytes:
            break
        if entry["active"]:
            continue
        removed = _remove_candidate(entry["path"], entry["path"].name)
        if removed is not None:
            total -= removed["contribution"]
    if total + additional_bytes > max_bytes:
        raise RetentionLimitError("retention capacity is reserved by active or protected runs")


def _export_identity(value: Any) -> dict[str, str] | None:
    fields = {"project_uid", "task_key", "task_uid", "task_binding_sha256"}
    if not isinstance(value, Mapping) or set(value) != fields:
        return None
    try:
        return {
            "project_uid": _token(value["project_uid"], "project uid"),
            "task_key": _token(value["task_key"], "task key"),
            "task_uid": _token(value["task_uid"], "task uid"),
            "task_binding_sha256": _digest(
                value["task_binding_sha256"], "task binding hash",
            ),
        }
    except EvidenceError:
        return None


def _attachment_rows_match(
    recorded: Any,
    objects: Iterable[tuple[str, int]],
    task_key: str,
) -> bool:
    expected = [
        {
            "sha256": digest,
            "size_bytes": size,
            "locator": f"tasks/{task_key}/artifacts/attachments/objects/{digest}",
        }
        for digest, size in objects
    ]
    if not isinstance(recorded, list) or len(recorded) != len(expected):
        return False
    for actual, required in zip(recorded, expected):
        if (not isinstance(actual, Mapping) or set(actual) != set(required)
                or type(actual.get("size_bytes")) is not int
                or actual.get("sha256") != required["sha256"]
                or actual.get("size_bytes") != required["size_bytes"]
                or actual.get("locator") != required["locator"]):
            return False
    return True


def _export_record_header_matches(
    entry: Mapping[str, Any],
    *,
    interface: str,
    identity: Mapping[str, str],
    binding_sha: str,
    manifest_sha: str,
    hashes: tuple[str, ...],
) -> bool:
    required = {
        "schema", "attempt", "at", "status", "reason_code", "interface",
        "binding_sha256", "binding", "manifest_sha256", "attachment_sha256s",
        "exported_attachments",
    }
    try:
        saved_binding_sha = _digest(entry.get("binding_sha256"), "binding hash")
        saved_manifest_sha = _digest(entry.get("manifest_sha256"), "manifest hash")
        saved_hashes = entry.get("attachment_sha256s")
        if (not isinstance(saved_hashes, list)
                or tuple(_digest(item, "attachment hash") for item in saved_hashes) != hashes):
            return False
    except (EvidenceError, TypeError):
        return False
    saved_identity = _export_identity(entry.get("binding"))
    return (
        set(entry) == required
        and entry.get("schema") == "android-tools-export-v1"
        and type(entry.get("attempt")) is int
        and entry["attempt"] >= 1
        and isinstance(entry.get("at"), str)
        and entry.get("status") == "exported"
        and entry.get("reason_code") == ""
        and entry.get("interface") == interface
        and saved_binding_sha == binding_sha
        and saved_manifest_sha == manifest_sha
        and saved_identity == dict(identity)
    )


def _export_record_matches(
    entry: Mapping[str, Any],
    *,
    interface: str,
    identity: Mapping[str, str],
    binding_sha: str,
    manifest_sha: str,
    hashes: tuple[str, ...],
    objects: Iterable[tuple[bytes, str]],
) -> bool:
    object_rows = list(objects)
    if (not _export_record_header_matches(
            entry, interface=interface, identity=identity, binding_sha=binding_sha,
            manifest_sha=manifest_sha, hashes=hashes)
            or hashes != tuple(digest for _, digest in object_rows)):
        return False
    object_sizes = ((digest, len(content)) for content, digest in object_rows)
    return _attachment_rows_match(
        entry.get("exported_attachments"), object_sizes, identity["task_key"],
    )


def _context(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - _CONTEXT_FIELDS:
        raise EvidenceError("unsupported safe-context field")
    result = {}
    for key, item in value.items():
        if key == "source_fingerprint":
            result[key] = _digest(item, key)
        elif key == "source_revision":
            if not isinstance(item, str) or not re.fullmatch(
                    r"(?:[0-9a-f]{7,64}|[A-Za-z0-9][A-Za-z0-9._-]{0,63})", item):
                raise EvidenceError("invalid source_revision")
            result[key] = item
        elif key == "generation":
            if type(item) is not int or item < 0:
                raise EvidenceError("invalid generation")
            result[key] = item
        elif key == "package_name":
            if not isinstance(item, str) or not _PACKAGE.fullmatch(item):
                raise EvidenceError("invalid package_name")
            result[key] = item
        elif key == "target":
            if not isinstance(item, str) or not _TARGET.fullmatch(item):
                raise EvidenceError("invalid target")
            result[key] = item
        elif key == "device_serial":
            if not isinstance(item, str) or not _SERIAL.fullmatch(item):
                raise EvidenceError("invalid device_serial")
            result[key] = item
        else:
            result[key] = _token(item, key)
    return result


def begin_run(root: str | os.PathLike[str], operation: str, provider: str,
              safe_context: Mapping[str, Any], *,
              max_bytes: int = DEFAULT_MAX_BYTES,
              max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
              reservation_bytes: int | None = None) -> RunHandle:
    """Atomically reserve capacity and start a pinned local evidence record."""
    max_bytes, max_age_seconds = _retention_limits(max_bytes, max_age_seconds)
    evidence_root = _path(root, "evidence root")
    operation, provider = _token(operation, "operation"), _token(provider, "provider")
    context = _context(safe_context)
    minimum_reservation = reservation_for_operation(operation, MAX_ARTIFACT_BYTES)
    if reservation_bytes is None:
        reservation_bytes = minimum_reservation
    if (type(reservation_bytes) is not int
            or not minimum_reservation <= reservation_bytes <= MAX_MAX_BYTES):
        raise EvidenceError("invalid operation reservation")
    if reservation_bytes > max_bytes:
        raise RetentionLimitError("operation reservation exceeds configured retention capacity")
    if not evidence_root.parent.is_dir():
        raise EvidenceError("evidence root parent must already exist")
    _directory(evidence_root, create=True)
    runs = evidence_root / "runs"
    _directory(runs, create=True)
    root_lock = _lock(runs, ".retention.lock")
    run: Path | None = None
    activity_fd: int | None = None
    try:
        _cleanup_locked(
            runs, max_bytes=max_bytes, max_age_seconds=max_age_seconds,
            additional_bytes=reservation_bytes,
        )
        invocation_id = str(uuid.uuid4())
        run = runs / invocation_id
        staging = run / "staging"
        run.mkdir(mode=0o700)
        _directory(run)
        activity_fd = _lock(run, ".active.lock")
        staging.mkdir(mode=0o700)
        _directory(staging)
        start_record = {
            "schema": "android-tools-run-start-v1", "invocation_id": invocation_id,
            "started_at": _time(), "operation": operation, "provider": provider,
            "context": context, "state": "started",
            "outcome": {"transport": "unknown", "effect": "unknown",
                        "error": None, "data": {}},
        }
        _write_file(
            run, "started.json", _json(start_record) + b"\n",
            limit=MAX_EVIDENCE_METADATA_BYTES,
        )
        _write_file(run, "reservation.json", _json({
            "schema": "android-tools-run-reservation-v1",
            "invocation_id": invocation_id, "reservation_bytes": reservation_bytes,
        }) + b"\n", limit=4096)
        return RunHandle(
            evidence_root, invocation_id, run, staging, run / "manifest.json",
            _RunLease(activity_fd),
        )
    except BaseException:
        try:
            if run is not None and run.exists() and not run.is_symlink():
                _remove_run(run)
        finally:
            if activity_fd is not None:
                _unlock(activity_fd)
        raise
    finally:
        _unlock(root_lock)


def _handle_paths(handle: RunHandle) -> tuple[Path, Path, Path, Path]:
    if not isinstance(handle, RunHandle):
        raise EvidenceError("invalid run handle")
    root = _path(handle.root, "evidence root")
    try:
        run_id = uuid.UUID(handle.invocation_id)
    except (ValueError, TypeError, AttributeError) as error:
        raise EvidenceError("invalid invocation id") from error
    if str(run_id) != handle.invocation_id or run_id.version != 4:
        raise EvidenceError("invalid invocation id")
    run, staging = root / "runs" / handle.invocation_id, root / "runs" / handle.invocation_id / "staging"
    manifest = run / "manifest.json"
    if (handle.run_directory != run or handle.staging_directory != staging
            or handle.manifest_path != manifest):
        raise EvidenceError("run handle paths differ")
    return root, run, staging, manifest


def _paths(handle: RunHandle) -> tuple[Path, Path, Path]:
    root, run, staging, manifest = _handle_paths(handle)
    _directory(root)
    _directory(root / "runs")
    _directory(run)
    _directory(staging)
    return run, staging, manifest


def _require_active(handle: RunHandle) -> _RunLease:
    if not isinstance(handle, RunHandle) or not isinstance(handle._lease, _RunLease):
        raise EvidenceError("run handle does not own an active invocation")
    if handle._lease.fd is None:
        raise EvidenceError("run invocation has already been released")
    return handle._lease


def _read_staged(staging: Path, name: str) -> bytes:
    name = _token(name, "staged file name")
    fd = _dirfd(staging)
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                          dir_fd=fd)
    except OSError as error:
        os.close(fd)
        raise EvidenceError("staged artifact unavailable") from error
    try:
        info = os.fstat(file_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or info.st_mode & 0o7000
                or info.st_size > MAX_ARTIFACT_BYTES):
            raise EvidenceError("unsafe or oversized staged artifact")
        data = bytearray()
        while True:
            chunk = os.read(file_fd, min(65536, MAX_ARTIFACT_BYTES + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
            if len(data) > MAX_ARTIFACT_BYTES:
                raise EvidenceError("staged artifact exceeds size limit")
    finally:
        os.close(file_fd)
        os.close(fd)


def _read_nested(staging: Path, parts: tuple[str, ...]) -> bytes:
    directory_fd = _dirfd(staging)
    try:
        for part in parts[:-1]:
            if part in {"", ".", ".."}:
                raise EvidenceError("invalid staged artifact path")
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory_fd,
            )
            info = os.fstat(next_fd)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_mode & 0o7000):
                os.close(next_fd)
                raise EvidenceError("unsafe staged artifact directory")
            os.close(directory_fd)
            directory_fd = next_fd
        name = parts[-1]
        if name in {"", ".", ".."}:
            raise EvidenceError("invalid staged artifact path")
        file_fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        try:
            info = os.fstat(file_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid() or info.st_size > MAX_ARTIFACT_BYTES
                    or info.st_mode & 0o7000):
                raise EvidenceError("unsafe staged artifact file")
            chunks, size = [], 0
            while True:
                block = os.read(file_fd, min(65536, MAX_ARTIFACT_BYTES + 1 - size))
                if not block:
                    break
                size += len(block)
                if size > MAX_ARTIFACT_BYTES:
                    raise EvidenceError("staged artifact exceeds size limit")
                chunks.append(block)
            return b"".join(chunks)
        finally:
            os.close(file_fd)
    except OSError as error:
        raise EvidenceError("unsafe staged artifact path") from error
    finally:
        os.close(directory_fd)


def stage_artifact(handle: RunHandle, name: str, content: bytes,
                   media_type: str = "application/octet-stream") -> StagedArtifact:
    """Stage bytes explicitly; nothing is captured or persisted implicitly."""
    lease = _require_active(handle)
    with lease.guard:
        if lease.fd is None:
            raise EvidenceError("run invocation has already been released")
        _, staging, _ = _paths(handle)
        return _stage_artifact_active(staging, name, content, media_type)


def _stage_artifact_active(staging: Path, name: str, content: bytes,
                           media_type: str) -> StagedArtifact:
    if (not isinstance(name, str) or not _NAME.fullmatch(name)
            or name in {".", ".."} or ".." in name
            or re.search(r"(?:secret|credential|password|token|auth)", name, re.I)):
        raise EvidenceError("invalid or credential-like artifact name")
    if not isinstance(media_type, str) or not _MEDIA.fullmatch(media_type):
        raise EvidenceError("invalid artifact media type")
    if not isinstance(content, bytes) or len(content) > MAX_ARTIFACT_BYTES:
        raise EvidenceError("invalid staged artifact bytes")
    _write_file(staging, name, content, limit=MAX_ARTIFACT_BYTES)
    return StagedArtifact(name, media_type)


def _native_outcome(value: Any) -> tuple[dict[str, Any], list[Any]]:
    if isinstance(value, RunOutcome):
        result = value.to_dict()
    elif isinstance(value, Mapping):
        result = dict(value)
    elif callable(getattr(value, "to_dict", None)):
        result = value.to_dict()
    else:
        raise EvidenceError("outcome must be a NativeResult or closed mapping")
    if (not isinstance(result, dict)
            or set(result) != {"transport", "effect", "error", "data", "artifacts"}):
        raise EvidenceError("outcome does not match NativeResult")
    if result["transport"] not in _TRANSPORTS or result["effect"] not in _EFFECTS:
        raise EvidenceError("invalid transport or effect")
    error = result["error"]
    if error is not None and (not isinstance(error, str) or error not in _ERRORS):
        raise EvidenceError("invalid categorical error")
    raw_data = result["data"]
    data: dict[str, Any] = {}
    if isinstance(raw_data, Mapping):
        for key, item in raw_data.items():
            if key in _DATA_BOOL and type(item) is bool:
                data[key] = item
            elif key in _DATA_INT and type(item) is int and 0 <= item <= 2**31 - 1:
                data[key] = item
            elif (key == "state" and isinstance(item, str)
                    and item in {"present", "absent", "unknown", "sent"}):
                data[key] = item
            elif key == "selector" and isinstance(item, Mapping):
                field = item.get("field")
                if isinstance(field, str) and field in {"text", "resource_id"}:
                    data[key] = {"field": field}
    return {
        "transport": result["transport"], "effect": result["effect"],
        "error": error, "data": data,
    }, list(result["artifacts"] or [])


def _artifact_metadata(staging: Path, item: Any) -> tuple[str, str, bytes, int | None, int | None]:
    if isinstance(item, StagedArtifact):
        name, media = item.name, item.media_type
        if not isinstance(name, str) or not _NAME.fullmatch(name) or ".." in name:
            raise EvidenceError("invalid artifact name")
        if not isinstance(media, str) or not _MEDIA.fullmatch(media):
            raise EvidenceError("invalid artifact media type")
        raw = _read_staged(staging, name)
        if media == "image/png":
            width, height = _png_dimensions(raw)
            return name, media, raw, width, height
        return name, media, raw, None, None
    required = {"path", "media_type", "size_bytes", "sha256"}
    png_fields = required | {"width", "height"}
    if not isinstance(item, Mapping) or set(item) not in (required, png_fields):
        raise EvidenceError("native artifact metadata is incomplete")
    path = _path(item["path"], "artifact path")
    try:
        relative = path.relative_to(staging)
    except ValueError as error:
        raise EvidenceError("artifact is outside invocation staging") from error
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise EvidenceError("invalid staged artifact path")
    for part in relative.parts:
        if (not _NAME.fullmatch(part) or ".." in part
                or re.search(r"(?:secret|credential|password|token|auth)", part, re.I)):
            raise EvidenceError("unsafe artifact path")
    # The staging directory is private; still reject every symlink in nested outputs.
    current = staging
    for part in relative.parts:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise EvidenceError("symlink in staged artifact path")
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise EvidenceError("invalid staged artifact parent")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise EvidenceError("staged artifact is not a regular file")
    raw = _read_nested(staging, relative.parts)
    if (type(item["size_bytes"]) is not int or len(raw) > MAX_ARTIFACT_BYTES
            or len(raw) != item["size_bytes"]):
        raise EvidenceError("staged artifact size differs")
    digest = _digest(item["sha256"], "artifact digest")
    if _sha(raw) != digest:
        raise EvidenceError("staged artifact hash differs")
    media = item["media_type"]
    if not isinstance(media, str) or not _MEDIA.fullmatch(media):
        raise EvidenceError("invalid artifact media type")
    width = height = None
    if media == "image/png":
        if set(item) != png_fields:
            raise EvidenceError("PNG dimensions are missing")
        width, height = _png_dimensions(raw)
        if (type(item["width"]) is not int or type(item["height"]) is not int
                or item["width"] != width or item["height"] != height):
            raise EvidenceError("PNG dimensions differ")
    elif set(item) != required:
        raise EvidenceError("non-PNG artifact has dimensions")
    return relative.as_posix(), media, raw, width, height


def _png_dimensions(raw: bytes) -> tuple[int, int]:
    if len(raw) < 33 or not raw.startswith(_PNG_SIGNATURE):
        raise EvidenceError("invalid PNG artifact")
    offset, width, height = len(_PNG_SIGNATURE), 0, 0
    saw_header = saw_end = False
    while offset + 12 <= len(raw):
        size = struct.unpack(">I", raw[offset:offset + 4])[0]
        end = offset + size + 12
        if end > len(raw):
            raise EvidenceError("invalid PNG chunk")
        kind = raw[offset + 4:offset + 8]
        payload = raw[offset + 8:offset + 8 + size]
        crc = struct.unpack(">I", raw[offset + 8 + size:end])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc:
            raise EvidenceError("invalid PNG checksum")
        if not saw_header:
            if kind != b"IHDR" or size != 13:
                raise EvidenceError("PNG header is missing")
            width, height = struct.unpack(">II", payload[:8])
            if not 0 < width <= 16384 or not 0 < height <= 16384:
                raise EvidenceError("invalid PNG dimensions")
            saw_header = True
        if kind == b"IEND":
            if size or end != len(raw):
                raise EvidenceError("invalid PNG ending")
            saw_end = True
            break
        offset = end
    if not saw_header or not saw_end:
        raise EvidenceError("incomplete PNG artifact")
    return width, height


def _store_artifact(run: Path, name: str, media: str, raw: bytes,
                    width: int | None, height: int | None) -> dict[str, Any]:
    directory = run / "artifacts"
    _directory(directory, create=True)
    digest = _sha(raw)
    try:
        _write_file(directory, digest, raw, limit=MAX_ARTIFACT_BYTES)
    except EvidenceError:
        if _read_staged(directory, digest) != raw:
            raise EvidenceError("artifact object differs")
    value: dict[str, Any] = {
        "name": name, "media_type": media, "sha256": digest, "size_bytes": len(raw),
        "local_locator": f"artifacts/{digest}", "exported": False,
    }
    if width is not None and height is not None:
        value.update(width=width, height=height)
    return value


def finish_run(handle: RunHandle, outcome: Any,
               artifacts: Iterable[Any] | None = None) -> Path:
    lease = _require_active(handle)
    with lease.guard:
        if lease.fd is None:
            raise EvidenceError("run invocation has already been released")
        return _finish_run_active(handle, outcome, artifacts)


def _finish_run_active(handle: RunHandle, outcome: Any,
                       artifacts: Iterable[Any] | None = None) -> Path:
    """Finalize sanitized result data and verified local artifact bytes."""
    run, staging, manifest_path = _paths(handle)
    lock = _lock(run, ".record.lock")
    try:
        started = _read_json(run, "started.json")
        if started.get("invocation_id") != handle.invocation_id:
            raise EvidenceError("run start identity differs")
        normalized, native_artifacts = _native_outcome(outcome)
        selected = native_artifacts if artifacts is None else list(artifacts)
        if len(selected) > MAX_ARTIFACTS:
            raise EvidenceError("too many artifacts")
        entries, names, total = [], set(), 0
        for item in selected:
            name, media, raw, width, height = _artifact_metadata(staging, item)
            if name in names:
                raise EvidenceError("duplicate artifact name")
            names.add(name)
            total += len(raw)
            if total > MAX_TOTAL_ARTIFACT_BYTES:
                raise EvidenceError("artifact retention limit exceeded")
            entries.append(_store_artifact(run, name, media, raw, width, height))
        base = {
            "schema": "android-tools-run-v1", "invocation_id": handle.invocation_id,
            "state": "finished", "started_at": started["started_at"],
            "operation": started["operation"], "provider": started["provider"],
            "context": started["context"], "outcome": normalized, "artifacts": entries,
        }
        try:
            existing = _read_json(run, "manifest.json")
        except FileNotFoundError:
            existing = None
        if existing is not None:
            compare = {key: value for key, value in existing.items() if key != "finished_at"}
            if compare != base:
                raise EvidenceError("run already has a different final record")
            return manifest_path
        _write_file(
            run, "manifest.json", _json(base | {"finished_at": _time()}) + b"\n",
            limit=MAX_EVIDENCE_METADATA_BYTES,
        )
        return manifest_path
    finally:
        _unlock(lock)


def _unlink_private_file(directory: Path, name: str) -> None:
    parent = _dirfd(directory)
    try:
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_mode & 0o7000):
            raise EvidenceError("unsafe run reservation file")
        os.unlink(name, dir_fd=parent)
        os.fsync(parent)
    except OSError as error:
        raise EvidenceError("cannot release run reservation") from error
    finally:
        os.close(parent)


def release_run(handle: RunHandle) -> None:
    """Release a run's reservation and activity pin; repeated calls are safe."""
    if not isinstance(handle, RunHandle):
        raise EvidenceError("invalid run handle")
    lease = handle._lease
    if lease is None:
        return
    with lease.guard:
        activity_fd = lease.fd
        if activity_fd is None:
            return
        released = False
        try:
            run, _, _ = _paths(handle)
            runs = run.parent
            root_lock = _lock(runs, ".retention.lock")
            try:
                try:
                    _unlink_private_file(run, "reservation.json")
                finally:
                    lease.fd = None
                    _unlock(activity_fd)
                    released = True
            finally:
                _unlock(root_lock)
        finally:
            if not released:
                lease.fd = None
                _unlock(activity_fd)


def open_run(root: str | os.PathLike[str], invocation_id: str) -> RunHandle:
    """Construct an exact export handle without reading the local record."""
    evidence_root = _path(root, "evidence root")
    try:
        parsed = uuid.UUID(invocation_id)
    except (ValueError, TypeError, AttributeError) as error:
        raise EvidenceError("invalid invocation id") from error
    if str(parsed) != invocation_id or parsed.version != 4:
        raise EvidenceError("invalid invocation id")
    run = evidence_root / "runs" / invocation_id
    handle = RunHandle(evidence_root, invocation_id, run, run / "staging", run / "manifest.json")
    return handle


def _binding(value: Mapping[str, Any], interface: str) -> tuple[dict[str, str], dict[str, str]]:
    if not isinstance(value, Mapping):
        raise BindingError("explicit memory binding required")
    if interface == "standalone":
        required = {"project_root", "project_key", "project_uid", "task_key", "task_uid",
                    "task_binding_sha256"}
        if set(value) != required:
            raise BindingError("standalone binding is incomplete")
        root = _path(value["project_root"], "project root")
        project_key = _token(value["project_key"], "project key")
        identity = {
            "project_uid": _token(value["project_uid"], "project uid"),
            "task_key": _token(value["task_key"], "task key"),
            "task_uid": _token(value["task_uid"], "task uid"),
            "task_binding_sha256": _digest(value["task_binding_sha256"], "task binding hash"),
        }
        return identity, {"project_root": str(root), "project_key": project_key, **identity}
    if interface == "workflow":
        required = {"workspace_path", "project_path", "project_uid", "task_key", "task_uid",
                    "task_binding_sha256"}
        if set(value) != required:
            raise BindingError("workflow binding is incomplete")
        identity = {
            "project_uid": _token(value["project_uid"], "project uid"),
            "task_key": _token(value["task_key"], "task key"),
            "task_uid": _token(value["task_uid"], "task uid"),
            "task_binding_sha256": _digest(value["task_binding_sha256"], "task binding hash"),
        }
        return identity, {
            "workspace_path": str(_path(value["workspace_path"], "workspace")),
            "project_path": str(_path(value["project_path"], "project path")), **identity,
        }
    raise BindingError("unsupported memory interface")


def _helper(value: str | os.PathLike[str], interface: str) -> Path:
    helper = _path(value, "project-memory helper")
    expected = "project-memory" if interface == "standalone" else "workflow-project-memory"
    if helper.name != expected:
        raise BindingError("helper does not match memory interface")
    info = helper.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise BindingError("project-memory helper is not a regular file")
    return helper


def _call(helper: Path, interface: str, request: bytes,
          standalone_args: list[str] | None = None) -> tuple[int | None, bytes]:
    command = [sys.executable, str(helper)]
    if interface == "workflow":
        command.append("workflow")
    if standalone_args:
        command.extend(standalone_args)
    try:
        child = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError:
        return None, b""
    assert child.stdin is not None and child.stdout is not None
    selector = selectors.DefaultSelector()
    input_fd = child.stdin.fileno()
    output_fd = child.stdout.fileno()
    os.set_blocking(input_fd, False)
    os.set_blocking(output_fd, False)
    output = bytearray()
    offset = 0
    started = time.monotonic()
    deadline = started + 60

    def stop_child() -> None:
        if child.poll() is None:
            try:
                child.kill()
            except ProcessLookupError:
                pass
        remaining = max(0.0, deadline - time.monotonic())
        try:
            child.wait(timeout=min(remaining, 1.0))
        except subprocess.TimeoutExpired:
            pass

    if request:
        selector.register(input_fd, selectors.EVENT_WRITE, "input")
    else:
        child.stdin.close()
    selector.register(output_fd, selectors.EVENT_READ, "output")
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop_child()
                return None, b""
            for key, _ in selector.select(remaining):
                if key.data == "input":
                    try:
                        written = os.write(input_fd, request[offset:offset + 65536])
                    except BrokenPipeError:
                        written = 0
                        offset = len(request)
                    offset += written
                    if offset >= len(request):
                        selector.unregister(input_fd)
                        child.stdin.close()
                else:
                    block = os.read(
                        output_fd, min(65536, MAX_HELPER_OUTPUT_BYTES + 1 - len(output)),
                    )
                    if not block:
                        selector.unregister(output_fd)
                        child.stdout.close()
                    else:
                        output.extend(block)
                        if len(output) > MAX_HELPER_OUTPUT_BYTES:
                            stop_child()
                            return child.returncode, b""
        try:
            remaining = max(0.0, deadline - time.monotonic())
            return child.wait(timeout=remaining), bytes(output)
        except subprocess.TimeoutExpired:
            stop_child()
            return None, b""
    finally:
        selector.close()
        if child.poll() is None:
            stop_child()
        child.stdin.close()
        child.stdout.close()


def _workflow_result(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BindingError("invalid workflow binding receipt") from error
    if (not isinstance(value, dict) or set(value) != {"schema", "result"}
            or value.get("schema") != "r2-workflow-response-v1"
            or not isinstance(value.get("result"), dict)):
        raise BindingError("invalid workflow binding receipt")
    return value["result"]


def _confirm_workflow(helper: Path, args: Mapping[str, str]) -> tuple[bool, str]:
    request = _json({"schema": "r2-workflow-request-v1", "operation": "resolve-task",
                     "workspace_path": args["workspace_path"]}) + b"\n"
    code, output = _call(helper, "workflow", request)
    if code != 0:
        return False, "binding_unavailable"
    try:
        result = _workflow_result(output)
    except BindingError:
        return False, "invalid_binding_receipt"
    locator = result.get("binding_ref")
    task_file = Path(args["project_path"]) / "tasks" / args["task_key"] / "task.json"
    if (result.get("mode") != "r2" or result.get("task_key") != args["task_key"]
            or result.get("project_path") != args["project_path"]
            or result.get("project_uid") != args["project_uid"]
            or result.get("task_uid") != args["task_uid"]
            or not isinstance(locator, dict) or set(locator) != {"path", "sha256"}
            or locator.get("path") != str(task_file)
            or locator.get("sha256") != args["task_binding_sha256"]):
        return False, "binding_mismatch"
    return True, ""


def _write_attachment(helper: Path, interface: str, args: Mapping[str, str],
                      content: bytes) -> tuple[bool, str]:
    digest = _sha(content)
    request: dict[str, Any] = {
        "schema": "r2-request-v1" if interface == "standalone" else "r2-workflow-request-v1",
        "operation": "artifact-write", "sha256": digest, "byte_size": len(content),
    }
    if interface == "standalone":
        request["task_key"] = args["task_key"]
        standalone_args = ["--root", args["project_root"], "--project", args["project_key"],
                           "--project-uid", args["project_uid"]]
        payload = _json(request) + b"\n" + content
    else:
        request["workspace_path"] = args["workspace_path"]
        standalone_args = None
        payload = _json(request) + b"\n" + content
    code, output = _call(helper, interface, payload, standalone_args)
    if code is None:
        return False, "helper_timeout"
    if code != 0:
        return False, "helper_failed"
    header, separator, body = output.partition(b"\n")
    if not separator or body:
        return False, "invalid_receipt"
    try:
        receipt = json.loads(header)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, "invalid_receipt"
    root = (Path(args["project_root"]) / args["project_key"]
            if interface == "standalone" else Path(args["project_path"]))
    expected = str(root / "tasks" / args["task_key"] / "artifacts/attachments/objects" / digest)
    if (not isinstance(receipt, dict) or receipt.get("schema") != "r2-artifact-response-v1"
            or receipt.get("operation") != "artifact-write"
            or receipt.get("object_sha256") != digest
            or type(receipt.get("byte_size")) is not int
            or receipt.get("byte_size") != len(content)
            or receipt.get("object_path") != expected):
        return False, "receipt_mismatch"
    return True, ""


def _history(run: Path) -> list[dict[str, Any]]:
    try:
        raw = _read_file(run, "export.jsonl", MAX_EXPORT_LOG_BYTES)
    except FileNotFoundError:
        try:
            fd = _dirfd(run)
            try:
                os.stat("export.jsonl", dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return []
            raise
        finally:
            os.close(fd)
    records = []
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EvidenceError("invalid export history") from error
        if not isinstance(record, dict) or record.get("schema") != "android-tools-export-v1":
            raise EvidenceError("invalid export history")
        records.append(record)
    return records


def _next_export_attempt(history: list[dict[str, Any]]) -> int:
    attempts = []
    for entry in history:
        attempt = entry.get("attempt")
        if type(attempt) is not int or attempt < 1:
            raise EvidenceError("invalid export attempt history")
        attempts.append(attempt)
    return max(attempts, default=0) + 1


def _append_export(run: Path, record: Mapping[str, Any]) -> None:
    line = _json(record) + b"\n"
    parent = _dirfd(run)
    try:
        fd = os.open("export.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or info.st_mode & 0o7000 or info.st_size + len(line) > MAX_EXPORT_LOG_BYTES):
                raise EvidenceError("unsafe or oversized export history")
            fcntl.flock(fd, fcntl.LOCK_EX)
            remaining = memoryview(line)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise EvidenceError("short export-history write")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _write_export_reservation_locked(run: Path, invocation_id: str,
                                     info: Mapping[str, Any]) -> int:
    existing = _read_reservation(run, invocation_id)
    reservation = max(
        info["contribution"] + 4096, existing or 0, RESERVATION_METADATA_BYTES,
    )
    if reservation > MAX_MAX_BYTES:
        raise RetentionLimitError("export exceeds configured retention capacity")
    if existing is None or existing < reservation:
        if existing is not None:
            _unlink_private_file(run, "reservation.json")
        _write_file(run, "reservation.json", _json({
            "schema": "android-tools-run-reservation-v1",
            "invocation_id": invocation_id,
            "reservation_bytes": reservation,
        }) + b"\n", limit=4096)
    return reservation


def _prepare_export_pin(run: Path, invocation_id: str, *,
                        max_bytes: int, max_age_seconds: int) -> int:
    """Atomically acquire an export pin and publish its capacity reservation."""
    while True:
        root_lock = _lock(run.parent, ".retention.lock")
        activity_fd: int | None = None
        reservation_published = False
        waiting = False
        try:
            activity_fd = _try_lock(run, ".active.lock")
            if activity_fd is None:
                waiting = True
            else:
                info = _run_info(run, invocation_id)
                _write_export_reservation_locked(run, invocation_id, info)
                reservation_published = True
                _cleanup_locked(
                    run.parent, max_bytes=max_bytes,
                    max_age_seconds=max_age_seconds,
                )
                return activity_fd
        except BaseException:
            if activity_fd is not None:
                try:
                    if reservation_published:
                        _unlink_private_file(run, "reservation.json")
                finally:
                    _unlock(activity_fd)
                    activity_fd = None
            raise
        finally:
            _unlock(root_lock)
        # Never wait for an activity lock while holding the retention lock:
        # retry atomically after the current owner has released its pin.
        if waiting:
            time.sleep(0.01)


def export_run(handle: RunHandle, binding: Mapping[str, Any], *, helper: str | os.PathLike[str],
               interface: str = "standalone", include_artifacts: bool = False,
               max_bytes: int = DEFAULT_MAX_BYTES,
               max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS) -> ExportResult:
    """Export pinned evidence; retries never invoke Android tools."""
    max_bytes, max_age_seconds = _retention_limits(max_bytes, max_age_seconds)
    if type(include_artifacts) is not bool:
        raise EvidenceError("include_artifacts must be boolean")
    identity, args = _binding(binding, interface)
    helper_path = _helper(helper, interface)
    _, run, _, _ = _handle_paths(handle)
    lease = handle._lease
    if lease is not None:
        with lease.guard:
            if lease.fd is None:
                raise EvidenceError("run invocation has already been released")
            return _export_pinned(
                handle, run, identity, args, helper_path, interface, include_artifacts,
                max_bytes, max_age_seconds,
            )
    activity_fd = _prepare_export_pin(
        run, handle.invocation_id, max_bytes=max_bytes,
        max_age_seconds=max_age_seconds,
    )
    temporary_run = {"validated": True}
    try:
        return _export_pinned(
            handle, run, identity, args, helper_path, interface, include_artifacts,
            max_bytes, max_age_seconds, temporary_run,
        )
    finally:
        root_lock = None
        try:
            if temporary_run["validated"]:
                root_lock = _lock(run.parent, ".retention.lock")
                try:
                    try:
                        _unlink_private_file(run, "reservation.json")
                    finally:
                        _unlock(activity_fd)
                        activity_fd = -1
                finally:
                    _unlock(root_lock)
        finally:
            if activity_fd >= 0:
                _unlock(activity_fd)


def _export_pinned(handle: RunHandle, run: Path, identity: Mapping[str, str],
                   args: Mapping[str, str], helper_path: Path, interface: str,
                   include_artifacts: bool, max_bytes: int,
                   max_age_seconds: int,
                   temporary_run: dict[str, bool] | None = None) -> ExportResult:
    record_lock = _lock(run, ".record.lock")
    export_lock: int | None = None
    try:
        export_lock = _lock(run, ".export.lock")
        _paths(handle)
        info = _run_info(run, handle.invocation_id)
        if temporary_run is not None:
            temporary_run["validated"] = True
        root_lock = _lock(run.parent, ".retention.lock")
        try:
            _write_export_reservation_locked(run, handle.invocation_id, info)
            _cleanup_locked(
                run.parent, max_bytes=max_bytes, max_age_seconds=max_age_seconds,
            )
        finally:
            _unlock(root_lock)

        manifest_raw = _read_file(run, "manifest.json", MAX_EVIDENCE_METADATA_BYTES)
        try:
            manifest = json.loads(manifest_raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EvidenceError("invalid run manifest") from error
        if (not isinstance(manifest, dict) or manifest.get("schema") != "android-tools-run-v1"
                or manifest.get("state") != "finished"
                or manifest.get("invocation_id") != handle.invocation_id):
            raise EvidenceError("only a finished run can be exported")
        manifest_sha = _sha(manifest_raw)
        artifact_bytes = []
        if include_artifacts:
            for item in manifest.get("artifacts", []):
                digest = _digest(item.get("sha256"), "artifact hash")
                if item.get("local_locator") != f"artifacts/{digest}":
                    raise EvidenceError("invalid local artifact locator")
                content = _read_file(run / "artifacts", digest, MAX_ARTIFACT_BYTES)
                if len(content) != item.get("size_bytes") or _sha(content) != digest:
                    raise EvidenceError("local artifact hash mismatch")
                artifact_bytes.append((digest, content))
        envelope = _json({
            "schema": "android-tools-evidence-export-v1", "binding": identity,
            "manifest_sha256": manifest_sha, "manifest": manifest,
        }) + b"\n"
        if len(envelope) > MAX_RECORD_BYTES:
            raise EvidenceError("export envelope exceeds size limit")
        objects = [(envelope, _sha(envelope))]
        objects.extend((content, digest) for digest, content in artifact_bytes)
        hashes = tuple(digest for _, digest in objects)
        binding_sha = _sha(_json({"interface": interface, "binding": args}))
        history = _history(run)
        attempt = _next_export_attempt(history)
        if interface == "workflow":
            valid, reason = _confirm_workflow(helper_path, args)
            if not valid:
                _append_export(run, {
                    "schema": "android-tools-export-v1", "attempt": attempt, "at": _time(),
                    "status": "blocked", "reason_code": reason, "interface": interface,
                    "binding_sha256": binding_sha, "manifest_sha256": manifest_sha,
                    "attachment_sha256s": list(hashes),
                })
                return ExportResult("blocked", manifest_sha, hashes, attempt, reason)
        previous = next((entry for entry in reversed(history)
                         if entry.get("interface") == interface
                         and entry.get("binding_sha256") == binding_sha
                         and entry.get("manifest_sha256") == manifest_sha
                         and entry.get("attachment_sha256s") == list(hashes)), None)
        if (previous is not None and previous.get("status") == "exported"
                and _export_record_matches(
                    previous, interface=interface, identity=identity,
                    binding_sha=binding_sha, manifest_sha=manifest_sha,
                    hashes=hashes, objects=objects,
                )):
            return ExportResult("exported", manifest_sha, hashes, previous["attempt"])
        exported = []
        reason = ""
        _append_export(run, {
            "schema": "android-tools-export-v1", "attempt": attempt, "at": _time(),
            "status": "pending", "reason_code": "", "interface": interface,
            "binding_sha256": binding_sha, "binding": identity,
            "manifest_sha256": manifest_sha, "attachment_sha256s": list(hashes),
            "exported_attachments": [],
        })
        for content, digest in objects:
            ok, reason = _write_attachment(helper_path, interface, args, content)
            if not ok:
                break
            exported.append({
                "sha256": digest, "size_bytes": len(content),
                "locator": (f"tasks/{identity['task_key']}/artifacts/attachments/objects/"
                            f"{digest}"),
            })
        status = "exported" if len(exported) == len(objects) else "unknown"
        _append_export(run, {
            "schema": "android-tools-export-v1", "attempt": attempt, "at": _time(),
            "status": status, "reason_code": reason, "interface": interface,
            "binding_sha256": binding_sha, "binding": identity,
            "manifest_sha256": manifest_sha, "attachment_sha256s": list(hashes),
            "exported_attachments": exported,
        })
        return ExportResult(status, manifest_sha, hashes, attempt, reason)
    finally:
        if export_lock is not None:
            _unlock(export_lock)
        _unlock(record_lock)


__all__ = [
    "BindingError", "EvidenceError", "ExportResult", "RetentionLimitError", "RunHandle",
    "RunOutcome", "StagedArtifact", "begin_run", "export_run", "finish_run", "open_run",
    "release_run", "reservation_for_operation", "stage_artifact",
]
