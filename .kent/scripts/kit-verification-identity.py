#!/usr/bin/env python3
"""Calculate the bounded identity carried by a local verification report.

This module is deliberately a read-only freshness reader.  It does not run
the project validator, persist state, contact Kent, or decide whether a
report may be reused.  The compile child compares two closed identity
objects around its validator invocation and fails closed on uncertainty.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import selectors
import shutil
import stat
import subprocess
import sys
import time
from typing import Any


IDENTITY_SCHEMA = "kit-verification-identity-v1"
IDENTITY_KEYS = frozenset(
    {"schema", "source_sha256", "environment_sha256", "head", "workspace_path"}
)
MAX_SOURCE_ENTRIES = 20_000
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = MAX_SOURCE_BYTES
MAX_TOOL_BYTES = 32 * 1024 * 1024
MAX_VERSION_BYTES = 4096
VERSION_TIMEOUT_SECONDS = 10.0
CHUNK_BYTES = 1024 * 1024
MAX_SYMLINK_BYTES = 4096

PATH_TOOLS = ("bash", "sh", "git", "ruby", "jq")
OPTIONAL_ENVIRONMENT = (
    "HOME",
    "JAVA_HOME",
    "ANDROID_HOME",
    "ANDROID_SDK_ROOT",
    "GRADLE_USER_HOME",
    "GOROOT",
    "GOPATH",
    "GOMODCACHE",
    "GOCACHE",
    "XDG_CACHE_HOME",
)
FIXED_ENVIRONMENT = (
    "CI",
    "LANG",
    "LC_ALL",
    "GIT_TERMINAL_PROMPT",
    "GCM_INTERACTIVE",
    "PYTHONDONTWRITEBYTECODE",
)
WRAPPER_ENVIRONMENT_CONTRACT = {
    "schema": "workflow-verify-report-replacement-environment-v1",
    "fixed_values": {
        "CI": "1",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": "contained-private-directory",
    },
    "optional_values": list(OPTIONAL_ENVIRONMENT),
    "fixed_path_prefixes": [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/home/linuxbrew/.linuxbrew/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ],
}


class IdentityError(ValueError):
    """An identity cannot be safely calculated."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fs_text(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 10.0,
    check: bool = True,
) -> bytes:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise IdentityError("identity command could not be executed") from error
    if check and result.returncode != 0:
        raise IdentityError("identity command failed")
    return result.stdout


def _which(name: str, path: str) -> Path | None:
    candidate = shutil.which(name, path=path)
    if not candidate:
        return None
    try:
        resolved = Path(candidate).resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError, ValueError):
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or not os.access(resolved, os.X_OK)
        or metadata.st_mode & 0o022
    ):
        return None
    return resolved


def _hash_file(path: Path, *, limit: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise IdentityError("identity file-size bound exceeded")
                digest.update(chunk)
    except IdentityError:
        raise
    except OSError as error:
        raise IdentityError("identity source is unreadable") from error
    return digest.hexdigest(), total


def _git_path_inventory(workspace: Path, git: Path) -> list[bytes]:
    raw = _run(
        [
            str(git),
            "-C",
            str(workspace),
            "ls-files",
            "--cached",
            "--modified",
            "--deleted",
            "--others",
            "--exclude-standard",
            "--full-name",
            "-z",
        ],
    )
    entries = [item for item in raw.split(b"\0") if item]
    if len(entries) > MAX_SOURCE_ENTRIES:
        raise IdentityError("identity source-entry bound exceeded")
    return sorted(set(entries))


def _git_head(workspace: Path, git: Path) -> str:
    raw = _run(
        [str(git), "-C", str(workspace), "rev-parse", "--verify", "HEAD"],
    ).strip()
    try:
        head = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise IdentityError("identity HEAD is not ASCII") from error
    if len(head) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in head
    ):
        raise IdentityError("identity HEAD is unavailable")
    return head


def _index_modes(workspace: Path, git: Path) -> dict[bytes, int]:
    raw = _run(
        [str(git), "-C", str(workspace), "ls-files", "--stage", "-z"],
    )
    modes: dict[bytes, int] = {}
    for entry in (item for item in raw.split(b"\0") if item):
        header, separator, path = entry.partition(b"\t")
        if not separator:
            raise IdentityError("identity Git inventory is malformed")
        fields = header.split()
        if len(fields) != 3:
            raise IdentityError("identity Git index entry is malformed")
        try:
            mode = int(fields[0], 8)
        except ValueError as error:
            raise IdentityError("identity Git index mode is malformed") from error
        modes[path] = mode
    return modes


def _relative_path(workspace: Path, raw_path: bytes) -> Path:
    decoded = os.fsdecode(raw_path)
    candidate = workspace / Path(decoded)
    try:
        candidate.relative_to(workspace)
    except ValueError as error:
        raise IdentityError("identity source path escapes workspace") from error
    return candidate


