"""Independent, identity-bound Markdown storage with immutable revisions."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, Callable, Iterator
import uuid


MAX_BYTES = 8 * 1024 * 1024
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
TOKEN = re.compile(r"[0-9a-f]{64}\Z")
CHECKBOX = re.compile(r"^(\s*(?:(?:[-*+]|\d+[.)])\s+|#{1,6}\s+)\[)[ xX](\])",
                      re.MULTILINE)


class MemoryError(ValueError):
    """Invalid, unsafe or incomplete project memory."""


class Conflict(MemoryError):
    """The expected identity or revision no longer matches."""


class MissingMemory(MemoryError):
    """An admitted storage component has not been created."""


@dataclass(frozen=True)
class Revision:
    token: str
    path: Path
    sha256: str


def _key(value: str) -> str:
    if not isinstance(value, str) or not KEY.fullmatch(value) or value in {".", ".."}:
        raise MemoryError("invalid memory key")
    return value


def _token(value: object) -> str:
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise MemoryError("invalid revision digest")
    return value


def _bytes(value: Any) -> bytes:
    try:
        result = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise MemoryError("invalid canonical JSON") from error
    if len(result) > MAX_BYTES:
        raise MemoryError("memory input exceeds size limit")
    return result


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise MemoryError("duplicate JSON key")
        result[key] = value
    return result


def _json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(
                               MemoryError("nonfinite JSON number")))
        _bytes(value)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise MemoryError("invalid memory JSON") from error
    if not isinstance(value, dict):
        raise MemoryError("memory record must be an object")
    return value


def _closed(value: dict[str, Any], keys: set[str], schema: str) -> None:
    if set(value) != keys or value.get("schema") != schema:
        raise MemoryError("unexpected memory record schema or fields")


def _normalized(text: str) -> str:
    text = CHECKBOX.sub(r"\1 \2", text.replace("\r\n", "\n").replace("\r", "\n"))
    return "\n".join(line.rstrip() for line in text.split("\n")).rstrip("\n") + "\n"


def _references(refs: Any, fields: set[str], *, digest_field: bool = False) -> None:
    if not isinstance(refs, list) or len(refs) > 64:
        raise MemoryError("invalid source references")
    seen: set[bytes] = set()
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != fields:
            raise MemoryError("invalid source reference")
        for field, value in ref.items():
            if field == "sha256" and digest_field:
                _token(value)
            else:
                _key(value)
        encoded = _bytes(ref)
        if encoded in seen:
            raise MemoryError("duplicate source reference")
        seen.add(encoded)


def _provenance(refs: Any, *, required: bool = False) -> list[str]:
    if (not isinstance(refs, list) or len(refs) > 64 or (required and not refs)
            or any(not isinstance(ref, str) or not ref or len(ref) > 4096 for ref in refs)):
        raise MemoryError("invalid provenance references")
    return refs


def _selection_digest(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not value:
        raise MemoryError("coder selection must be a nonempty object")
    return _digest(_bytes(value))


def _file_metadata(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or info.st_mode & 0o077
            or info.st_mode & 0o7000):
        raise MemoryError("unsafe private memory file")
    return info


def _read(fd: int, name: str) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        stream = os.open(name, flags, dir_fd=fd)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise MemoryError("unsafe memory file") from error
    try:
        before = _file_metadata(stream)
        if before.st_size > MAX_BYTES:
            raise MemoryError("memory file exceeds size limit")
        chunks: list[bytes] = []
        size = 0
        while block := os.read(stream, min(65536, MAX_BYTES + 1 - size)):
            chunks.append(block)
            size += len(block)
            if size > MAX_BYTES:
                raise MemoryError("memory file exceeds size limit")
        after = _file_metadata(stream)
        linked = os.stat(name, dir_fd=fd, follow_symlinks=False)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, f) != getattr(after, f) for f in fields):
            raise Conflict("memory file changed during read")
        if (after.st_dev, after.st_ino) != (linked.st_dev, linked.st_ino):
            raise Conflict("memory file link changed during read")
        return b"".join(chunks)
    finally:
        os.close(stream)


def _write(fd: int, name: str, raw: bytes, *, replace: bool = False) -> None:
    if len(raw) > MAX_BYTES:
        raise MemoryError("memory file exceeds size limit")
    # Callers hold the owning document/task lock, including immutable writes.
    # A crash must leave an orphan temporary file, never a partial published record.
    if not replace:
        try:
            os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(name)
    temporary = ".publish-" + uuid.uuid4().hex
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    stream = os.open(temporary, flags, 0o600, dir_fd=fd)
    try:
        _file_metadata(stream)
        remaining = memoryview(raw)
        while remaining:
            written = os.write(stream, remaining)
            if not written:
                raise OSError("memory write made no progress")
            remaining = remaining[written:]
        os.fsync(stream)
    finally:
        os.close(stream)
    os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
    os.fsync(fd)


@contextmanager
def _lock(fd: int, *, create: bool = True) -> Iterator[None]:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    if not create:
        stream = os.open(".lock", flags, dir_fd=fd)
    else:
        try:
            stream = os.open(".lock", flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
            os.fsync(fd)
        except FileExistsError:
            stream = os.open(".lock", flags, dir_fd=fd)
    try:
        before = _file_metadata(stream)
        fcntl.flock(stream, fcntl.LOCK_EX)
        linked = os.stat(".lock", dir_fd=fd, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (linked.st_dev, linked.st_ino):
            raise Conflict("memory lock changed")
        yield
    finally:
        os.close(stream)


class ProjectMemory:
    def __init__(self, root: Path, project_key: str, project_uid: str):
        self.root = root.expanduser().absolute()
        if ".." in self.root.parts or self.root == Path("/"):
            raise MemoryError("invalid memory root")
        self.project_key = _key(project_key)
        self.project_uid = _key(project_uid)
        self.path = self.root / project_key

    @contextmanager
    def _directory(self, relative: str = "", *, create: bool = False,
                   include_project: bool = True) -> Iterator[int]:
        parts = self.root.parts[1:]
        tail = ([self.project_key] if include_project else []) + (
            relative.split("/") if relative else [])
        for part in tail:
            _key(part)
        chain: list[tuple[int, str, int]] = []
        descriptors = [os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)]
        try:
            for index, name in enumerate([*parts, *tail]):
                parent = descriptors[-1]
                private = index >= len(parts) - 1
                if create and private:
                    try:
                        os.mkdir(name, 0o700, dir_fd=parent)
                        os.fsync(parent)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY |
                                    os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                except FileNotFoundError as error:
                    raise MissingMemory("memory directory is absent") from error
                except OSError as error:
                    raise MemoryError("missing or unsafe memory directory") from error
                descriptors.append(child)
                info = os.fstat(child)
                if private and (info.st_uid != os.getuid() or info.st_mode & 0o077
                                or info.st_mode & 0o7000):
                    raise MemoryError("memory directory must be private and owned")
                chain.append((parent, name, child))
            yield descriptors[-1]
            for parent, name, child in chain:
                linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
                info = os.fstat(child)
                if (linked.st_dev, linked.st_ino) != (info.st_dev, info.st_ino):
                    raise Conflict("memory directory changed during operation")
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @classmethod
    def initialize(cls, root: Path, project_key: str, project_uid: str) -> ProjectMemory:
        store = cls(root, project_key, project_uid)
        expected = {"schema": "r2-project-v1", "project_uid": project_uid,
                    "project_key": project_key}
        with store._directory(create=True) as fd, _lock(fd):
            try:
                actual = _json(_read(fd, "project.json"))
            except FileNotFoundError:
                _write(fd, "project.json", _bytes(expected))
            else:
                if actual != expected:
                    raise Conflict("project identity differs")
            for name in ("knowledge", "tasks"):
                with store._directory(name, create=True):
                    pass
        return store

    def _project(self) -> None:
        with self._directory() as fd:
            value = _json(_read(fd, "project.json"))
        if value != {"schema": "r2-project-v1", "project_uid": self.project_uid,
                     "project_key": self.project_key}:
            raise Conflict("project identity differs")

    def initialize_task(self, task_key: str, task_uid: str | None,
                        external_refs: list[dict[str, str]], *,
                        source_refs: list[dict[str, str]] | None = None) -> dict[str, Any]:
        self._project()
        _key(task_key)
        if task_uid is not None:
            _key(task_uid)
        _references(external_refs, {"namespace", "project_id", "task_id"})
        expected = {"schema": "r2-task-v1", "project_uid": self.project_uid,
                    "task_key": task_key, "task_uid": task_uid,
                    "external_refs": external_refs}
        if source_refs is not None:
            _references(source_refs, {"project_uid", "task_uid", "sha256"}, digest_field=True)
            expected["source_refs"] = source_refs
        with self._directory(f"tasks/{task_key}", create=True) as fd, _lock(fd):
            try:
                _read(fd, "intake.json")
            except FileNotFoundError:
                pass
            else:
                raise Conflict("pre-native intake identity cannot be rebound")
            try:
                actual = _json(_read(fd, "task.json"))
            except FileNotFoundError:
                if task_uid is None:
                    expected["task_uid"] = str(uuid.uuid4())
                _write(fd, "task.json", _bytes(expected))
            else:
                if task_uid is None:
                    expected["task_uid"] = _key(actual.get("task_uid"))
                if actual != expected:
                    raise Conflict("task identity differs")
        return expected

    def initialize_intake(self, invocation_id: str, intent: dict[str, Any], *,
                          phase_hook: Callable[[str], None] | None = None
                          ) -> dict[str, Any]:
        """Create or exactly resume a standalone, pre-native task identity.

        The caller supplies a random UUIDv4 before initialization and reuses it
        only for retries of the same intent. Intent contents are hashed, never
        interpreted as authority or used to discover an existing task.
        """
        self._project()
        try:
            parsed = uuid.UUID(invocation_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise MemoryError("intake invocation ID must be a canonical UUIDv4") from error
        if str(parsed) != invocation_id or parsed.version != 4:
            raise MemoryError("intake invocation ID must be a canonical UUIDv4")
        if not isinstance(intent, dict):
            raise MemoryError("intake intent must be an object")
        intent_sha256 = _digest(_bytes(intent))
        task_key = f"intake-{invocation_id}"
        task = {"schema": "r2-task-v1", "project_uid": self.project_uid,
                "task_key": task_key, "task_uid": invocation_id, "external_refs": []}
        marker = {"schema": "r2-intake-v1", "project_uid": self.project_uid,
                  "task_key": task_key, "task_uid": invocation_id,
                  "invocation_id": invocation_id, "intent_sha256": intent_sha256}
        with self._directory(f"tasks/{task_key}", create=True) as fd, _lock(fd):
            marker_raw = _bytes(marker)
            task_raw = _bytes(task)
            try:
                actual_marker = _json(_read(fd, "intake.json"))
            except FileNotFoundError:
                actual_marker = None
            if actual_marker is not None:
                _closed(actual_marker, set(marker), "r2-intake-v1")
                if actual_marker != marker:
                    raise Conflict("intake invocation or intent differs")
            try:
                actual_task = _json(_read(fd, "task.json"))
            except FileNotFoundError:
                actual_task = None
            if actual_marker is None and actual_task is not None:
                raise Conflict("intake directory already has a non-intake task identity")
            if actual_task is not None and actual_task != task:
                raise Conflict("intake task identity differs")

            def validate_interrupted_entries(allowed: set[str]) -> None:
                for name in os.listdir(fd):
                    if name in allowed:
                        continue
                    if not re.fullmatch(r"\.publish-[0-9a-f]{32}", name):
                        raise Conflict("incomplete intake directory has unknown contents")
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or info.st_uid != os.getuid() or info.st_mode & 0o077
                            or info.st_mode & 0o7000 or info.st_size > MAX_BYTES):
                        raise Conflict("incomplete intake directory has unsafe temporary")

            if actual_marker is None:
                # An interrupted pre-marker attempt may leave only the lock and
                # atomic-write temporaries. Unknown contents are never adopted.
                validate_interrupted_entries({".lock"})
                _write(fd, "intake.json", marker_raw)
                if phase_hook:
                    phase_hook("intake_durable")
            if actual_task is None:
                if actual_marker is not None:
                    validate_interrupted_entries({".lock", "intake.json"})
                _write(fd, "task.json", task_raw)
                if phase_hook:
                    phase_hook("task_durable")
            else:
                # Validate through the same strict reader used by every later
                # document operation before treating an exact retry as success.
                self._task(task_key)
        with self._directory(f"tasks/{task_key}") as fd:
            binding_sha256 = _digest(_read(fd, "task.json"))
        return {"task_key": task_key, "task_uid": invocation_id,
                "project_uid": self.project_uid, "external_refs": [],
                "invocation_id": invocation_id, "intent_sha256": intent_sha256,
                "task_binding_sha256": binding_sha256}

    def _task(self, key: str) -> dict[str, Any]:
        with self._directory(f"tasks/{_key(key)}") as fd:
            value = _json(_read(fd, "task.json"))
        keys = {"schema", "project_uid", "task_uid", "task_key", "external_refs"}
        if "source_refs" in value:
            keys.add("source_refs")
        _closed(value, keys, "r2-task-v1")
        if value["project_uid"] != self.project_uid or value["task_key"] != key:
            raise Conflict("task identity differs")
        _key(value["task_uid"])
        _references(value["external_refs"], {"namespace", "project_id", "task_id"})
        if "source_refs" in value:
            _references(value["source_refs"], {"project_uid", "task_uid", "sha256"},
                        digest_field=True)
        return value

    def _document(self, key: str) -> str:
        self._project()
        if not isinstance(key, str):
            raise MemoryError("document key must be a string")
        parts = key.split("/")
        for part in parts:
            _key(part)
        if len(parts) == 2 and parts[0] == "knowledge":
            return key
        if len(parts) == 3 and parts[0] == "tasks":
            self._task(parts[1])
            return key
        raise MemoryError("document is outside admitted knowledge or task")

    def _head(self, fd: int, name: str = "head.json") -> str | None:
        try:
            value = _json(_read(fd, name))
        except FileNotFoundError:
            return None
        _closed(value, {"schema", "revision"}, "r2-head-v1")
        return _token(value["revision"])

    def _revision(self, key: str, token: str) -> Revision:
        _token(token)
        relative = f"{key}/revisions/{token}"
        with self._directory(relative) as fd:
            metadata_raw = _read(fd, "revision.json")
            metadata = _json(metadata_raw)
            _closed(metadata, {"schema", "project_uid", "document_key", "parent",
                               "content_sha256", "operation_id", "provenance"},
                    "r2-revision-v1")
            if (_digest(metadata_raw) != token or metadata["project_uid"] != self.project_uid
                    or metadata["document_key"] != key):
                raise MemoryError("revision identity or hash mismatch")
            if metadata["parent"] is not None:
                _token(metadata["parent"])
            _key(metadata["operation_id"])
            _provenance(metadata["provenance"])
            raw = _read(fd, "content.md")
            try:
                raw.decode("utf-8")
            except UnicodeError as error:
                raise MemoryError("invalid Markdown encoding") from error
            if _digest(raw) != metadata["content_sha256"]:
                raise MemoryError("revision content hash mismatch")
        return Revision(token, self.path / relative / "content.md", _digest(raw))

    def read(self, document_key: str) -> Revision:
        key = self._document(document_key)
        with self._directory(key) as fd:
            token = self._head(fd)
        if token is None:
            raise MemoryError("document has no published revision")
        return self._revision(key, token)

    def publish(self, document_key: str, expected_revision: str | None, content: str,
                *, operation_id: str | None = None,
                provenance: list[str] | None = None,
                phase_hook: Callable[[str], None] | None = None) -> Revision:
        key = self._document(document_key)
        try:
            raw = content.encode("utf-8")
        except (AttributeError, UnicodeError) as error:
            raise MemoryError("content must be valid UTF-8 text") from error
        if len(raw) > MAX_BYTES:
            raise MemoryError("document exceeds size limit")
        if expected_revision is not None:
            _token(expected_revision)
        operation_id = _key(operation_id if operation_id is not None else uuid.uuid4().hex)
        provenance = _provenance(provenance if provenance is not None else [])
        metadata = {"schema": "r2-revision-v1", "project_uid": self.project_uid,
                    "document_key": key, "parent": expected_revision,
                    "content_sha256": _digest(raw), "operation_id": operation_id,
                    "provenance": provenance}
        encoded = _bytes(metadata)
        token = _digest(encoded)
        with self._directory(key, create=True) as fd, _lock(fd):
            current = self._head(fd)
            if current == token:
                return self._revision(key, token)
            if current != expected_revision:
                raise Conflict("working revision changed")
            if current:
                self._revision(key, current)
            with self._directory(f"{key}/revisions/{token}", create=True) as revision_fd:
                for name, expected in (("content.md", raw), ("revision.json", encoded)):
                    try:
                        _write(revision_fd, name, expected)
                    except FileExistsError:
                        if _read(revision_fd, name) != expected:
                            raise Conflict("prepared revision differs from exact request")
            if phase_hook:
                phase_hook("revision_durable")
            _write(fd, "head.json", _bytes({"schema": "r2-head-v1", "revision": token}),
                   replace=True)
            if phase_hook:
                phase_hook("head_published")
            return self._revision(key, token)

    def accept_plan(self, task_key: str, expected_revision: str,
                    expected_accepted: str | None, metadata: dict[str, Any]) -> Revision:
        key = self._document(f"tasks/{task_key}/plan")
        task = self._task(task_key)
        if (not isinstance(metadata, dict)
                or not {"work_kind", "authority_refs"} <= set(metadata)
                or set(metadata) - {"work_kind", "authority_refs", "coder_selection", "plan_kind"}):
            raise MemoryError("invalid acceptance metadata")
        plan_kind = metadata.get("plan_kind", "markdown")
        if plan_kind not in ("markdown", "report_only"):
            raise MemoryError("invalid plan kind")
        _key(metadata["work_kind"])
        _provenance(metadata["authority_refs"], required=True)
        selection = metadata.get("coder_selection")
        selection_digest = _selection_digest(selection)
        if expected_accepted is not None:
            _token(expected_accepted)
        with self._directory(key) as fd, _lock(fd):
            if self._head(fd) != expected_revision:
                raise Conflict("working plan changed before acceptance")
            with self._directory(f"tasks/{task_key}") as task_fd:
                current_accepted = self._head(task_fd, "accepted-head.json")
                revision = self._revision(key, expected_revision)
                with self._directory(f"{key}/revisions/{revision.token}") as revision_fd:
                    normalized = _normalized(_read(revision_fd, "content.md").decode("utf-8"))
                if plan_kind == "report_only" and (
                        normalized != "not-applicable\n" or selection is not None):
                    raise MemoryError("report-only contract must use its unselected sentinel")
                snapshot = {"schema": "r2-accepted-plan-v1", "project_uid": self.project_uid,
                            "task_uid": task["task_uid"], "task_binding_sha256": _digest(_bytes(task)),
                            "revision_token": revision.token, "raw_sha256": revision.sha256,
                            "normalized_plan": normalized,
                            "normalized_sha256": _digest(normalized.encode()),
                            "parent": expected_accepted,
                            "coder_selection": selection,
                            "coder_selection_sha256": selection_digest,
                            "plan_kind": plan_kind,
                            **metadata}
                encoded = _bytes(snapshot)
                token = _digest(encoded)
                if current_accepted not in (expected_accepted, token):
                    raise Conflict("accepted plan changed")
                with self._directory(f"tasks/{task_key}/accepted", create=True) as accepted_fd:
                    if current_accepted == token:
                        if _read(accepted_fd, token + ".json") != encoded:
                            raise Conflict("accepted snapshot conflict")
                        return Revision(token, self.path / f"tasks/{task_key}/accepted/{token}.json", token)
                    try:
                        _write(accepted_fd, token + ".json", encoded)
                    except FileExistsError:
                        if _read(accepted_fd, token + ".json") != encoded:
                            raise Conflict("accepted snapshot conflict")
                _write(task_fd, "accepted-head.json",
                       _bytes({"schema": "r2-head-v1", "revision": token}), replace=True)
        return Revision(token, self.path / f"tasks/{task_key}/accepted/{token}.json", token)

    def accepted_head(self, task_key: str) -> str | None:
        """Read an acceptance CAS token without creating a cache or accepting a plan."""
        self._project()
        self._task(task_key)
        with self._directory(f"tasks/{task_key}") as task_fd:
            return self._head(task_fd, "accepted-head.json")

    def plan_revision(self, task_key: str, path: str) -> Revision:
        key = self._document(f"tasks/{task_key}/plan")
        if not isinstance(path, str):
            raise MemoryError("plan locator must be an absolute revision path")
        prefix = self.path / key / "revisions"
        candidate = Path(path)
        if (not candidate.is_absolute() or candidate.name != "content.md"
                or candidate.parent.parent != prefix
                or str(candidate) != path):
            raise MemoryError("plan locator is outside task revisions")
        return self._revision(key, candidate.parent.name)

    def check_plan(self, task_key: str, *, coder_selection: dict[str, Any] | None = None
                   ) -> dict[str, Any]:
        selection_digest = _selection_digest(coder_selection)
        key = self._document(f"tasks/{task_key}/plan")
        task = self._task(task_key)
        with self._directory(key) as fd, _lock(fd):
            current = self._head(fd)
            if current is None:
                raise MemoryError("plan has no working revision")
            working = self._revision(key, current)
            with self._directory(f"tasks/{task_key}") as task_fd:
                accepted = self._head(task_fd, "accepted-head.json")
            if accepted is None:
                raise MemoryError("plan has no accepted snapshot")
            with self._directory(f"tasks/{task_key}/accepted") as accepted_fd:
                raw = _read(accepted_fd, accepted + ".json")
            snapshot = _json(raw)
            _closed(snapshot, {"schema", "project_uid", "task_uid", "task_binding_sha256",
                               "revision_token", "raw_sha256", "normalized_plan",
                               "normalized_sha256", "work_kind", "authority_refs", "parent",
                               "coder_selection", "coder_selection_sha256", "plan_kind"},
                    "r2-accepted-plan-v1")
            if (_digest(raw) != accepted
                    or snapshot.get("task_binding_sha256") != _digest(_bytes(task))
                    or snapshot.get("task_uid") != task["task_uid"]
                    or snapshot.get("project_uid") != self.project_uid):
                raise MemoryError("accepted snapshot identity or hash mismatch")
            _key(snapshot["work_kind"])
            if (snapshot["plan_kind"] not in ("markdown", "report_only")
                    or snapshot["plan_kind"] == "report_only" and (
                        snapshot["normalized_plan"] != "not-applicable\n"
                        or snapshot["coder_selection"] is not None)):
                raise MemoryError("invalid accepted plan kind")
            if snapshot["parent"] is not None:
                _token(snapshot["parent"])
            _provenance(snapshot["authority_refs"], required=True)
            if _selection_digest(snapshot["coder_selection"]) != snapshot["coder_selection_sha256"]:
                raise MemoryError("accepted coder selection mismatch")
            historical = self._revision(key, snapshot["revision_token"])
            with self._directory(f"{key}/revisions/{historical.token}") as history_fd:
                historical_normalized = _normalized(_read(history_fd, "content.md").decode())
            if (snapshot["raw_sha256"] != historical.sha256
                    or snapshot["normalized_plan"] != historical_normalized
                    or snapshot["normalized_sha256"] != _digest(historical_normalized.encode())):
                raise MemoryError("accepted snapshot content mismatch")
            with self._directory(f"{key}/revisions/{current}") as revision_fd:
                normalized = _normalized(_read(revision_fd, "content.md").decode())
            return {"comparison": "unchanged" if _digest(normalized.encode()) ==
                    snapshot["normalized_sha256"] and selection_digest ==
                    snapshot["coder_selection_sha256"] else "changed",
                    "working_revision": current, "working_path": str(working.path),
                    "accepted_snapshot": accepted,
                    "accepted_path": str(self.path / f"tasks/{task_key}/accepted/{accepted}.json"),
                    "accepted": snapshot}

    def _put_object(self, task_key: str, raw: bytes, namespace: str) -> Revision:
        self._project()
        self._task(task_key)
        if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
            raise MemoryError("artifact must be bounded bytes")
        digest = _digest(raw)
        relative = f"tasks/{task_key}/artifacts/{namespace}/objects"
        with self._directory(relative, create=True) as fd, _lock(fd):
            try:
                existing = _read(fd, digest)
            except FileNotFoundError:
                _write(fd, digest, raw)
            else:
                if existing != raw:
                    raise Conflict("immutable artifact conflict")
        return Revision(digest, self.path / relative / digest, digest)

    def put_artifact(self, task_key: str, raw: bytes) -> Revision:
        """Publish an immutable MCP object, preserving the existing wire path."""
        return self._put_object(task_key, raw, "mcp")

    def put_attachment(self, task_key: str, raw: bytes) -> Revision:
        """Publish bounded immutable task attachment bytes by content digest."""
        return self._put_object(task_key, raw, "attachments")

    def _read_object(self, task_key: str, digest: str, namespace: str
                     ) -> tuple[Revision, bytes]:
        self._project()
        self._task(task_key)
        _token(digest)
        relative = f"tasks/{task_key}/artifacts/{namespace}/objects"
        try:
            with self._directory(relative) as fd:
                raw = _read(fd, digest)
        except (FileNotFoundError, MissingMemory) as error:
            raise MemoryError("referenced artifact is missing") from error
        if _digest(raw) != digest:
            raise MemoryError("artifact digest mismatch")
        return Revision(digest, self.path / relative / digest, digest), raw

    def read_attachment(self, task_key: str, digest: str) -> bytes:
        """Read exact bytes for one digest in this task's fixed attachment namespace."""
        return self._read_object(task_key, digest, "attachments")[1]

    def put_invocation_artifact(self, task_key: str, invocation_id: str,
                                owner_token: str, raw: bytes) -> Revision:
        _token(owner_token)
        relative = self._invocation(task_key, invocation_id)
        with self._directory(relative) as fd, _lock(fd):
            self._active_owner(fd, task_key, invocation_id, owner_token)
            return self.put_artifact(task_key, raw)

    def _active_owner(self, fd: int, task_key: str, invocation_id: str,
                      owner_token: str) -> dict[str, Any]:
        current = self._read_invocation(fd, task_key, invocation_id)
        if (current["state"] != "unknown"
                or current["started"]["owner_sha256"] != _digest(owner_token.encode())):
            raise Conflict("publication requires active invocation owner")
        return current["started"]

    def read_cache(self, task_key: str, cache_key: str) -> dict[str, str]:
        self._project()
        task = self._task(task_key)
        _token(cache_key)
        try:
            with self._directory(f"tasks/{task_key}/artifacts/mcp/cache") as fd:
                raw = _read(fd, cache_key + ".json")
        except (MissingMemory, FileNotFoundError):
            return {"state": "miss"}
        head = _json(raw)
        _closed(head, {"schema", "project_uid", "task_uid", "cache_key",
                       "object_sha256"}, "r2-mcp-cache-v1")
        if (head["project_uid"] != self.project_uid or head["task_uid"] != task["task_uid"]
                or head["cache_key"] != cache_key):
            raise Conflict("cache identity mismatch")
        artifact = self._artifact(task_key, head["object_sha256"])
        return {"state": "hit", "object_sha256": artifact.sha256,
                "object_path": str(artifact.path)}

    def publish_cache(self, task_key: str, cache_key: str, object_sha256: str,
                      invocation_id: str, owner_token: str) -> dict[str, str]:
        _token(cache_key)
        _token(owner_token)
        relative = self._invocation(task_key, invocation_id)
        with self._directory(relative) as invocation_fd, _lock(invocation_fd):
            self._active_owner(invocation_fd, task_key, invocation_id, owner_token)
            artifact = self._artifact(task_key, object_sha256)
            head = {"schema": "r2-mcp-cache-v1", "project_uid": self.project_uid,
                    "task_uid": self._task(task_key)["task_uid"], "cache_key": cache_key,
                    "object_sha256": artifact.sha256}
            with self._directory(f"tasks/{task_key}/artifacts/mcp/cache", create=True) as fd, _lock(fd):
                _write(fd, cache_key + ".json", _bytes(head), replace=True)
        return {"state": "published"}

    def _artifact(self, task_key: str, digest: str) -> Revision:
        return self._read_object(task_key, digest, "mcp")[0]

    def _invocation(self, task_key: str, invocation_id: str) -> str:
        self._project()
        self._task(task_key)
        try:
            parsed = uuid.UUID(invocation_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise MemoryError("invocation ID must be a canonical UUID") from error
        if str(parsed) != invocation_id:
            raise MemoryError("invocation ID must be a canonical UUID")
        return f"tasks/{task_key}/artifacts/mcp/calls/{invocation_id}"

    def new_invocation(self, task_key: str) -> dict[str, str]:
        invocation = str(uuid.uuid4())
        relative = self._invocation(task_key, invocation)
        return {"invocation_id": invocation,
                "started_path": str(self.path / relative / "started.json"),
                "finished_path": str(self.path / relative / "finished.json")}

    def _read_invocation(self, fd: int, task_key: str, invocation_id: str) -> dict[str, Any]:
        try:
            started = _json(_read(fd, "started.json"))
        except FileNotFoundError:
            try:
                _read(fd, "finished.json")
            except FileNotFoundError:
                return {"state": "not-started"}
            raise MemoryError("finished receipt lacks original start")
        _closed(started, {"schema", "project_uid", "task_uid", "invocation_id",
                          "request_sha256", "owner_sha256", "permitted_metadata"},
                "r2-invocation-start-v1")
        self._validate_metadata(started["permitted_metadata"])
        task = self._task(task_key)
        if (started["project_uid"] != self.project_uid or started["task_uid"] != task["task_uid"]
                or started["invocation_id"] != invocation_id):
            raise Conflict("invocation identity differs")
        _token(started["request_sha256"])
        _token(started["owner_sha256"])
        try:
            finished = _json(_read(fd, "finished.json"))
        except FileNotFoundError:
            return {"state": "unknown", "started": started}
        _closed(finished, {"schema", "started_sha256", "exit_code", "error_code",
                           "object_sha256"}, "r2-invocation-finish-v1")
        if finished["started_sha256"] != _digest(_bytes(started)):
            raise MemoryError("finished receipt does not match start")
        self._validate_outcome(finished["exit_code"], finished["error_code"])
        if finished["object_sha256"] is not None:
            self._artifact(task_key, finished["object_sha256"])
        return {"state": "finished", "started": started, "finished": finished}

    @staticmethod
    def _validate_outcome(exit_code: int, error_code: str) -> None:
        if type(exit_code) is not int or not 0 <= exit_code <= 255:
            raise MemoryError("invalid invocation exit code")
        if not isinstance(error_code, str) or (error_code and not KEY.fullmatch(error_code)):
            raise MemoryError("invalid safe error code")

    @staticmethod
    def _validate_metadata(metadata: dict[str, Any]) -> None:
        if not isinstance(metadata, dict):
            raise MemoryError("invalid permitted metadata")
        if not metadata:
            return
        if set(metadata) != {"operation", "server", "tool", "config_sha256",
                             "output_mode", "safe_mode", "save_raw"}:
            raise MemoryError("invalid permitted metadata fields")
        if metadata["operation"] not in ("list", "call"):
            raise MemoryError("invalid MCP operation")
        for name in ("server", "tool", "output_mode"):
            value = metadata[name]
            if not isinstance(value, str) or len(value) > 128 or (
                    value and not KEY.fullmatch(value)):
                raise MemoryError("invalid permitted metadata value")
        _token(metadata["config_sha256"])
        if any(type(metadata[name]) is not bool for name in ("safe_mode", "save_raw")):
            raise MemoryError("invalid permitted metadata flag")

    def begin_invocation(self, task_key: str, invocation_id: str,
                         request_sha256: str, *,
                         permitted_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        relative = self._invocation(task_key, invocation_id)
        _token(request_sha256)
        if permitted_metadata is not None and not isinstance(permitted_metadata, dict):
            raise MemoryError("invalid permitted metadata")
        metadata = {} if permitted_metadata is None else dict(permitted_metadata)
        self._validate_metadata(metadata)
        with self._directory(relative, create=True) as fd, _lock(fd):
            existing = self._read_invocation(fd, task_key, invocation_id)
            if existing["state"] != "not-started":
                if (existing["started"]["request_sha256"] != request_sha256
                        or existing["started"]["permitted_metadata"] != metadata):
                    raise Conflict("invocation request differs")
                return existing
            owner_token = secrets.token_hex(32)
            started = {"schema": "r2-invocation-start-v1", "project_uid": self.project_uid,
                       "task_uid": self._task(task_key)["task_uid"],
                       "invocation_id": invocation_id, "request_sha256": request_sha256,
                       "owner_sha256": _digest(owner_token.encode()),
                       "permitted_metadata": metadata}
            _write(fd, "started.json", _bytes(started))
            return {"state": "started", "owner_token": owner_token, "started": started}

    def finish_invocation(self, task_key: str, invocation_id: str, owner_token: str,
                          *, exit_code: int, error_code: str,
                          object_sha256: str | None) -> dict[str, Any]:
        relative = self._invocation(task_key, invocation_id)
        _token(owner_token)
        self._validate_outcome(exit_code, error_code)
        if object_sha256 is not None:
            self._artifact(task_key, object_sha256)
        with self._directory(relative) as fd, _lock(fd):
            existing = self._read_invocation(fd, task_key, invocation_id)
            if (existing["state"] == "not-started"
                    or not secrets.compare_digest(
                        existing["started"]["owner_sha256"], _digest(owner_token.encode()))):
                raise Conflict("invocation owner does not match")
            finished = {"schema": "r2-invocation-finish-v1",
                        "started_sha256": _digest(_bytes(existing["started"])),
                        "exit_code": exit_code, "error_code": error_code,
                        "object_sha256": object_sha256}
            if existing["state"] == "finished":
                if existing["finished"] != finished:
                    raise Conflict("terminal invocation result differs")
                return existing["finished"]
            _write(fd, "finished.json", _bytes(finished))
            return finished

    def read_invocation(self, task_key: str, invocation_id: str) -> dict[str, Any]:
        relative = self._invocation(task_key, invocation_id)
        try:
            with self._directory(relative) as fd:
                try:
                    with _lock(fd, create=False):
                        return self._read_invocation(fd, task_key, invocation_id)
                except FileNotFoundError:
                    # No writer has acquired this invocation yet. Inspect without
                    # creating bookkeeping; malformed existing receipts still fail.
                    result = self._read_invocation(fd, task_key, invocation_id)
                    if result["state"] != "not-started":
                        raise MemoryError("invocation receipts lack their owning lock")
                    return result
        except MissingMemory:
            return {"state": "not-started"}
