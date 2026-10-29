"""Bounded native Android CLI provider for task-scoped mobile tools."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import errno
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import struct
import subprocess
import threading
import time
from typing import Any, Iterator, Mapping
import xml.etree.ElementTree as ET
import zlib


BUNDLE_PROTOCOL = 1

MAX_TIMEOUT_SECONDS = 300.0
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_JSON_NODES = 10_000
MAX_JSON_DEPTH = 32
MAX_COMPONENT_TREE_DEPTH = 64
MAX_UI_XML_DEPTH = 64
MAX_PNG_PIXELS = 16_000_000
MAX_PNG_DECOMPRESSED_BYTES = 64 * 1024 * 1024
GROUP_CLEANUP_SECONDS = 3.0
GROUP_TERM_GRACE_SECONDS = 0.3

_OPERATIONS = {
    "docs.search",
    "docs.fetch",
    "preview.list",
    "preview.render",
    "device.tree",
    "device.assert-visible",
    "device.assert-gone",
    "device.launch",
    "device.tap",
    "device.key",
    "device.swipe",
    "device.capture",
}
_DEVICE_OPERATIONS = _OPERATIONS - {
    "docs.search",
    "docs.fetch",
    "preview.list",
    "preview.render",
}
_GOOGLE_OPERATIONS = {
    "docs.search",
    "docs.fetch",
    "preview.list",
    "preview.render",
}
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|token|api[_-]?key|password|passphrase|secret|cookie|credential)",
    re.IGNORECASE,
)
_SECRET_TEXT = re.compile(
    r"(?i)\b(authorization|(?:access|refresh|api|auth)?[_-]?token|api[_-]?key|password|"
    r"passphrase|secret|cookie|credential)\b"
    r"(\s*[:=]\s*)([^\r\n]+)"
)
_SERIAL = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\Z")
_TARGET = re.compile(r"[A-Za-z0-9_.:-]+@[A-Za-z0-9_.-]+\Z")
_FQN = re.compile(r"[A-Za-z_$][A-Za-z0-9_$.]*\Z")
_UI_BOUNDS = re.compile(r"\[\d+,\d+\]\[\d+,\d+\]\Z")
_UI_XML_NODE_ATTRIBUTES = {
    "index",
    "text",
    "resource-id",
    "class",
    "package",
    "content-desc",
    "checkable",
    "checked",
    "clickable",
    "enabled",
    "focusable",
    "focused",
    "scrollable",
    "long-clickable",
    "password",
    "selected",
    "NAF",
    "bounds",
}
_UI_XML_BOOLEAN_ATTRIBUTES = {
    "checkable",
    "checked",
    "clickable",
    "enabled",
    "focusable",
    "focused",
    "scrollable",
    "long-clickable",
    "password",
    "selected",
    "NAF",
}
_KEYS = {
    "back",
    "home",
    "recents",
    "power",
    "volume-up",
    "volume-down",
    "enter",
    "tab",
    "esc",
    "up",
    "down",
    "left",
    "right",
    "center",
}
_DIRECTIONS = {"up", "down", "left", "right"}


@dataclass(frozen=True)
class NativeResult:
    """Machine-readable provider outcome with transport and effect separated."""

    transport: str
    effect: str
    error: str | None = None
    data: Any = None
    artifacts: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["artifacts"] = list(self.artifacts)
        return value


@dataclass(frozen=True)
class _CallSpec:
    argv: tuple[str, ...]
    output_kind: str
    effectful: bool = False
    artifact_path: Path | None = None
    artifact_dir: Path | None = None
    assertion: tuple[str, str, bool] | None = None
    cwd: Path | None = None
    fqn: str | None = None


@dataclass(frozen=True)
class _ProcessOutput:
    stdout: bytes
    exit_code: int | None
    error: str | None = None


class _InputError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _TerminationRequested(Exception):
    """Internal signal used to clean up a child group before terminating."""


def execute_android_native(
    provider_id: str,
    operation: str,
    arguments: Mapping[str, object],
    *,
    executable: Path,
    expected_version: str,
    timeout_s: float = 30.0,
    output_limit_bytes: int = 1024 * 1024,
    source_root: Path | None = None,
    staging_dir: Path | None = None,
) -> NativeResult:
    """Execute one closed native operation without shell expansion or retries."""

    try:
        if provider_id not in {"mobile-native", "google-android"}:
            raise _InputError("unsupported_provider")
        if operation not in _OPERATIONS:
            raise _InputError("unsupported_operation")
        if not isinstance(arguments, Mapping):
            raise _InputError("invalid_arguments")
        if provider_id == "mobile-native" and operation not in _DEVICE_OPERATIONS:
            raise _InputError("unsupported_operation")
        if provider_id == "google-android" and operation not in _GOOGLE_OPERATIONS:
            raise _InputError("unsupported_operation")

        timeout = _bounded_timeout(timeout_s)
        output_limit = _bounded_output_limit(output_limit_bytes)
        executable_path = _validate_executable(executable)
        version = _validate_version(expected_version)
        spec = _build_call(
            provider_id,
            operation,
            arguments,
            source_root=source_root,
            staging_dir=staging_dir,
        )
    except _InputError as error:
        return _not_started(error.code)
    except (OSError, TypeError, ValueError):
        return _not_started("invalid_arguments")

    command_prefix = ("--no-metrics",) if provider_id == "google-android" else ()
    version_output = _run_process(
        (str(executable_path), *command_prefix, "--version"),
        timeout_s=5.0,
        output_limit_bytes=min(output_limit, 4096),
    )
    if version_output.error is not None:
        return _process_failure(version_output, effectful=False)
    if version_output.exit_code != 0 or not _version_matches(version_output.stdout, version):
        return _not_started("provider_version_mismatch")

    result = _run_process(
        (str(executable_path), *command_prefix, *spec.argv),
        timeout_s=timeout,
        output_limit_bytes=output_limit,
        cwd=spec.cwd,
    )
    if result.error is not None:
        return _process_failure(result, effectful=spec.effectful)
    if result.exit_code != 0:
        return NativeResult(
            transport="failed",
            effect="unknown" if spec.effectful else "not_applicable",
            error="provider_failed",
        )

    try:
        data, artifacts, error = _interpret_output(spec, result.stdout, output_limit)
    except _InputError as failure:
        return NativeResult(
            transport="completed",
            effect="unknown" if spec.effectful else "not_applicable",
            error=failure.code,
        )
    return NativeResult(
        transport="completed",
        effect="unknown" if spec.effectful else "not_applicable",
        error=error,
        data=data,
        artifacts=tuple(artifacts),
    )


def _not_started(error: str) -> NativeResult:
    return NativeResult(transport="not_started", effect="not_dispatched", error=error)


def _process_failure(output: _ProcessOutput, *, effectful: bool) -> NativeResult:
    if output.error == "provider_unavailable":
        return NativeResult(
            transport="failed",
            effect="not_dispatched",
            error=output.error,
        )
    if output.error in {"query_timeout", "interrupted"}:
        return NativeResult(
            transport="timed_out" if output.error == "query_timeout" else "unknown",
            effect="unknown" if effectful else "not_applicable",
            error=output.error,
        )
    if output.error == "observation_limit":
        return NativeResult(
            transport="failed",
            effect="unknown" if effectful else "not_applicable",
            error=output.error,
        )
    return NativeResult(transport="unknown", effect="unknown", error="drain_unknown")


def _bounded_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InputError("invalid_arguments")
    timeout = float(value)
    if not math.isfinite(timeout) or not 0.1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise _InputError("invalid_arguments")
    return timeout


def _bounded_output_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _InputError("invalid_arguments")
    if not 1024 <= value <= MAX_OUTPUT_BYTES:
        raise _InputError("invalid_arguments")
    return value


def _validate_executable(value: object) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise _InputError("provider_unavailable")
    try:
        resolved = value.resolve(strict=True)
    except OSError as error:
        raise _InputError("provider_unavailable") from error
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise _InputError("provider_unavailable")
    return resolved


def _validate_version(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise _InputError("invalid_arguments")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", value):
        raise _InputError("invalid_arguments")
    return value


def _version_matches(output: bytes, expected: str) -> bool:
    text = output.decode("utf-8", errors="replace").strip()
    return re.search(
        rf"(?<![A-Za-z0-9._+-]){re.escape(expected)}(?![A-Za-z0-9._+-])",
        text,
    ) is not None


def _closed_arguments(
    arguments: Mapping[str, object],
    allowed: set[str],
    required: set[str],
) -> None:
    if any(not isinstance(key, str) for key in arguments):
        raise _InputError("invalid_arguments")
    if set(arguments) - allowed or required - set(arguments):
        raise _InputError("invalid_arguments")
    if any(_SENSITIVE_KEY.search(key) for key in arguments):
        raise _InputError("secret_transport_unsupported")


def _text_arg(value: object, *, limit: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit:
        raise _InputError("invalid_arguments")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _InputError("invalid_arguments")
    if _SECRET_TEXT.search(value):
        raise _InputError("secret_transport_unsupported")
    return value


def _selector(arguments: Mapping[str, object]) -> tuple[str, str]:
    has_text = "text" in arguments
    has_resource = "resource_id" in arguments
    if has_text == has_resource:
        raise _InputError("invalid_arguments")
    field = "text" if has_text else "resource_id"
    value = _text_arg(arguments[field], limit=512)
    if any(character in value for character in ",=\\"):
        raise _InputError("invalid_arguments")
    if field == "resource_id" and not re.fullmatch(r"[A-Za-z0-9_./:$-]{1,256}", value):
        raise _InputError("invalid_arguments")
    return field, value


def _serial(arguments: Mapping[str, object]) -> str:
    value = arguments.get("device_serial")
    if not isinstance(value, str) or not _SERIAL.fullmatch(value):
        raise _InputError("device_serial_required")
    return value


def _source_root(value: Path | None) -> Path:
    if not isinstance(value, Path) or not value.is_absolute() or value.is_symlink():
        raise _InputError("source_root_required")
    try:
        resolved = value.resolve(strict=True)
    except OSError as error:
        raise _InputError("source_root_required") from error
    if not resolved.is_dir():
        raise _InputError("source_root_required")
    return resolved


def _staging_output(
    staging_dir: Path | None,
    name_value: object,
    *,
    default_name: str,
) -> Path:
    if not isinstance(staging_dir, Path) or not staging_dir.is_absolute():
        raise _InputError("staging_directory_required")
    if staging_dir.is_symlink():
        raise _InputError("invalid_output_path")
    try:
        directory = staging_dir.resolve(strict=True)
    except OSError as error:
        raise _InputError("invalid_output_path") from error
    if not _private_owned(directory, is_directory=True):
        raise _InputError("invalid_output_path")
    name = default_name if name_value is None else _text_arg(name_value, limit=128)
    if (
        name in {".", ".."}
        or Path(name).name != name
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.png", name, re.IGNORECASE)
    ):
        raise _InputError("invalid_output_path")
    output = directory / name
    if output.exists() or output.is_symlink():
        raise _InputError("output_already_exists")
    return output


def _staging_directory(staging_dir: Path | None) -> Path:
    if not isinstance(staging_dir, Path) or not staging_dir.is_absolute():
        raise _InputError("staging_directory_required")
    if staging_dir.is_symlink():
        raise _InputError("invalid_output_path")
    try:
        directory = staging_dir.resolve(strict=True)
    except OSError as error:
        raise _InputError("invalid_output_path") from error
    if not _private_owned(directory, is_directory=True):
        raise _InputError("invalid_output_path")
    return directory


def _private_owned(path: Path, *, is_directory: bool) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return False
    return stat.S_ISDIR(metadata.st_mode) if is_directory else stat.S_ISREG(metadata.st_mode)


def _build_call(
    provider: str,
    operation: str,
    arguments: Mapping[str, object],
    *,
    source_root: Path | None,
    staging_dir: Path | None,
) -> _CallSpec:
    if operation == "docs.search":
        _closed_arguments(arguments, {"query"}, {"query"})
        query = _text_arg(arguments["query"], limit=512)
        return _CallSpec(("docs", "search", query), "text")
    if operation == "docs.fetch":
        _closed_arguments(arguments, {"url"}, {"url"})
        url = _text_arg(arguments["url"], limit=2048)
        if not url.startswith("kb://") or any(char in url for char in "?#"):
            raise _InputError("invalid_arguments")
        return _CallSpec(("docs", "fetch", url), "text")
    if operation in {"preview.list", "preview.render"}:
        required = {"target"} | ({"fqn"} if operation == "preview.render" else set())
        _closed_arguments(arguments, required, required)
        target = _text_arg(arguments["target"], limit=256)
        if target.startswith("<") or not _TARGET.fullmatch(target):
            raise _InputError("unsupported_target")
        root = _source_root(source_root)
        if (root / ".git").is_file():
            raise _InputError("unsupported_build_launcher")
        reference = target if target.startswith(":") else ":" + target
        if operation == "preview.list":
            return _CallSpec(
                (
                    "preview",
                    "list",
                    "--all",
                    "--format=json",
                    reference,
                    "--",
                    "--configuration-cache",
                    "--build-cache",
                    "--parallel",
                ),
                "json",
                cwd=root,
            )
        fqn = _text_arg(arguments["fqn"], limit=512)
        if not _FQN.fullmatch(fqn):
            raise _InputError("invalid_arguments")
        output = _staging_directory(staging_dir)
        return _CallSpec(
            (
                "preview",
                "render",
                "--component-tree",
                "--format=json",
                "--fqn=" + fqn,
                "--output=" + str(output),
                reference,
                "--",
                "--configuration-cache",
                "--build-cache",
                "--parallel",
            ),
            "preview",
            artifact_dir=output,
            cwd=root,
            fqn=fqn,
        )

    if operation not in _DEVICE_OPERATIONS:
        raise _InputError("unsupported_operation")
    if provider != "mobile-native":
        raise _InputError("unsupported_operation")
    serial = _serial(arguments)

    if operation == "device.tree":
        _closed_arguments(arguments, {"device_serial"}, {"device_serial"})
        command = ("ui-dump", "--platform", "android", "--format", "xml", "--device", serial)
        return _CallSpec(command, "ui_tree")

    if operation in {"device.assert-visible", "device.assert-gone"}:
        _closed_arguments(
            arguments,
            {"device_serial", "text", "resource_id"},
            {"device_serial"},
        )
        field, value = _selector(arguments)
        command = ("ui-dump", "--platform", "android", "--format", "xml", "--device", serial)
        return _CallSpec(
            command,
            "assertion",
            assertion=(field, value, operation == "device.assert-visible"),
        )

    if operation == "device.launch":
        _closed_arguments(arguments, {"device_serial", "package"}, {"device_serial", "package"})
        package = _text_arg(arguments["package"], limit=255)
        if not _PACKAGE.fullmatch(package):
            raise _InputError("invalid_arguments")
        command = ("launch", "--platform", "android", "--device", serial, package)
        return _CallSpec(command, "status", effectful=True)

    if operation == "device.tap":
        _closed_arguments(
            arguments,
            {"device_serial", "text", "resource_id", "x", "y"},
            {"device_serial"},
        )
        coordinates = {"x", "y"} & set(arguments)
        if coordinates:
            if coordinates != {"x", "y"}:
                raise _InputError("invalid_arguments")
            if "text" in arguments or "resource_id" in arguments:
                raise _InputError("invalid_arguments")
            x, y = _coordinate(arguments["x"]), _coordinate(arguments["y"])
            command = ("tap", "--platform", "android", "--device", serial, str(x), str(y))
        else:
            field, value = _selector(arguments)
            flag = "--text" if field == "text" else "--resource-id"
            command = ("tap", "--platform", "android", "--device", serial, flag, value)
        return _CallSpec(command, "status", effectful=True)

    if operation == "device.key":
        _closed_arguments(arguments, {"device_serial", "key"}, {"device_serial", "key"})
        key = _text_arg(arguments["key"], limit=32).lower()
        if key not in _KEYS:
            raise _InputError("invalid_arguments")
        native_key = "KEYCODE_DPAD_CENTER" if key == "center" else key
        command = ("key", "--platform", "android", "--device", serial, native_key)
        return _CallSpec(command, "status", effectful=True)

    if operation == "device.swipe":
        _closed_arguments(
            arguments,
            {"device_serial", "x1", "y1", "x2", "y2", "duration_ms", "direction"},
            {"device_serial"},
        )
        direction = arguments.get("direction")
        if direction is not None:
            if not isinstance(direction, str) or direction not in _DIRECTIONS or any(
                key in arguments for key in {"x1", "y1", "x2", "y2", "duration_ms"}
            ):
                raise _InputError("invalid_arguments")
            command = (
                "swipe",
                "--platform",
                "android",
                "--device",
                serial,
                "--direction",
                direction,
            )
        else:
            point_keys = {"x1", "y1", "x2", "y2"}
            if not point_keys.issubset(arguments):
                raise _InputError("invalid_arguments")
            points = tuple(_coordinate(arguments[key]) for key in ("x1", "y1", "x2", "y2"))
            duration = _integer_range(arguments.get("duration_ms", 300), 50, 5000)
            command = (
                "swipe",
                "--platform",
                "android",
                "--device",
                serial,
                "--duration",
                str(duration),
                *(str(point) for point in points),
            )
        return _CallSpec(command, "status", effectful=True)

    if operation == "device.capture":
        _closed_arguments(
            arguments,
            {"device_serial", "output_name"},
            {"device_serial"},
        )
        output = _staging_output(
            staging_dir,
            arguments.get("output_name"),
            default_name="screen.png",
        )
        command = (
            "screenshot",
            "--platform",
            "android",
            "--device",
            serial,
            "--output",
            str(output),
        )
        return _CallSpec(command, "artifact", artifact_path=output)

    raise _InputError("unsupported_operation")


def _integer_range(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _InputError("invalid_arguments")
    return value


def _coordinate(value: object) -> int:
    return _integer_range(value, 0, 100_000)


@contextmanager
def _termination_guard() -> Iterator[None]:
    """Turn SIGTERM into an exception so the owned child process group is settled."""

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(_number: int, _frame: object) -> None:
        raise _TerminationRequested()

    signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _run_process(
    argv: tuple[str, ...],
    *,
    timeout_s: float,
    output_limit_bytes: int,
    cwd: Path | None = None,
) -> _ProcessOutput:
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
            umask=0o077,
            cwd=cwd,
        )
    except OSError:
        return _ProcessOutput(b"", None, "provider_unavailable")

    assert process.stdout is not None and process.stderr is not None
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    selector = selectors.DefaultSelector()
    failure: str | None = None
    interrupted: BaseException | None = None
    cleanup_deadline: float | None = None
    deadline = time.monotonic() + timeout_s
    try:
        with _termination_guard():
            for stream in streams:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "query_timeout"
                    break
                for key, _ in selector.select(min(0.05, remaining)):
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total = sum(len(value) for value in streams.values())
                    available = output_limit_bytes - total
                    buffer = streams[key.fileobj]
                    if len(chunk) > available:
                        if available > 0:
                            buffer.extend(chunk[:available])
                        failure = "observation_limit"
                        break
                    buffer.extend(chunk)
                if failure is not None:
                    break
            if failure is not None:
                cleanup_deadline = time.monotonic() + GROUP_CLEANUP_SECONDS
                settled = _settle_group(process, cleanup_deadline)
                drained = _drain_pipes(selector, cleanup_deadline)
                if not settled or not drained:
                    failure = "drain_unknown"
            else:
                process.wait()
                cleanup_deadline = time.monotonic() + GROUP_CLEANUP_SECONDS
                if not _settle_group(process, cleanup_deadline):
                    failure = "drain_unknown"
                    _drain_pipes(selector, cleanup_deadline)
    except BaseException as error:
        interrupted = error
        cleanup_deadline = time.monotonic() + GROUP_CLEANUP_SECONDS
        settled = _settle_group(process, cleanup_deadline)
        drained = _drain_pipes(selector, cleanup_deadline)
        if not settled or not drained:
            failure = "drain_unknown"
    finally:
        selector.close()
        for stream in streams:
            try:
                stream.close()
            except OSError:
                pass

    if interrupted is not None:
        if failure == "drain_unknown":
            return _ProcessOutput(b"", None, "drain_unknown")
        if isinstance(interrupted, _TerminationRequested):
            return _ProcessOutput(b"", None, "interrupted")
        raise interrupted
    if failure is not None:
        return _ProcessOutput(b"", None, failure)
    return _ProcessOutput(bytes(streams[process.stdout]), process.returncode)


def _group_state(process: subprocess.Popen[bytes], deadline: float) -> str:
    while True:
        process.poll()
        try:
            os.killpg(process.pid, 0)
            return "present"
        except ProcessLookupError:
            process.poll()
            return "absent"
        except PermissionError:
            # Darwin may report EPERM for a zombie-only group; reap, then require ESRCH.
            process.poll()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "unknown"
            time.sleep(min(0.005, remaining))
        except OSError:
            return "unknown"


def _signal_group(
    process: subprocess.Popen[bytes],
    number: int,
    deadline: float,
) -> str:
    try:
        os.killpg(process.pid, number)
        return "sent"
    except ProcessLookupError:
        process.poll()
        return "absent"
    except PermissionError:
        process.poll()
        state = _group_state(process, deadline)
        if state == "absent":
            return "absent"
        return "unknown"
    except OSError:
        return "unknown"


def _settle_group(process: subprocess.Popen[bytes], deadline: float) -> bool:
    state = _group_state(process, deadline)
    if state == "absent":
        try:
            process.wait(timeout=0)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return True
    if state == "unknown":
        return False
    if _signal_group(process, signal.SIGTERM, deadline) == "unknown":
        return False

    kill_at = min(deadline, time.monotonic() + GROUP_TERM_GRACE_SECONDS)
    kill_sent = False
    while time.monotonic() < deadline:
        state = _group_state(process, deadline)
        if state == "absent":
            try:
                process.wait(timeout=0)
            except (OSError, subprocess.TimeoutExpired):
                pass
            return True
        if state == "unknown":
            return False
        if not kill_sent and time.monotonic() >= kill_at:
            if _signal_group(process, signal.SIGKILL, deadline) == "unknown":
                return False
            kill_sent = True
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
    return _group_state(process, deadline) == "absent"


def _drain_pipes(selector: selectors.BaseSelector, deadline: float) -> bool:
    while selector.get_map() and time.monotonic() < deadline:
        timeout = min(0.02, max(0.0, deadline - time.monotonic()))
        for key, _ in selector.select(timeout):
            try:
                chunk = os.read(key.fd, 65536)
            except BlockingIOError:
                continue
            except OSError:
                return False
            if not chunk:
                selector.unregister(key.fileobj)
    return not selector.get_map()


def _interpret_output(
    spec: _CallSpec,
    stdout: bytes,
    output_limit: int,
) -> tuple[Any, list[dict[str, Any]], str | None]:
    if spec.output_kind == "preview":
        if spec.artifact_dir is None or spec.fqn is None:
            raise _InputError("invalid_provider_output")
        return _interpret_preview(stdout, spec.artifact_dir, spec.fqn, output_limit)
    if spec.output_kind == "artifact":
        if spec.artifact_path is None:
            raise _InputError("artifact_missing")
        return {"captured": True}, [_validate_png(spec.artifact_path, output_limit)], None
    if spec.output_kind == "status":
        return {"dispatched": True}, [], None
    if not stdout.strip():
        raise _InputError("missing_output")

    if spec.output_kind in {"ui_tree", "assertion"}:
        value, has_redacted_text = _validate_native_ui_xml(stdout, output_limit)
        if spec.output_kind == "assertion":
            if spec.assertion is None:
                raise _InputError("invalid_provider_output")
            field, selector, should_be_visible = spec.assertion
            count = _count_selector(value, field, selector)
            if field == "text" and has_redacted_text and count == 0:
                data = {
                    "selector": {"field": field, "value": _redact_text(selector)},
                    "state": "unknown",
                }
                return data, [], "assertion_failed"
            visible = count > 0
            data = {
                "selector": {"field": field, "value": _redact_text(selector)},
                "visible": visible,
                "match_count": count,
            }
            return data, [], None if visible == should_be_visible else "assertion_failed"
        return value, [], None

    if spec.output_kind == "json":
        try:
            value = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise _InputError("invalid_provider_output") from error
        value = _bounded_json(value)
        return _redact(value), [], None

    text = stdout.decode("utf-8", errors="replace").strip()
    if len(text.encode("utf-8")) > output_limit:
        raise _InputError("observation_limit")
    try:
        return _redact(_bounded_json(json.loads(text))), [], None
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return {"text": _redact_text(text)}, [], None


def _interpret_preview(
    stdout: bytes,
    staging_dir: Path,
    fqn: str,
    output_limit: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
    try:
        response = _bounded_json(json.loads(stdout))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise _InputError("invalid_provider_output") from error
    if not isinstance(response, dict) or response.get("success") is not True:
        raise _InputError("preview_failed")
    results = response.get("results")
    if not isinstance(results, list) or not results:
        raise _InputError("preview_result_missing")
    matching = [
        result for result in results
        if isinstance(result, dict) and result.get("methodFqn") == fqn
    ]
    if not matching or len(matching) > 16:
        raise _InputError("preview_result_missing")

    artifact_rows = []
    summaries = []
    total_bytes = 0
    for result in matching:
        preview_id = result.get("previewId")
        image_path = result.get("imagePath")
        tree_path = result.get("componentTreePath")
        if (
            not isinstance(preview_id, str)
            or not preview_id
            or len(preview_id) > 512
            or not isinstance(image_path, str)
            or not isinstance(tree_path, str)
        ):
            raise _InputError("invalid_provider_output")
        image = _stage_path(staging_dir, image_path, suffix=".png")
        tree = _stage_path(staging_dir, tree_path, suffix=".json")
        image_artifact = _validate_png(image, min(output_limit, MAX_ARTIFACT_BYTES))
        tree_artifact, tree_data = _validate_tree(tree, min(output_limit, MAX_ARTIFACT_BYTES))
        if tree_data.get("methodFQN") != fqn or tree_data.get("previewId") != preview_id:
            raise _InputError("invalid_provider_output")
        total_bytes += image_artifact["size_bytes"] + tree_artifact["size_bytes"]
        if total_bytes > output_limit:
            raise _InputError("observation_limit")
        artifact_rows.extend((image_artifact, tree_artifact))
        summaries.append({
            "preview_id": _redact_text(preview_id),
            "method_fqn": fqn,
            "image_path": str(image),
            "component_tree_path": str(tree),
        })
    return {"success": True, "results": summaries}, artifact_rows, None


def _stage_path(staging_dir: Path, raw_path: str, *, suffix: str) -> Path:
    if not raw_path or len(raw_path.encode("utf-8")) > 2048:
        raise _InputError("invalid_artifact_path")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = staging_dir / candidate
    try:
        lexical_relative = candidate.relative_to(staging_dir)
    except (OSError, ValueError) as error:
        raise _InputError("invalid_artifact_path") from error
    if not lexical_relative.parts or any(part in {".", ".."} for part in lexical_relative.parts):
        raise _InputError("invalid_artifact_path")
    cursor = staging_dir
    for index, part in enumerate(lexical_relative.parts):
        cursor = cursor / part
        if not _private_owned(cursor, is_directory=index < len(lexical_relative.parts) - 1):
            raise _InputError("invalid_artifact_path")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(staging_dir)
    except (OSError, ValueError) as error:
        raise _InputError("invalid_artifact_path") from error
    if resolved.suffix.lower() != suffix:
        raise _InputError("invalid_artifact_path")
    return resolved


def _validate_tree(path: Path, size_limit: int) -> tuple[dict[str, Any], dict[str, Any]]:
    content = _read_bounded_file(path, size_limit)
    try:
        value = _bounded_json(
            json.loads(content),
            max_depth=MAX_COMPONENT_TREE_DEPTH,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise _InputError("invalid_component_tree") from error
    if not isinstance(value, dict):
        raise _InputError("invalid_component_tree")
    required = {
        "components",
        "densityDpi",
        "deviceId",
        "fontScale",
        "height",
        "methodFQN",
        "previewId",
        "uiMode",
        "width",
    }
    if not required.issubset(value) or not isinstance(value["components"], list):
        raise _InputError("invalid_component_tree")
    for key in ("densityDpi", "height", "width"):
        if isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] <= 0:
            raise _InputError("invalid_component_tree")
    if isinstance(value["fontScale"], bool) or not isinstance(value["fontScale"], (int, float)):
        raise _InputError("invalid_component_tree")
    if not math.isfinite(float(value["fontScale"])) or value["fontScale"] <= 0:
        raise _InputError("invalid_component_tree")
    if not isinstance(value["deviceId"], str) or not isinstance(value["methodFQN"], str):
        raise _InputError("invalid_component_tree")
    if not isinstance(value["previewId"], str):
        raise _InputError("invalid_component_tree")
    if not isinstance(value["uiMode"], (str, int)):
        raise _InputError("invalid_component_tree")
    sanitized = _redact(value)
    safe_content = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(safe_content) > size_limit:
        raise _InputError("artifact_limit")
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "wb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077
            ):
                raise _InputError("invalid_artifact_path")
            stream.write(safe_content)
    except _InputError:
        raise
    except OSError as error:
        code = "invalid_artifact_path" if error.errno == errno.ELOOP else "artifact_missing"
        raise _InputError(code) from error
    artifact = {
        "path": str(path),
        "media_type": "application/json",
        "size_bytes": len(safe_content),
        "sha256": hashlib.sha256(safe_content).hexdigest(),
    }
    return artifact, sanitized


def _read_bounded_file(path: Path, size_limit: int) -> bytes:
    try:
        if path.is_symlink():
            raise _InputError("invalid_artifact_path")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077
            ):
                raise _InputError("invalid_artifact_path")
            if metadata.st_size <= 0 or metadata.st_size > size_limit:
                raise _InputError("artifact_limit")
            content = stream.read(size_limit + 1)
            if len(content) != metadata.st_size:
                raise _InputError("invalid_artifact")
            return content
    except _InputError:
        raise
    except OSError as error:
        code = "invalid_artifact_path" if error.errno == errno.ELOOP else "artifact_missing"
        raise _InputError(code) from error


def _validate_native_ui_xml(
    content: bytes,
    output_limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    if len(content) > output_limit:
        raise _InputError("observation_limit")
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise _InputError("invalid_provider_output")

    try:
        xml = content.decode("utf-8")
        parser = ET.XMLPullParser(events=("start", "end"))
        result: list[dict[str, Any]] = []
        stack: list[str] = []
        depth = 0
        node_count = 0
        root_seen = False
        has_redacted_text = False

        def consume_events() -> None:
            nonlocal depth, node_count, root_seen, has_redacted_text
            for event, element in parser.read_events():
                if event == "start":
                    if depth >= MAX_UI_XML_DEPTH:
                        raise _InputError("observation_limit")
                    if depth == 0:
                        if element.tag != "hierarchy" or root_seen:
                            raise _InputError("invalid_provider_output")
                        if set(element.attrib) - {"rotation"}:
                            raise _InputError("invalid_provider_output")
                        root_seen = True
                    else:
                        if element.tag != "node" or stack[-1] not in {"hierarchy", "node"}:
                            raise _InputError("invalid_provider_output")
                        attributes = element.attrib
                        required = {"class", "bounds", "clickable", "password"}
                        if required - set(attributes) or set(attributes) - _UI_XML_NODE_ATTRIBUTES:
                            raise _InputError("invalid_provider_output")
                        if any(
                            attributes.get(name) not in {"true", "false"}
                            for name in _UI_XML_BOOLEAN_ATTRIBUTES
                            if name in attributes
                        ):
                            raise _InputError("invalid_provider_output")
                        class_name = attributes["class"]
                        bounds = attributes["bounds"]
                        if not class_name or not _UI_BOUNDS.fullmatch(bounds):
                            raise _InputError("invalid_provider_output")
                        node: dict[str, Any] = {
                            "class": class_name,
                            "bounds": bounds,
                            "clickable": attributes["clickable"] == "true",
                            "password": attributes["password"] == "true",
                        }
                        if "NAF" in attributes:
                            node["naf"] = attributes["NAF"] == "true"
                        for xml_name, field in (
                            ("text", "text"),
                            ("resource-id", "resource_id"),
                            ("content-desc", "content_desc"),
                        ):
                            field_value = attributes.get(xml_name, "")
                            if field_value:
                                safe_value = _redact_text(field_value)
                                if safe_value != field_value:
                                    node[field] = "<redacted>"
                                    has_redacted_text = True
                                else:
                                    node[field] = safe_value
                        if node["password"] or _is_sensitive_ui_node(node):
                            for field in ("text", "content_desc"):
                                if field in node:
                                    node[field] = "<redacted>"
                                    has_redacted_text = True
                        node_count += 1
                        if node_count > MAX_JSON_NODES:
                            raise _InputError("observation_limit")
                        result.append(node)
                    stack.append(element.tag)
                    depth += 1
                else:
                    if not stack or stack[-1] != element.tag:
                        raise _InputError("invalid_provider_output")
                    if (element.text or "").strip() or (element.tail or "").strip():
                        raise _InputError("invalid_provider_output")
                    stack.pop()
                    depth -= 1
                    element.clear()

        for offset in range(0, len(xml), 4096):
            parser.feed(xml[offset : offset + 4096])
            consume_events()
        parser.close()
        consume_events()
    except _InputError:
        raise
    except (ET.ParseError, UnicodeDecodeError, RecursionError, ValueError) as error:
        raise _InputError("invalid_provider_output") from error

    if not root_seen or depth != 0 or stack:
        raise _InputError("invalid_provider_output")
    return result, has_redacted_text


def _is_sensitive_ui_node(node: Mapping[str, Any]) -> bool:
    class_name = node["class"].rsplit(".", 1)[-1].casefold()
    if any(
        marker in class_name
        for marker in ("edittext", "textinput", "textfield", "password", "secret", "credential")
    ):
        return True
    resource_id = node.get("resource_id", "")
    return isinstance(resource_id, str) and bool(
        re.search(r"password|passphrase|secret|credential|token", resource_id, re.IGNORECASE)
    )


def _count_selector(value: Any, field: str, expected: str) -> int:
    if field not in {"text", "resource_id"} or not isinstance(value, list):
        raise _InputError("invalid_provider_output")
    matches = 0
    for node in value:
        if not isinstance(node, dict):
            raise _InputError("invalid_provider_output")
        if field == "resource_id":
            matched = node.get("resource_id", "") == expected
        else:
            expected_value = expected.casefold()
            matched = any(
                isinstance(node.get(key), str)
                and node[key] != "<redacted>"
                and expected_value in node[key].casefold()
                for key in ("text", "content_desc")
            )
        if matched:
            matches += 1
    return min(matches, MAX_JSON_NODES)


def _bounded_json(value: Any, *, max_depth: int = MAX_JSON_DEPTH) -> Any:
    count = 0

    def visit(item: Any, depth: int) -> Any:
        nonlocal count
        count += 1
        if count > MAX_JSON_NODES or depth > max_depth:
            raise _InputError("observation_limit")
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                if isinstance(key, str):
                    result[key[:128]] = visit(child, depth + 1)
            return result
        if isinstance(item, list):
            return [visit(child, depth + 1) for child in item]
        if isinstance(item, str):
            if len(item.encode("utf-8")) > 4096:
                raise _InputError("observation_limit")
            return _redact_text(item)
        if isinstance(item, float) and not math.isfinite(item):
            raise _InputError("invalid_provider_output")
        if item is None or isinstance(item, (bool, int, float)):
            return item
        raise _InputError("invalid_provider_output")

    return visit(value, 0)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if _is_sensitive_key(key) else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    suffixes = (
        "authorization",
        "token",
        "apikey",
        "password",
        "passphrase",
        "secret",
        "cookie",
        "credential",
    )
    return normalized.endswith(suffixes)


def _redact_text(value: str) -> str:
    return _SECRET_TEXT.sub(r"\1\2<redacted>", value)


def _validate_png(path: Path, output_limit: int) -> dict[str, Any]:
    content = _read_bounded_file(path, min(output_limit, MAX_ARTIFACT_BYTES))
    if len(content) < 45 or not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise _InputError("invalid_png")
    offset = 8
    width = height = bit_depth = color_type = 0
    saw_header = saw_idat = saw_end = idat_ended = False
    compressed_data = bytearray()
    while offset + 12 <= len(content):
        length = struct.unpack(">I", content[offset:offset + 4])[0]
        kind = content[offset + 4:offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(content):
            raise _InputError("invalid_png")
        payload = content[offset + 8:offset + 8 + length]
        crc = struct.unpack(">I", content[offset + 8 + length:chunk_end])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc:
            raise _InputError("invalid_png")
        if not saw_header:
            if kind != b"IHDR" or length != 13:
                raise _InputError("invalid_png")
            width, height, bit_depth, color_type, compression, filter_method, interlace = (
                struct.unpack(">IIBBBBB", payload)
            )
            if (
                not 0 < width <= 16_384
                or not 0 < height <= 16_384
                or compression != 0
                or filter_method != 0
                or interlace != 0
            ):
                raise _InputError("invalid_png")
            if width * height > MAX_PNG_PIXELS:
                raise _InputError("artifact_limit")
            saw_header = True
        elif kind == b"IHDR":
            raise _InputError("invalid_png")
        if kind == b"IDAT":
            if idat_ended or not saw_header:
                raise _InputError("invalid_png")
            saw_idat = True
            compressed_data.extend(payload)
        elif saw_idat and kind != b"IEND":
            idat_ended = True
        if kind == b"IEND":
            if length != 0 or chunk_end != len(content):
                raise _InputError("invalid_png")
            saw_end = True
            break
        offset = chunk_end
    if not saw_header or not saw_idat or not saw_end:
        raise _InputError("invalid_png")
    channels_by_color = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    bit_depths_by_color = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8},
                           4: {8, 16}, 6: {8, 16}}
    channels = channels_by_color.get(color_type)
    if channels is None or bit_depth not in bit_depths_by_color[color_type]:
        raise _InputError("invalid_png")
    row_bytes = (width * channels * bit_depth + 7) // 8
    decoded_size = (row_bytes + 1) * height
    if decoded_size > MAX_PNG_DECOMPRESSED_BYTES:
        raise _InputError("artifact_limit")
    try:
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(bytes(compressed_data), decoded_size + 1)
    except zlib.error as error:
        raise _InputError("invalid_png") from error
    if (
        len(decoded) != decoded_size
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise _InputError("invalid_png")
    scanline_size = row_bytes + 1
    if any(decoded[row] > 4 for row in range(0, decoded_size, scanline_size)):
        raise _InputError("invalid_png")
    return {
        "path": str(path),
        "media_type": "image/png",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "width": width,
        "height": height,
    }