def _source_record(
    workspace: Path,
    raw_path: bytes,
    *,
    index_mode: int | None,
    byte_count: list[int],
) -> dict[str, Any]:
    path = _relative_path(workspace, raw_path)
    encoded_path = _fs_text(raw_path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if index_mode is None:
            raise IdentityError("identity inventory contains an unknown deletion")
        return {
            "kind": "deleted",
            "mode": index_mode,
            "path": encoded_path,
        }
    except OSError as error:
        raise IdentityError("identity source metadata is unreadable") from error

    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISLNK(metadata.st_mode):
        try:
            target = os.fsencode(os.readlink(path))
        except OSError as error:
            raise IdentityError("identity symlink is unreadable") from error
        if len(target) > MAX_SYMLINK_BYTES:
            raise IdentityError("identity symlink bound exceeded")
        try:
            resolved_target = path.parent.joinpath(os.fsdecode(target)).resolve(
                strict=True
            )
            resolved_target.relative_to(workspace)
        except (OSError, RuntimeError, ValueError) as error:
            raise IdentityError("identity source symlink escapes workspace") from error
        return {
            "kind": "symlink",
            "mode": mode,
            "path": encoded_path,
            "target": _fs_text(target),
        }
    if not stat.S_ISREG(metadata.st_mode):
        raise IdentityError("identity source contains unsupported file type")

    file_digest, size = _hash_file(path, limit=MAX_FILE_BYTES)
    byte_count[0] += size
    if byte_count[0] > MAX_SOURCE_BYTES:
        raise IdentityError("identity source-byte bound exceeded")
    try:
        after = path.lstat()
    except OSError as error:
        raise IdentityError("identity source changed during read") from error
    if (
        after.st_dev != metadata.st_dev
        or after.st_ino != metadata.st_ino
        or after.st_size != metadata.st_size
        or stat.S_IMODE(after.st_mode) != mode
    ):
        raise IdentityError("identity source changed during read")
    return {
        "kind": "file",
        "mode": mode,
        "path": encoded_path,
        "sha256": file_digest,
        "size": size,
    }


def _source_identity(workspace: Path, git: Path, head: str) -> str:
    paths = _git_path_inventory(workspace, git)
    modes = _index_modes(workspace, git)
    records = []
    byte_count = [0]
    for raw_path in paths:
        records.append(
            _source_record(
                workspace,
                raw_path,
                index_mode=modes.get(raw_path),
                byte_count=byte_count,
            )
        )

    required = (
        ".kent/scripts/workflow-compile-verify",
        ".kent/scripts/workflow-verify-report",
        ".kent/scripts/workflow_runtime_contracts.py",
        ".kent/scripts/kit-verification-identity.py",
        "scripts/validate",
    )
    inventory = {os.fsdecode(path) for path in paths}
    missing = [name for name in required if name not in inventory]
    if missing:
        raise IdentityError("identity required verifier source is missing")

    payload = {
        "schema": IDENTITY_SCHEMA,
        "head": head,
        "inventory": records,
        "workspace_path": str(workspace),
    }
    return _digest(_canonical(payload))


def _safe_value_digest(value: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise IdentityError("identity environment contains invalid text") from error
    if len(encoded) > 4096 or b"\0" in encoded:
        raise IdentityError("identity environment value is unsupported")
    return _digest(encoded)


def _tmpdir_contract(workspace: Path) -> dict[str, str]:
    value = os.environ.get("TMPDIR")
    if not value:
        raise IdentityError("identity TMPDIR is unavailable")
    try:
        resolved = Path(value).expanduser().resolve(strict=True)
        metadata = resolved.stat()
        workflow_root = (workspace / "build/kent-workflow").resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise IdentityError("identity TMPDIR is unvalidated") from error
    if (
        workflow_root.is_relative_to(workspace)
        and resolved.is_relative_to(workflow_root)
        and resolved.name.startswith(".verify-tmp-")
        and stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700
    ):
        return {
            "state": "contained-private",
            "contract": "workspace/build/kent-workflow/private-0700",
        }
    raise IdentityError("identity TMPDIR is not a contained private directory")


def _tool_metadata(path: Path) -> tuple[int, ...]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or not os.access(path, os.X_OK)
        or metadata.st_mode & 0o022
    ):
        raise IdentityError("identity tool is unsafe")
    return (
        metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size,
        metadata.st_mtime_ns, metadata.st_ctime_ns, metadata.st_uid, metadata.st_gid,
    )


def _tool_version(path: Path) -> tuple[bytes, int]:
    """Bound output while reading, including when descendants retain the pipe."""
    deadline = time.monotonic() + VERSION_TIMEOUT_SECONDS
    output = bytearray()
    try:
        process = subprocess.Popen(
            [str(path), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise IdentityError("identity tool version timed out")
                    chunk = os.read(
                        process.stdout.fileno(), MAX_VERSION_BYTES + 1 - len(output)
                    )
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > MAX_VERSION_BYTES:
                        raise IdentityError("identity tool version output bound exceeded")
            returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
            process.wait()
    except (OSError, subprocess.SubprocessError) as error:
        raise IdentityError("identity tool version could not be read") from error
    return bytes(output), returncode


def _tool_record(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"state": "missing"}
    try:
        before = _tool_metadata(path)
        digest, size = _hash_file(path, limit=MAX_TOOL_BYTES)
        if _tool_metadata(path) != before:
            raise IdentityError("identity tool changed during hashing")
        output, returncode = _tool_version(path)
        if _tool_metadata(path) != before:
            raise IdentityError("identity tool changed during version probing")
        after_digest, after_size = _hash_file(path, limit=MAX_TOOL_BYTES)
        if (
            _tool_metadata(path) != before
            or (after_digest, after_size) != (digest, size)
        ):
            raise IdentityError("identity tool changed after version probing")
    except OSError as error:
        raise IdentityError("identity tool became unavailable") from error
    return {
        "path_sha256": _safe_value_digest(str(path)),
        "executable_sha256": digest,
        "executable_size": size,
        "mode": stat.S_IMODE(before[2]),
        "version_sha256": _digest(output),
        "version_returncode": returncode,
    }


def _environment_identity(workspace: Path, selected_python: Path) -> str:
    path_value = os.environ.get("PATH")
    if not path_value:
        raise IdentityError("identity PATH is unavailable")
    tools = {
        name: _tool_record(_which(name, path_value))
        for name in PATH_TOOLS
    }
    system_python = Path("/usr/bin/python3")
    if system_python.is_file() and os.access(system_python, os.X_OK):
        tools["/usr/bin/python3"] = _tool_record(system_python.resolve())
    tools["selected_python"] = _tool_record(selected_python)

    effective_environment: dict[str, Any] = {
        name: _safe_value_digest(os.environ[name])
        for name in FIXED_ENVIRONMENT
        if name in os.environ
    }
    effective_environment["PATH_sha256"] = _safe_value_digest(path_value)
    effective_environment["optional"] = {
        name: _safe_value_digest(os.environ[name])
        for name in OPTIONAL_ENVIRONMENT
        if name in os.environ
    }
    effective_environment["TMPDIR"] = _tmpdir_contract(workspace)
    wrapper_contract_digest = _digest(_canonical(WRAPPER_ENVIRONMENT_CONTRACT))
    payload = {
        "schema": IDENTITY_SCHEMA,
        "os": {
            "name": platform.system(),
            "release": platform.release(),
            "architecture": platform.machine(),
            "python_platform": sys.platform,
        },
        "effective_environment": effective_environment,
        "report_wrapper_contract_sha256": wrapper_contract_digest,
        "selected_python_sha256": _safe_value_digest(str(selected_python)),
        "tools": tools,
    }
    return _digest(_canonical(payload))


def calculate_identity(workspace: Path, selected_python: str | Path) -> dict[str, str]:
    """Return the closed identity object for the current checkout."""

    try:
        workspace = workspace.expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise IdentityError("identity workspace is unavailable") from error
    if not workspace.is_dir():
        raise IdentityError("identity workspace is not a directory")
    python_path = Path(selected_python).expanduser()
    try:
        python_path = python_path.resolve(strict=True)
        python_metadata = python_path.stat()
    except (OSError, RuntimeError, ValueError) as error:
        raise IdentityError("identity selected Python is unavailable") from error
    if (
        not stat.S_ISREG(python_metadata.st_mode)
        or not os.access(python_path, os.X_OK)
        or python_metadata.st_mode & 0o022
    ):
        raise IdentityError("identity selected Python is unsafe")
    path_value = os.environ.get("PATH", "")
    git = _which("git", path_value)
    if git is None:
        raise IdentityError("identity Git is unavailable")
    head = _git_head(workspace, git)
    return {
        "schema": IDENTITY_SCHEMA,
        "source_sha256": _source_identity(workspace, git, head),
        "environment_sha256": _environment_identity(workspace, python_path),
        "head": head,
        "workspace_path": str(workspace),
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--python-executable", required=True)
    return parser.parse_args()


def main() -> int:
    try:
        arguments = _arguments()
        identity = calculate_identity(
            Path(arguments.workspace),
            arguments.python_executable,
        )
        if set(identity) != IDENTITY_KEYS:
            raise IdentityError("identity object has an unexpected shape")
        print(json.dumps(identity, sort_keys=True, separators=(",", ":")))
        return 0
    except (IdentityError, OSError, ValueError) as error:
        print(f"Verification identity blocked: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
