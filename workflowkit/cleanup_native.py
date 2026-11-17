"""Native bindings and narrowly scoped effects for merged-task cleanup.

The policy lives in :mod:`workflowkit.cleanup`; this module owns native
identity, bounded read-only observation, and the supported Kent/R2 interfaces.
"""
from __future__ import annotations

from bisect import bisect_left
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import tomllib
import uuid
from typing import Any, Mapping, Sequence

from . import delivery, operations
from .cleanup import (
    CleanupError,
    MAX_CONTAINERS,
    MAX_INVENTORY_BYTES,
    MAX_SESSIONS,
    MAX_WORKTREES,
    MAX_RECEIPT_BYTES,
    RECEIPT_SCHEMA,
    _native_worktree_path,
    FiniteHistoryInventory,
    _SESSION_ID,
    _TASK_ID,
    _PROJECT_ID,
    _OID,
    _validate_release,
    build_session_closure,
    canonical_bytes,
    inventory_from_bytes,
    make_handoff,
    parse_json,
    supervision_comment,
    validate_policy,
    validate_handoff,
    validate_history_inventory,
    validate_result,
    validate_receipt,
    receipt_subject,
    validate_supervision_record,
)


_TASK_PREFIXED = re.compile(r"task-[0-9a-f-]{36}\Z")
_PROJECT_PREFIXED = re.compile(r"project-[0-9a-f-]{36}\Z")
_MAX_NATIVE_OUTPUT = 1024 * 1024
_MAX_MEMORY_OUTPUT = MAX_INVENTORY_BYTES + 65_536
_MAX_NATIVE_SESSIONS = 32_768
_SNAPSHOT_SECONDS = 3.0
_MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
_MAX_MEMBER_TASKS = 256
_MAX_RECEIPT_COMMENTS = 32_768
_MAX_RECEIPT_HISTORY_BYTES = 16 * 1024 * 1024
_SESSION_COLUMNS = {
    "id": "TEXT", "project_id": "TEXT", "artifact_relpath": "TEXT",
    "previous_session_id": "TEXT", "parent_agent_session_id": "TEXT",
    "task_id": "TEXT", "workspace_id": "TEXT", "worktree_id": "TEXT",
}
_TASK_COLUMNS = {
    "id": "TEXT", "project_id": "TEXT", "workflow_id": "BLOB",
    "source_workspace_id": "TEXT", "managed_worktree_id": "TEXT",
}
_WORKSPACE_COLUMNS = {
    "id": "TEXT", "project_id": "TEXT", "canonical_root_path": "TEXT",
}
_WORKTREE_COLUMNS = {
    "id": "TEXT", "workspace_id": "TEXT", "canonical_root_path": "TEXT",
}
_COMMENT_COLUMNS = {
    "id": "TEXT", "task_id": "TEXT", "body": "TEXT",
    "author_kind": "TEXT", "author_id": "TEXT",
    "created_at_unix_ms": "INTEGER", "updated_at_unix_ms": "INTEGER",
}
_CURRENT_NODE_COLUMNS = {
    "task_id": "TEXT", "node_id": "TEXT", "session_id": "TEXT",
    "current_input_values_json": "TEXT", "entered_by_edge_id": "TEXT",
}
_NULLABLE_COLUMNS = {
    "sessions": {
        "workspace_id", "worktree_id", "previous_session_id",
        "parent_agent_session_id", "task_id",
    },
    "task_records": {"source_workspace_id", "managed_worktree_id"},
    "workspaces": set(),
    "worktrees": set(),
    "task_comments": set(),
    "task_current_nodes": {"session_id", "entered_by_edge_id"},
}


def _closed(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise CleanupError(f"{label} fields are invalid")
    return value


def _uuid4(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise CleanupError(f"{label} is invalid")
    return value


def _clean_path(path: str | Path, label: str) -> Path:
    value = Path(path)
    if (
        not value.is_absolute() or str(value) != str(path)
        or ".." in value.parts or "\x00" in str(value)
    ):
        raise CleanupError(f"{label} is not a canonical absolute path")
    return value


def _check_path_components(
    path: Path, *, allow_missing_leaf: bool = False,
    allow_missing_tree: bool = False,
) -> os.stat_result | None:
    """lstat every component so no caller follows a substituted symlink."""
    current = Path(path.anchor)
    for index, part in enumerate(path.parts[1:]):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing_tree:
                return None
            if allow_missing_leaf and index == len(path.parts[1:]) - 1:
                return None
            raise CleanupError("native path is absent")
        except OSError as error:
            raise CleanupError("native path cannot be inspected") from error
        if stat.S_ISLNK(info.st_mode):
            raise CleanupError("native path traverses a symlink")
        if index < len(path.parts[1:]) - 1 and not stat.S_ISDIR(info.st_mode):
            raise CleanupError("native path has a non-directory parent")
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise CleanupError("native path cannot be inspected") from error


def _safe_environment(context: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Keep only ordinary CLI variables and genuine injected Kent identity."""
    allowed = {
        "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT",
        "KENT_TASK_ID", "KENT_SESSION_ID", "KENT_STEP_ID", "KENT_NODE_ID",
        "KENT_PERSISTENCE_ROOT", "KENT_SERVER_HOST", "KENT_SERVER_PORT",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    if context is not None:
        environment.update(context["environment"])
    return environment


def _run_bounded(
    argv: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: float = 20.0,
    output_limit: int = _MAX_NATIVE_OUTPUT,
    input_bytes: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    if (
        not argv or not Path(argv[0]).is_absolute() or not cwd.is_absolute()
        or not 0 < timeout <= 300 or not 1 <= output_limit <= _MAX_MEMORY_OUTPUT
        or (input_bytes is not None and len(input_bytes) > MAX_INVENTORY_BYTES + 65_536)
    ):
        raise CleanupError("native command bounds are invalid")
    try:
        process = subprocess.Popen(
            list(argv), cwd=str(cwd), env=dict(environment),
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
        )
    except OSError as error:
        raise CleanupError("native command could not start") from error

    selector = selectors.DefaultSelector()
    stdout_fd, stderr_fd = process.stdout.fileno(), process.stderr.fileno()
    outputs: dict[int, bytearray] = {stdout_fd: bytearray(), stderr_fd: bytearray()}
    selector.register(process.stdout, selectors.EVENT_READ)
    selector.register(process.stderr, selectors.EVENT_READ)
    input_offset = 0
    stdin_fd = None
    if input_bytes is not None:
        stdin_fd = process.stdin.fileno()
        os.set_blocking(stdin_fd, False)
        selector.register(process.stdin, selectors.EVENT_WRITE)
        if not input_bytes:
            selector.unregister(process.stdin)
            process.stdin.close()
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise CleanupError("native command timed out; effect state is unknown")
            for key, _ in selector.select(min(remaining, 0.25)):
                if key.fileobj is process.stdin:
                    try:
                        written = os.write(
                            stdin_fd,
                            input_bytes[input_offset:input_offset + 65_536],
                        )
                    except BlockingIOError:
                        continue
                    except OSError as error:
                        process.kill()
                        process.wait()
                        raise CleanupError(
                            "native command rejected its bounded request",
                        ) from error
                    if written <= 0:
                        process.kill()
                        process.wait()
                        raise CleanupError("native command closed its request stream")
                    input_offset += written
                    if input_offset == len(input_bytes):
                        selector.unregister(process.stdin)
                        process.stdin.close()
                    continue
                try:
                    chunk = os.read(key.fd, 65_536)
                except OSError as error:
                    process.kill()
                    process.wait()
                    raise CleanupError("native command output became unavailable") from error
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output = outputs[key.fd]
                output.extend(chunk)
                if len(output) > output_limit:
                    process.kill()
                    process.wait()
                    raise CleanupError("native command output exceeds its byte limit")
        code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait()
        raise CleanupError("native command did not settle; effect state is unknown") from error
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
    return code, bytes(outputs[stdout_fd]), bytes(outputs[stderr_fd])


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    value = parse_json(raw.strip(), maximum=_MAX_NATIVE_OUTPUT)
    if not isinstance(value, dict):
        raise CleanupError(f"{label} did not return a JSON object")
    return value


def _validate_cleanup_route_graph(
    graph: Mapping[str, Any], workflow_id: str, entrypoint: Path,
) -> Path:
    if (
        graph.get("workflow_id") != workflow_id
        or not isinstance(graph.get("nodes"), list)
        or not isinstance(graph.get("edges"), list)
        or not isinstance(graph.get("transition_groups"), list)
    ):
        raise CleanupError("native cleanup Workflow graph is incomplete")
    scripts = [
        node for node in graph["nodes"]
        if isinstance(node, dict)
        and node.get("kind") == "script"
        and isinstance(node.get("script_path"), str)
        and Path(node["script_path"]).name == "cleanup-merged-task"
    ]
    if len(scripts) != 1:
        raise CleanupError("selected Workflow does not own one final cleanup Script")
    script = scripts[0]
    script_id = script.get("id")
    if not isinstance(script_id, str) or not script_id:
        raise CleanupError("final cleanup Script has no native node identity")
    script_path = _clean_path(script["script_path"], "native cleanup Script path")
    try:
        actual_entrypoint = entrypoint.resolve(strict=True)
        info = script_path.lstat()
    except OSError as error:
        raise CleanupError("native cleanup Script path is unavailable") from error
    if (
        script_path != actual_entrypoint
        or not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or not os.access(script_path, os.X_OK)
    ):
        raise CleanupError("native cleanup Script is not the executing qualified entrypoint")
    groups = [
        row for row in graph["transition_groups"]
        if isinstance(row, dict) and row.get("source_node_id") == script_id
    ]
    if (
        len(groups) != 1
        or groups[0].get("transition_id") != "disposal_completed"
        or groups[0].get("workflow_id") != workflow_id
    ):
        raise CleanupError("final cleanup Script has an unexpected transition route")
    edges = [
        row for row in graph["edges"]
        if isinstance(row, dict) and row.get("transition_group_id") == groups[0].get("id")
    ]
    if len(edges) != 1 or edges[0].get("key") != "finish_disposal":
        raise CleanupError("final cleanup Script has an unexpected successor edge")
    targets = [
        node for node in graph["nodes"]
        if isinstance(node, dict) and node.get("id") == edges[0].get("target_node_id")
    ]
    if (
        len(targets) != 1
        or targets[0].get("kind") != "terminal"
        or targets[0].get("key") != "done"
    ):
        raise CleanupError("final cleanup Script does not transition directly to Done")
    return script_path


def _validate_delivered_policy_graph(
    graph: Mapping[str, Any],
    *,
    project_id: str,
    workflow_id: str,
    entrypoint: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read standing policy only from the unique native delivered route."""
    if (
        graph.get("workflow_id") != workflow_id
        or not isinstance(graph.get("nodes"), list)
        or not isinstance(graph.get("edges"), list)
        or not isinstance(graph.get("transition_groups"), list)
    ):
        raise CleanupError("native policy Workflow graph is incomplete")
    script_path = _validate_cleanup_route_graph(graph, workflow_id, entrypoint)
    scripts = [
        row for row in graph["nodes"]
        if isinstance(row, dict)
        and row.get("kind") == "script"
        and isinstance(row.get("script_path"), str)
        and Path(row["script_path"]).name == "cleanup-merged-task"
    ]
    if len(scripts) != 1:
        raise CleanupError("selected Workflow does not own one policy entry Script")
    entry_node_id = scripts[0].get("id")
    groups = [
        row for row in graph["transition_groups"]
        if isinstance(row, dict) and row.get("transition_id") == "delivered"
    ]
    if (
        len(groups) != 1
        or groups[0].get("workflow_id") != workflow_id
    ):
        raise CleanupError("selected Workflow has no unique delivered policy group")
    group = groups[0]
    source_node_id = group.get("source_node_id")
    sources = [
        row for row in graph["nodes"]
        if isinstance(row, dict) and row.get("id") == source_node_id
    ]
    edges = [
        row for row in graph["edges"]
        if isinstance(row, dict) and row.get("transition_group_id") == group.get("id")
    ]
    if (
        len(sources) != 1 or sources[0].get("kind") != "agent"
        or len(edges) != 1 or edges[0].get("key") != "finish_delivered"
        or edges[0].get("target_node_id") != entry_node_id
    ):
        raise CleanupError("delivered policy group does not enter the qualified final Script")
    description = group.get("description")
    if not isinstance(description, str):
        raise CleanupError("delivered policy description is absent")
    try:
        raw = description.encode("ascii")
    except UnicodeEncodeError as error:
        raise CleanupError("delivered policy description is not ASCII") from error
    if len(raw) > 1000:
        raise CleanupError("delivered policy description exceeds 1000 bytes")
    policy = validate_policy(
        parse_json(raw, maximum=1000),
        project_id=project_id, workflow_id=workflow_id,
    )
    provenance = {
        "transition_group_id": group.get("id"),
        "transition_id": "delivered",
        "source_node_id": source_node_id,
        "entry_node_id": entry_node_id,
        "edge_key": "finish_delivered",
        "policy_sha256": hashlib.sha256(canonical_bytes(policy, maximum=1000)).hexdigest(),
    }
    # Reuse the closed validator to reject malformed native identifiers and
    # bind the digest to parsed values, independent of description formatting.
    from .cleanup import validate_policy_provenance
    validate_policy_provenance(provenance, policy=policy)
    if script_path != Path(scripts[0]["script_path"]).resolve(strict=True):
        raise CleanupError("delivered route entrypoint differs from final Script")
    return policy, provenance


class NativeCleanupBackend:
    """Qualified native observer and effect adapter.

    ``runner`` and ``context`` are dependency injection seams for deterministic
    fixture tests. Production callers always discover and revalidate the
    actual local Kent service context.
    """

    def __init__(
        self,
        *,
        workspace: str | Path | None = None,
        kent: str | Path | None = None,
        context: Mapping[str, Any] | None = None,
        runner=None,
        memory_adapter: str | Path | None = None,
        provider_runner=None,
    ) -> None:
        self.workspace = _clean_path(workspace or Path.cwd(), "execution workspace")
        # The native Script assignment remains bound to its original worktree.
        # Effects run from primary after handoff, independently of that identity.
        self._script_workspace = self.workspace
        self.kent = Path(kent or shutil.which("kent") or "")
        if not self.kent.is_absolute():
            raise CleanupError("Kent executable is unavailable")
        try:
            self.kent = self.kent.resolve(strict=True)
        except OSError as error:
            raise CleanupError("Kent executable is unavailable") from error
        info = self.kent.stat()
        if not stat.S_ISREG(info.st_mode) or not os.access(self.kent, os.X_OK):
            raise CleanupError("Kent executable is not a regular executable file")
        self.context = dict(context) if context is not None else operations.native_local_context(
            kent=str(self.kent), project_root=str(self.workspace),
        )
        operations.validate_native_local_context(self.context)
        self.runner = runner or self._run_cli
        self.provider_runner = provider_runner or self._run_provider
        self.memory_adapter = Path(memory_adapter or (
            self.workspace / ".kent/scripts/workflow-project-memory"
        ))
        self._protected_roots: list[Path] = []
        self._identity: dict[str, Any] | None = None
        self._detail: dict[str, Any] | None = None
        self._graph: dict[str, Any] | None = None
        self._snapshot: dict[str, Any] | None = None
        self._worktree_settlements: set[str] = set()
        self._script_native_context: dict[str, Any] | None = None
        self._frozen_script_identity: dict[str, Any] | None = None
        self._owned_container_paths: set[str] = set()
        self._receipt_inventory = None
        self._receipt_entries: dict[tuple, dict[str, Any]] = {}
        self._native_ack: dict[str, dict[str, Any]] = {}
        self._pending_worktree_proofs: dict[str, dict[str, Any]] = {}

    @property
    def environment(self) -> dict[str, str]:
        return _safe_environment(self.context)

    def _run_cli(self, args: Sequence[str], *, cwd: Path, environment=None, **kwargs):
        return _run_bounded(
            [str(self.kent), *args], cwd=cwd,
            environment=self.environment if environment is None else environment, **kwargs,
        )

    def _run_provider(self, args: Sequence[str], *, cwd: Path, **kwargs):
        executable = shutil.which("gh")
        if not executable:
            raise CleanupError("GitHub CLI is unavailable for fresh PR verification")
        return _run_bounded(
            [str(Path(executable).resolve(strict=True)), *args],
            cwd=cwd, environment=self.environment, **kwargs,
        )

    def _read(self, args: Sequence[str], *, cwd: Path | None = None, limit=_MAX_NATIVE_OUTPUT):
        code, output, error = self.runner(
            args, cwd=cwd or self.workspace, output_limit=limit,
        )
        if code:
            raise CleanupError(
                "native read failed: " + error.decode("utf-8", "replace")[:512],
            )
        return output

    def _native_task(self, task_id: str, project_id: str) -> dict[str, Any]:
        row = _json_object(self._read([
            "task", "show", task_id, "--project", project_id, "--json",
        ]), "native Task read")
        summary = row.get("summary")
        status = row.get("status")
        if (
            not isinstance(summary, dict) or summary.get("id") != task_id
            or summary.get("project_id") != project_id
            or not isinstance(status, dict)
            or not isinstance(status.get("native_state"), str)
            or not status["native_state"]
            or len(status["native_state"]) > 128
        ):
            raise CleanupError("native Task identity or state is incomplete")
        return row

    def _script_identity(self, native_context: Mapping[str, Any]) -> dict[str, Any]:
        context = dict(native_context)
        if self._script_native_context is not None and context != self._script_native_context:
            raise CleanupError("native Script context changed after preparation")
        if not self._script_workspace.exists():
            return self._outside_script_identity(context)
        observed = delivery.observe_script_identity(
            self._script_workspace, None, context,
            native_read=self._delivery_native_read,
            return_details=True,
        )
        identity = observed[0]
        if self._frozen_script_identity is not None and identity != self._frozen_script_identity:
            raise CleanupError("fresh native Script identity differs from the prepared Script")
        self._script_native_context = context
        self._frozen_script_identity = dict(identity)
        self._identity, self._detail, self._graph = observed
        return self._identity

    def _outside_script_identity(self, context: Mapping[str, Any]) -> dict[str, Any]:
        """Fresh Script/DB qualification after this invocation removed its root.

        This is not an attachment loader or permission to launch from an absent
        workspace. Native Resume must recreate the subject before loading R2.
        """
        inventory = self._receipt_inventory
        if inventory is None or self._frozen_script_identity is None:
            raise CleanupError("outside Script lacks a loaded, qualified inventory")
        value = inventory.value
        if value["subject"]["worktree_id"] not in self._worktree_settlements:
            raise CleanupError("outside Script root removal is not settled")
        detail = self._native_task(value["task_id"], value["project_id"])
        summary, source = detail.get("summary", {}), detail.get("source_workspace", {})
        scripts = detail.get("current_scripts")
        matches = [
            row for row in scripts
            if isinstance(row, dict) and isinstance(row.get("current_node"), dict)
            and row["current_node"].get("node_id") == context["node_id"]
            and row["current_node"].get("transition_branch_key") == context.get("transition_branch_key")
        ] if isinstance(scripts, list) and len(scripts) <= 256 else []
        if (
            summary.get("workflow_id") != value["workflow_id"]
            or source.get("is_primary") is not True
            or source.get("workspace_id") != value["subject"]["workspace_id"]
            or source.get("root_path") != str(self.workspace)
            or detail.get("worktree_path") not in (None, str(self._script_workspace))
            or len(matches) != 1 or matches[0]["current_node"].get("session_id")
            or self.workspace != Path.cwd().resolve()
        ):
            raise CleanupError("fresh outside Script native assignment differs")
        snapshot = self._session_snapshot((value["task_id"],))
        tasks = [row for row in snapshot["tasks"] if row["task_id"] == value["task_id"]]
        nodes = [
            row for row in snapshot["current_nodes"]
            if row["task_id"] == value["task_id"] and row["node_id"] == context["node_id"]
            and row["session_id"] is None
        ]
        if (
            len(tasks) != 1 or len(nodes) != 1
            or tasks[0]["project_id"] != value["project_id"]
            or tasks[0]["workflow_id"] != value["workflow_id"]
            or tasks[0]["workspace_id"] != value["subject"]["workspace_id"]
            or tasks[0]["worktree_id"] not in (None, value["subject"]["worktree_id"])
        ):
            raise CleanupError("fresh outside Script database identity differs")
        graph = _json_object(self._read([
            "workflow", "graph", "inspect", value["workflow_id"], "--json",
        ]), "outside Script Workflow")
        body = graph.get("graph")
        if not isinstance(body, dict):
            raise CleanupError("outside Script Workflow graph is absent")
        policy, provenance = _validate_delivered_policy_graph(
            {**body, "workflow_id": graph.get("workflow_id")},
            project_id=value["project_id"], workflow_id=value["workflow_id"],
            entrypoint=Path(sys.argv[0]),
        )
        if policy != value["policy"] or provenance != value["policy_provenance"]:
            raise CleanupError("outside Script policy or route changed")
        script = next(row for row in body["nodes"] if row["id"] == context["node_id"])
        if delivery._script_path(self._script_workspace, matches[0].get("path")) != (
            delivery._script_path(self._script_workspace, script.get("script_path"))
        ):
            raise CleanupError("outside Script executable differs from native graph")
        identity = {
            "task_id": summary["id"], "task_short_id": summary["short_id"],
            "project_id": summary["project_id"], "workspace_path": str(self._script_workspace),
            "invocation_kind": "script", "node_id": context["node_id"],
            "transition_branch_key": context.get("transition_branch_key"),
        }
        if identity != self._frozen_script_identity:
            raise CleanupError("fresh outside Script identity differs")
        self._identity, self._detail, self._graph = identity, detail, graph
        return identity

    def _delivery_native_read(self, root: Path, args: Sequence[str], *, structured=True):
        if root != self._script_workspace:
            raise CleanupError("native Script reader root differs")
        output = self._read(args)
        if not structured:
            value = output.decode("utf-8").strip()
            return value
        return _json_object(output, "native Script read")

    def _session_snapshot(self, task_ids: Sequence[str] = ()) -> dict[str, Any]:
        task_ids = tuple(sorted(set(task_ids)))
        if len(task_ids) > _MAX_MEMBER_TASKS:
            raise CleanupError("native Task selection exceeds its bound")
        for task_id in task_ids:
            _uuid4(task_id, _TASK_ID, "Task id")
        operations.validate_native_local_context(self.context)
        database = Path(self.context["persistence_root"]) / "db/main.sqlite3"
        before = database.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise CleanupError("native persistence database identity is unsafe")
        deadline = time.monotonic() + _SNAPSHOT_SECONDS
        connection = None
        result: tuple[list[dict[str, str]], list[dict[str, str]]]
        try:
            connection = sqlite3.connect(
                database.as_uri() + "?mode=ro", uri=True,
                timeout=0.25, isolation_level=None,
            )
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_SNAPSHOT_BYTES)
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=250")
            connection.execute("BEGIN")
            if connection.execute("PRAGMA query_only").fetchone() != (1,):
                raise CleanupError("native observer is not query-only")
            actual = [
                row[2] for row in connection.execute("PRAGMA database_list")
                if row[1] == "main"
            ]
            if actual != [str(database)]:
                raise CleanupError("native observer database binding differs")
            schema_objects = {
                row[0]: row[1] for row in connection.execute(
                    "SELECT name,type FROM sqlite_master "
                    "WHERE name IN ('sessions','task_records','workspaces','worktrees',"
                    "'task_comments','task_current_nodes')",
                )
            }
            for table, required in (
                ("sessions", _SESSION_COLUMNS),
                ("task_records", _TASK_COLUMNS),
                ("workspaces", _WORKSPACE_COLUMNS),
                ("worktrees", _WORKTREE_COLUMNS),
                ("task_comments", _COMMENT_COLUMNS),
                ("task_current_nodes", _CURRENT_NODE_COLUMNS),
            ):
                schema_rows = connection.execute(
                    f'PRAGMA table_info("{table}")'
                ).fetchall()
                expected_object = "view" if table == "task_records" else "table"
                if schema_objects.get(table) != expected_object:
                    raise CleanupError("native observer object kind differs")
                columns = {
                    row[1]: {
                        "type": row[2].upper(),
                        "notnull": row[3] == 1,
                        "primary_key": row[5] > 0,
                    }
                    for row in schema_rows
                }
                if any(
                    name not in columns
                    or columns[name]["type"] != kind
                    or (
                        table != "task_records"
                        and
                        (not columns[name]["notnull"] and not columns[name]["primary_key"])
                        != (name in _NULLABLE_COLUMNS[table])
                    )
                    for name, kind in required.items()
                ):
                    raise CleanupError("native observer schema differs")
            def scan_catalog(cursor, label):
                rows, scanned_bytes = [], 0
                while True:
                    batch = cursor.fetchmany(128)
                    if not batch:
                        break
                    for item in batch:
                        scanned_bytes += sum(
                            len(value.encode("utf-8")) if isinstance(value, str)
                            else len(value) if isinstance(value, bytes) else 8
                            for value in item if value is not None
                        )
                    rows.extend(batch)
                    if (
                        len(rows) > _MAX_NATIVE_SESSIONS
                        or scanned_bytes > _MAX_SNAPSHOT_BYTES
                        or time.monotonic() > deadline
                    ):
                        raise CleanupError(f"native {label} catalog exceeds its bounded scan")
                return rows

            sessions = scan_catalog(connection.execute(
                "SELECT id,project_id,task_id,parent_agent_session_id,"
                "previous_session_id,artifact_relpath,workspace_id,worktree_id "
                "FROM sessions ORDER BY id LIMIT ?",
                (_MAX_NATIVE_SESSIONS + 1,),
            ), "Session")
            current_node_owners = scan_catalog(connection.execute(
                "SELECT task_id,node_id,session_id,entered_by_edge_id "
                "FROM task_current_nodes "
                "ORDER BY task_id,node_id,session_id LIMIT ?",
                (_MAX_NATIVE_SESSIONS + 1,),
            ), "current-node")
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                current_node_values = connection.execute(
                    "SELECT task_id,node_id,session_id,current_input_values_json,"
                    "entered_by_edge_id "
                    "FROM task_current_nodes "
                    f"WHERE task_id IN ({placeholders}) "
                    "ORDER BY task_id,node_id,session_id LIMIT ?",
                    (*task_ids, MAX_SESSIONS + 1),
                ).fetchall()
                if len(current_node_values) > MAX_SESSIONS:
                    raise CleanupError("selected native Task current nodes exceed their bound")
                tasks = connection.execute(
                    "SELECT id,project_id,workflow_id,source_workspace_id,managed_worktree_id "
                    f"FROM task_records WHERE id IN ({placeholders}) ORDER BY id LIMIT ?",
                    (*task_ids, _MAX_MEMBER_TASKS + 1),
                ).fetchall()
            else:
                current_node_values = []
                tasks = []
            if len(tasks) > _MAX_MEMBER_TASKS:
                raise CleanupError("native Task catalog exceeds its bound")
            workspace_ids = sorted({row[3] for row in tasks if row[3] is not None})
            worktree_ids = sorted({row[4] for row in tasks if row[4] is not None})
            if workspace_ids:
                placeholders = ",".join("?" for _ in workspace_ids)
                workspaces = connection.execute(
                    "SELECT id,project_id,canonical_root_path FROM workspaces "
                    f"WHERE id IN ({placeholders}) ORDER BY id LIMIT ?",
                    (*workspace_ids, MAX_WORKTREES + _MAX_MEMBER_TASKS + 1),
                ).fetchall()
            else:
                workspaces = []
            if worktree_ids:
                placeholders = ",".join("?" for _ in worktree_ids)
                worktrees = connection.execute(
                    "SELECT id,workspace_id,canonical_root_path FROM worktrees "
                    f"WHERE id IN ({placeholders}) ORDER BY id LIMIT ?",
                    (*worktree_ids, MAX_WORKTREES + _MAX_MEMBER_TASKS + 1),
                ).fetchall()
            else:
                worktrees = []
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                comments = connection.execute(
                    "SELECT id,task_id,body,author_kind,author_id,created_at_unix_ms,"
                    "updated_at_unix_ms FROM task_comments "
                    f"WHERE task_id IN ({placeholders}) "
                    "ORDER BY task_id,created_at_unix_ms,id LIMIT ?",
                    (*task_ids, _MAX_MEMBER_TASKS * 256 + 1),
                ).fetchall()
            else:
                comments = []
            estimated = sum(len(str(field).encode("utf-8")) for group in (
                sessions, current_node_owners, current_node_values, tasks,
                workspaces, worktrees, comments,
            ) for row in group for field in row if field is not None)
            if estimated > _MAX_SNAPSHOT_BYTES or time.monotonic() > deadline:
                raise CleanupError("native snapshot exceeds its byte/time bound")
            parsed_current_nodes = []
            selected_task_ids = set(task_ids)
            for task_id, node_id, session_id, values_raw, entered_by_edge_id in current_node_values:
                if (
                    not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id)
                    or not isinstance(node_id, str) or not _SESSION_ID.fullmatch(node_id)
                    or (session_id is not None and (
                        not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id)
                    ))
                    or not isinstance(values_raw, str)
                    or len(values_raw.encode("utf-8")) > 65_536
                    or (entered_by_edge_id is not None and (
                        not isinstance(entered_by_edge_id, str)
                        or not _SESSION_ID.fullmatch(entered_by_edge_id)
                    ))
                ):
                    raise CleanupError("native current-node identity or input size is invalid")
                values = parse_json(values_raw.encode("utf-8"), maximum=65_536)
                if not isinstance(values, dict):
                    raise CleanupError("native current-node values are not an object")
                parsed_current_nodes.append({
                    "task_id": task_id, "node_id": node_id, "session_id": session_id,
                    "current_input_values": values,
                    "entered_by_edge_id": entered_by_edge_id,
                })
            known_owners = {
                (row["task_id"], row["node_id"], row["session_id"])
                for row in parsed_current_nodes
            }
            for task_id, node_id, session_id, entered_by_edge_id in current_node_owners:
                if (
                    not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id)
                    or not isinstance(node_id, str) or not _SESSION_ID.fullmatch(node_id)
                    or (session_id is not None and (
                        not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id)
                    ))
                    or (entered_by_edge_id is not None and (
                        not isinstance(entered_by_edge_id, str)
                        or not _SESSION_ID.fullmatch(entered_by_edge_id)
                    ))
                ):
                    raise CleanupError("native current-node owner identity is invalid")
                key = (task_id, node_id, session_id)
                if task_id not in selected_task_ids and key not in known_owners:
                    parsed_current_nodes.append({
                        "task_id": task_id, "node_id": node_id, "session_id": session_id,
                        "current_input_values": None,
                        "entered_by_edge_id": entered_by_edge_id,
                    })
                    known_owners.add(key)
            if [row[0] for row in connection.execute("PRAGMA query_only")] != [1]:
                raise CleanupError("native database is not read-only")
        except (sqlite3.Error, OSError, TypeError, ValueError) as error:
            if isinstance(error, CleanupError):
                raise
            raise CleanupError("native SQLite snapshot is unavailable or incompatible") from error
        finally:
            if connection is not None:
                connection.close()
        after = database.lstat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise CleanupError("native persistence database changed during observation")
        operations.validate_native_local_context(self.context)
        return {
            "sessions": [
                dict(zip(
                    ("session_id", "project_id", "task_id", "parent_agent_session_id",
                     "previous_session_id", "artifact_relpath", "workspace_id",
                     "worktree_id"), row,
                )) for row in sessions
            ],
            "current_nodes": parsed_current_nodes,
            "tasks": [
                {
                    **dict(zip(("task_id", "project_id", "workflow_id", "workspace_id",
                                "worktree_id"), row)),
                    "workflow_id": (
                        str(uuid.UUID(bytes=bytes(row[2])))
                        if isinstance(row[2], (bytes, bytearray, memoryview))
                        and len(row[2]) == 16 else None
                    ),
                }
                for row in tasks
            ],
            "workspaces": [
                dict(zip(("workspace_id", "project_id", "path"), row))
                for row in workspaces
            ],
            "worktrees": [
                dict(zip(("worktree_id", "workspace_id", "path"), row))
                for row in worktrees
            ],
            "comments": [
                dict(zip(("comment_id", "task_id", "body", "author_kind", "author_id",
                          "created_at_unix_ms", "updated_at_unix_ms"), row))
                for row in comments
            ],
        }

    def _snapshot_once(self, task_ids: Sequence[str] = ()) -> dict[str, Any]:
        # The Session catalog is small and global by contract; native Task and
        # comment queries stay restricted to the explicitly selected members.
        key = tuple(sorted(set(task_ids)))
        snapshots = getattr(self, "_snapshots", None)
        if snapshots is None:
            snapshots = {}
            self._snapshots = snapshots
        if key not in snapshots:
            snapshots[key] = self._session_snapshot(key)
        return snapshots[key]

    def _task_and_session_rows(self, task_id: str, project_id: str):
        snapshot = self._snapshot_once((task_id,))
        task = next((row for row in snapshot["tasks"] if row["task_id"] == task_id), None)
        if task is None or task["project_id"] != project_id:
            raise CleanupError("native Task is absent from the qualified database")
        by_id = {row["session_id"]: row for row in snapshot["sessions"]}
        return task, by_id, snapshot

    def _session_path(self, row: Mapping[str, Any]) -> tuple[Path, os.stat_result | None]:
        relative = row["artifact_relpath"]
        if (
            not isinstance(relative, str) or relative.startswith("/")
            or "\\" in relative or any(part in {"", ".", ".."} for part in relative.split("/"))
            or relative != f"projects/{row['project_id']}/sessions/{row['session_id']}"
        ):
            raise CleanupError("native Session artifact path is unsafe")
        root = Path(self.context["persistence_root"])
        path = _clean_path(root / relative, "Session artifact path")
        if root not in path.parents:
            raise CleanupError("Session artifact escapes the persistence root")
        info = _check_path_components(path, allow_missing_leaf=True)
        if info is not None and not stat.S_ISDIR(info.st_mode):
            raise CleanupError("Session artifact is not a directory")
        return path, info

    def _task_comment_records(self, task_id: str) -> list[dict[str, Any]]:
        snapshot = self._snapshot_once((task_id,))
        result = []
        for comment in snapshot["comments"]:
            if comment["task_id"] != task_id:
                continue
            if (
                comment["author_kind"] not in {"user", "agent"}
                or not isinstance(comment["body"], str)
                or len(comment["body"].encode("utf-8")) > 262_144
                or type(comment["created_at_unix_ms"]) is not int
                or type(comment["updated_at_unix_ms"]) is not int
            ):
                raise CleanupError("native Task comment record is malformed")
            result.append(comment)
        return result

    def _receipt_comments(self) -> list[dict[str, Any]]:
        """Read every comment page in one qualified read-only WAL transaction."""
        operations.validate_native_local_context(self.context)
        database = Path(self.context["persistence_root"]) / "db/main.sqlite3"
        before = _check_path_components(database)
        if before is None or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise CleanupError("receipt comment database is unsafe")
        connection = None
        result: list[dict[str, Any]] = []
        deadline, size = time.monotonic() + _SNAPSHOT_SECONDS, 0
        try:
            connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.25)
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            if (
                connection.execute("PRAGMA query_only").fetchone() != (1,)
                or [r[2] for r in connection.execute("PRAGMA database_list")
                    if r[1] == "main"] != [str(database)]
                or connection.execute(
                    "SELECT type FROM sqlite_master WHERE name='task_comments'",
                ).fetchone() != ("table",)
            ):
                raise CleanupError("receipt reader is not bound to native comments")
            columns = {r[1]: r[2].upper() for r in connection.execute("PRAGMA table_info(task_comments)")}
            if any(columns.get(key) != value for key, value in _COMMENT_COLUMNS.items()):
                raise CleanupError("receipt comment schema differs")
            offset = 0
            while True:
                page = connection.execute(
                    "SELECT id,task_id,body,author_kind,author_id,created_at_unix_ms,"
                    "updated_at_unix_ms FROM task_comments WHERE task_id=? "
                    "ORDER BY created_at_unix_ms,id LIMIT 128 OFFSET ?",
                    (self._receipt_inventory.value["task_id"], offset),
                ).fetchall()
                for item in page:
                    size += sum(len(v.encode("utf-8")) for v in item if isinstance(v, str))
                    if (
                        len(result) >= _MAX_RECEIPT_COMMENTS or size > _MAX_RECEIPT_HISTORY_BYTES
                        or time.monotonic() > deadline
                    ):
                        raise CleanupError("complete receipt history exceeds its row/byte/time bound")
                    result.append(dict(zip((
                        "comment_id", "task_id", "body", "author_kind", "author_id",
                        "created_at_unix_ms", "updated_at_unix_ms",
                    ), item)))
                if len(page) < 128:
                    break
                offset += len(page)
        except sqlite3.Error as error:
            raise CleanupError("complete native receipt read is unavailable") from error
        finally:
            if connection is not None:
                connection.close()
        after = database.lstat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise CleanupError("receipt database changed during readback")
        operations.validate_native_local_context(self.context)
        return result

    def _qualify_receipt_producer(self) -> dict[str, Any]:
        value = self._receipt_inventory.value
        if value["history_operation"] is not None:
            operation = value["history_operation"]
            self._snapshots = {}
            observed = self._verify_history_operator(operation)
            if value["task_id"] not in operation["selected_task_ids"]:
                raise CleanupError("receipt historical subject is outside the frozen operation")
            return {
                "kind": "history", "session_id": observed["session_id"],
                "project_id": observed["project_id"],
            }
        if self._script_native_context is None:
            raise CleanupError("receipt lacks genuine Script context")
        identity = self._script_identity(self._script_native_context)
        if (
            identity.get("task_id") != value["task_id"]
            or identity.get("project_id") != value["project_id"]
            or identity.get("node_id") != value["policy_provenance"]["entry_node_id"]
        ):
            raise CleanupError("receipt producer differs from the native Script subject")
        return {
            "kind": "script", "task_id": identity["task_id"], "node_id": identity["node_id"],
            "transition_branch_key": self._script_native_context.get("transition_branch_key"),
        }

    @staticmethod
    def _receipt_key(stage: str, entry: Mapping[str, Any]) -> tuple:
        key = (stage, entry["kind"], entry["index"])
        if stage == "worktree_settled":
            incarnation = entry["proof"]["incarnation"]
            key += tuple(incarnation[field] for field in ("device", "inode", "ctime_ns"))
        return key

    def _load_receipts(self) -> list[dict[str, Any]]:
        inventory = self._receipt_inventory
        entries: dict[tuple, dict[str, Any]] = {}
        records = self._receipt_comments()
        for record in records:
            body = record["body"]
            if not isinstance(body, str) or len(body.encode("utf-8")) > 262_144:
                raise CleanupError("native receipt comment body is malformed")
            try:
                value = parse_json(body.encode("utf-8"), maximum=262_144)
            except CleanupError:
                if "merged-cleanup-receipt-" in body:
                    raise CleanupError("receipt history contains a truncated or malformed receipt") from None
                continue
            if (
                isinstance(value, dict)
                and isinstance(value.get("schema"), str)
                and value["schema"].startswith("merged-cleanup-receipt-")
                and value["schema"] != RECEIPT_SCHEMA
                and value.get("inventory_sha256") == inventory.sha256
            ):
                raise CleanupError("receipt history uses an unsupported receipt version")
            if not isinstance(value, dict) or value.get("schema") != RECEIPT_SCHEMA:
                continue
            if value.get("inventory_sha256") != inventory.sha256:
                continue
            if (
                record["author_kind"] != "agent"
                or record["author_id"] != self._receipt_author_id
                or type(record["created_at_unix_ms"]) is not int
                or record["updated_at_unix_ms"] != record["created_at_unix_ms"]
                or not isinstance(record["comment_id"], str)
                or re.fullmatch(r"comment-[0-9a-f-]{36}", record["comment_id"]) is None
            ):
                raise CleanupError("receipt actor provenance or append-only identity differs")
            receipt = validate_receipt(value, inventory, self._receipt_producer)
            for entry in receipt["entries"]:
                key = self._receipt_key(receipt["stage"], entry)
                if key in entries and entries[key] != entry:
                    raise CleanupError("conflicting durable cleanup receipts")
                entries[key] = entry
        for key in entries:
            if key[:2] == ("container_settled", "session") and (
                "session_native_settled", "session", key[2],
            ) not in entries:
                raise CleanupError("physical receipt lacks a durable lifecycle ACK")
        self._receipt_entries = entries
        self._session_settlements = {
            inventory.value["sessions"][key[2]]["session_id"]: entry["disposition"]
            for key, entry in entries.items() if key[0] == "session_native_settled"
        }
        self._worktree_settlements = set()
        for key in entries:
            if key[0] == "worktree_settled":
                row = inventory.value["worktrees"][key[2]]
                if _check_path_components(Path(row["path"]), allow_missing_tree=True) is None:
                    self._worktree_settlements.add(row["worktree_id"])
        for row in inventory.value["worktrees"]:
            if row["worktree_id"] in self._pending_worktree_proofs and (
                _check_path_components(Path(row["path"]), allow_missing_tree=True) is None
            ):
                self._worktree_settlements.add(row["worktree_id"])
        return records

    def begin_inventory(self, inventory):
        self._receipt_inventory = inventory
        self._native_ack = {}
        self._pending_worktree_proofs = {}
        self._snapshots = {}
        # A genuine already-applied terminal result is positive evidence, not
        # inference from absent victims. Do not repeat any effects.
        nodes = [
            row for row in self._snapshot_once((inventory.value["task_id"],))["current_nodes"]
            if row["task_id"] == inventory.value["task_id"]
        ]
        if len(nodes) == 1:
            values = nodes[0]["current_input_values"]
            raw = values.get("cleanup_result") if isinstance(values, dict) else None
            if isinstance(raw, str):
                try:
                    raw = parse_json(raw.encode("utf-8"), maximum=65_536)
                except CleanupError:
                    raw = None
            if isinstance(raw, dict) and raw.get("schema") == "merged-cleanup-result-v1":
                result = validate_result(raw)
                value = inventory.value
                if (
                    result["inventory"]["sha256"] == inventory.sha256
                    and all(result[key] == value[key] for key in ("task_id", "project_id", "workflow_id"))
                    and self._native_task(value["task_id"], value["project_id"])["status"]["native_state"]
                    == "terminal"
                ):
                    return result
        self._receipt_producer = self._qualify_receipt_producer()
        self._receipt_author_id = "merged-cleanup:" + hashlib.sha256(
            canonical_bytes(self._receipt_producer),
        ).hexdigest()
        self._load_receipts()
        return None

    def require_physical_coverage(self) -> None:
        self._load_receipts()
        for kind, rows in (
            ("session", self._receipt_inventory.value["sessions"]),
            ("owned", self._receipt_inventory.value["owned_containers"]),
        ):
            for index, row in enumerate(rows):
                if row["disposition"] != "remove":
                    continue
                if ("container_settled", kind, index) not in self._receipt_entries or (
                    kind == "session"
                    and ("session_native_settled", kind, index) not in self._receipt_entries
                ):
                    raise CleanupError("durable physical receipt coverage is incomplete")
                path = (
                    Path(self.context["persistence_root"]) / row["artifact_relpath"]
                    if kind == "session" else Path(row["path"])
                )
                if _check_path_components(path, allow_missing_tree=True) is not None:
                    raise CleanupError("a durably settled physical container reappeared")

    def record_settlement(self, stage: str, kind: str, index: int, disposition: str) -> str:
        inventory = self._receipt_inventory
        if inventory is None:
            raise CleanupError("settlement receipt lacks an immutable inventory")
        subject = receipt_subject(inventory.value, kind, index)
        prior = self._receipt_entries.get((stage, kind, index))
        if prior is not None:
            return prior["disposition"]
        if stage == "session_native_settled":
            proof = self._native_ack.get(subject["session_id"])
            if proof is None:
                raise CleanupError("cannot persist an unobserved native Session ACK")
        elif stage == "container_settled":
            if kind == "session" and subject["session_id"] not in self._session_settlements:
                raise CleanupError("container receipt lacks its durable native ACK")
            path = (
                Path(self.context["persistence_root"]) / subject["artifact_relpath"]
                if kind == "session" else Path(subject["path"])
            )
            if _check_path_components(path, allow_missing_tree=True) is not None:
                raise CleanupError("container receipt physical path is not absent")
            proof = {"path_absent": True}
        elif stage == "worktree_settled":
            self.require_physical_coverage()
            proof = self._pending_worktree_proofs.get(subject["worktree_id"])
            if proof is None:
                previous = [
                    entry for key, entry in self._receipt_entries.items()
                    if key[:3] == (stage, kind, index)
                ]
                if previous and subject["worktree_id"] in self._worktree_settlements:
                    return previous[-1]["disposition"]
                raise CleanupError("worktree receipt lacks an observed physical incarnation")
        else:
            raise CleanupError("unsupported receipt stage")
        entry = {
            "kind": kind, "index": index, "subject_sha256": hashlib.sha256(
                canonical_bytes(subject),
            ).hexdigest(), "disposition": disposition, "proof": proof,
        }
        receipt = validate_receipt({
            "schema": RECEIPT_SCHEMA, "task_id": inventory.value["task_id"],
            "inventory_sha256": inventory.sha256, "producer": self._receipt_producer,
            "stage": stage, "entries": [entry],
        }, inventory, self._receipt_producer)
        producer = self._qualify_receipt_producer()
        if producer != self._receipt_producer:
            raise CleanupError("receipt native producer changed")
        cwd = Path.cwd().resolve()
        victims = [
            self._session_path(row)[0] for row in inventory.value["sessions"]
            if row["disposition"] == "remove"
        ] + [
            Path(row["path"]) for row in (
                *inventory.value["worktrees"], *inventory.value["owned_containers"],
            ) if row["disposition"] == "remove" and row["path"] is not None
        ]
        if cwd != self.workspace or any(_overlap_path(cwd, root) for root in victims):
            raise CleanupError("receipt append is not running from a real outside cwd")
        body = canonical_bytes(receipt, maximum=MAX_RECEIPT_BYTES).decode("utf-8")
        environment = self.environment
        if producer["kind"] == "script":
            environment.pop("KENT_SESSION_ID", None)
            environment.pop("KENT_STEP_ID", None)
        try:
            _, output, _ = self.runner([
                "task", "comment", "add", inventory.value["task_id"],
                "--project", inventory.value["project_id"], "--author", "agent",
                "--author-id", self._receipt_author_id, "--body", body,
            ], cwd=self.workspace, timeout=30.0, output_limit=4096, environment=environment)
        except (OSError, CleanupError):
            output = b""
        # CLI 2.13 emits two TSV lines, not a JSON object. An uncertain write
        # must be reconciled through exact readback before any dependent effect.
        match = re.fullmatch(
            rb"comment_id\t(comment-[0-9a-f-]{36})\ntask_id\t(task-[0-9a-f-]{36})\n",
            output,
        )
        if match is not None and match[2].decode() != inventory.value["task_id"]:
            raise CleanupError("native comment response belongs to another Task")
        records = self._load_receipts()
        persisted = [
            row for row in records if row["body"] == body
            and row["author_kind"] == "agent" and row["author_id"] == self._receipt_author_id
            and (match is None or row["comment_id"] == match[1].decode())
        ]
        if not persisted or self._receipt_key(stage, entry) not in self._receipt_entries:
            raise CleanupError("receipt write remains unknown after exact complete readback")
        return disposition

    def _current_agent_session(self) -> tuple[str, dict[str, Any]]:
        session_id = os.environ.get("KENT_SESSION_ID", "")
        if not _SESSION_ID.fullmatch(session_id):
            raise CleanupError("genuine current Agent Session identity is unavailable")
        code, output, error = self.runner(
            ["session-id"], cwd=self.workspace, output_limit=4096,
        )
        identity_output = output.decode("utf-8", "replace")
        matches = re.findall(r"(?m)^UUID: ([0-9a-f-]{36})\s*$", identity_output)
        if code or len(matches) != 1 or matches[0] != session_id or not re.fullmatch(
            r"Short ID: [^\r\n]+\r?\nUUID: [0-9a-f-]{36}\r?\nLog path: [^\r\n]+\r?\n?",
            identity_output,
        ):
            raise CleanupError(
                "native current Session does not match the injected Agent identity: "
                + error.decode("utf-8", "replace")[:256],
            )
        snapshot = self._snapshot_once()
        matches = [row for row in snapshot["sessions"] if row["session_id"] == session_id]
        if len(matches) != 1:
            raise CleanupError("current Agent Session is absent from native metadata")
        return session_id, matches[0]

    def _supervision_records(self, task_id: str) -> list[dict[str, Any]]:
        records = {}
        for comment in self._task_comment_records(task_id):
            if comment["author_kind"] != "agent":
                continue
            try:
                record = parse_json(comment["body"].encode("utf-8"), maximum=16_384)
                if (
                    isinstance(record, dict)
                    and record.get("schema") == "merged-cleanup-supervision-v1"
                ):
                    validated = validate_supervision_record(record)
                    if task_id in validated["task_ids"]:
                        records[canonical_bytes(validated)] = validated
            except CleanupError:
                # A malformed record carrying the marker is ambiguous ownership.
                if "merged-cleanup-supervision-v1" in comment["body"]:
                    raise
        return list(records.values())

    def _provider_pr(self, url: str) -> dict[str, Any]:
        code, output, error = self.provider_runner([
            "pr", "view", url, "--json",
            "url,state,mergedAt,mergeCommit,baseRefName,baseRefOid,headRefName,headRefOid",
        ], cwd=self.workspace, timeout=20.0, output_limit=65_536)
        if code:
            raise CleanupError(
                "provider PR observation failed: " + error.decode("utf-8", "replace")[:512],
            )
        value = _json_object(output, "provider PR observation")
        url_match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/([0-9]+)", url)
        if not url_match:
            raise CleanupError("provider PR URL is outside the supported GitHub form")
        repository, number = url_match.group(1), int(url_match.group(2))
        if (
            value.get("url") != url or value.get("state") != "MERGED"
            or not isinstance(value.get("mergedAt"), str) or not value["mergedAt"]
            or not isinstance(value.get("baseRefName"), str) or not value["baseRefName"]
            or not isinstance(value.get("headRefName"), str) or not value["headRefName"]
            or not isinstance(value.get("baseRefOid"), str)
            or not _OID.fullmatch(value["baseRefOid"])
            or not isinstance(value.get("headRefOid"), str)
            or not _OID.fullmatch(value["headRefOid"])
        ):
            raise CleanupError("provider does not confirm the exact PR as MERGED")
        merge_commit = value.get("mergeCommit")
        if isinstance(merge_commit, dict):
            merge_commit = merge_commit.get("oid")
        if merge_commit is not None and (
            not isinstance(merge_commit, str) or not _OID.fullmatch(merge_commit)
        ):
            raise CleanupError("provider merge commit identity is malformed")
        return {
            "repository": repository, "number": number, "url": url,
            "base_ref": value["baseRefName"], "base_oid": value["baseRefOid"],
            "head_ref": value["headRefName"], "head_oid": value["headRefOid"],
            "state": "MERGED", "merged_at": value["mergedAt"],
            "merge_commit": merge_commit,
        }

    @staticmethod
    def _same_provider_pr_identity(
        frozen: Mapping[str, Any], fresh: Mapping[str, Any],
    ) -> bool:
        """Ignore only the moving base OID; retain every delivered-PR fact."""
        old = dict(frozen)
        current = dict(fresh)
        old.pop("base_oid", None)
        current.pop("base_oid", None)
        return current == old

    def _github_repository(self, root: Path) -> str:
        remote = self._git("-C", str(root), "remote", "get-url", "origin")
        match = re.fullmatch(
            r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
            r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?",
            remote,
        )
        if not match:
            raise CleanupError("native origin is not one exact supported GitHub repository")
        return match.group(1)

    def _native_paths(
        self, worktree_ids: Sequence[str], *, project_id: str,
        allow_missing: bool = False,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        worktree_ids = tuple(sorted(set(worktree_ids)))
        if len(worktree_ids) > MAX_WORKTREES:
            raise CleanupError("selected managed worktree set exceeds its bound")
        if not worktree_ids:
            return [], []
        operations.validate_native_local_context(self.context)
        database = Path(self.context["persistence_root"]) / "db/main.sqlite3"
        connection = None
        try:
            before = database.lstat()
        except OSError as error:
            raise CleanupError("native persistence database is unavailable") from error
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise CleanupError("native persistence database identity is unsafe")
        deadline = time.monotonic() + _SNAPSHOT_SECONDS
        try:
            connection = sqlite3.connect(
                database.as_uri() + "?mode=ro", uri=True,
                timeout=0.25, isolation_level=None,
            )
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=250")
            connection.execute("BEGIN")
            if connection.execute("PRAGMA query_only").fetchone() != (1,):
                raise CleanupError("native path snapshot is not query-only")
            actual = [
                row[2] for row in connection.execute("PRAGMA database_list")
                if row[1] == "main"
            ]
            if actual != [str(database)]:
                raise CleanupError("native path observer database binding differs")
            placeholders = ",".join("?" for _ in worktree_ids)
            rows = connection.execute(
                "SELECT wt.id,wt.workspace_id,wt.canonical_root_path,w.project_id,"
                "w.canonical_root_path FROM worktrees wt "
                "JOIN workspaces w ON w.id=wt.workspace_id "
                f"WHERE wt.id IN ({placeholders}) ORDER BY wt.id LIMIT ?",
                (*worktree_ids, MAX_WORKTREES + 1),
            ).fetchall()
            if (
                (not allow_missing and len(rows) != len(worktree_ids))
                or len(rows) > MAX_WORKTREES
            ):
                raise CleanupError("native managed worktree binding is missing or ambiguous")
            worktrees, workspaces = [], {}
            for worktree_id, workspace_id, path, row_project, primary in rows:
                if row_project != project_id:
                    raise CleanupError("native worktree belongs to another Project")
                path = _clean_path(path, "managed worktree root")
                primary = _clean_path(primary, "primary workspace root")
                worktrees.append({
                    "worktree_id": worktree_id,
                    "workspace_id": workspace_id,
                    "path": str(path),
                })
                workspaces[workspace_id] = {
                    "workspace_id": workspace_id,
                    "project_id": row_project,
                    "path": str(primary),
                }
            if time.monotonic() > deadline:
                raise CleanupError("native path snapshot exceeded its time bound")
            result = worktrees, list(workspaces.values())
        except (sqlite3.Error, OSError, TypeError, ValueError) as error:
            if isinstance(error, CleanupError):
                raise
            raise CleanupError("native worktree snapshot is unavailable") from error
        finally:
            if connection is not None:
                connection.close()
        after = database.lstat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise CleanupError("native persistence database changed during path observation")
        operations.validate_native_local_context(self.context)
        return result

    def _task_outcome(
        self,
        task_id: str,
        project_id: str,
        *,
        current_task_id: str,
        current_pr: Mapping[str, Any],
        known_detail: Mapping[str, Any] | None = None,
        current_disposition: bool = True,
        primary_path: Path | None = None,
    ) -> dict[str, Any]:
        detail = dict(known_detail) if known_detail is not None else self._native_task(
            task_id, project_id,
        )
        status = detail.get("status")
        native_state = status.get("native_state") if isinstance(status, dict) else None
        if task_id == current_task_id and current_disposition:
            pr = dict(current_pr)
            return {
                "task_id": task_id, "native_state": "final_disposal",
                "outcome": "merged", "pull_request_url": pr["url"],
                "head_oid": pr["head_oid"], "merged_at": pr["merged_at"],
                "merge_commit": pr["merge_commit"],
            }
        normalized_state = "terminal" if native_state == "terminal" else "active"
        if normalized_state == "terminal":
            endpoint = self._terminal_endpoint(task_id, detail)
            if endpoint is not None:
                url = f"https://github.com/{endpoint['repository']}/pull/{endpoint['number']}"
                pr = self._provider_pr(url)
                if (
                    pr["repository"] != endpoint["repository"]
                    or pr["number"] != endpoint["number"]
                    or pr["base_ref"] != endpoint["base_ref"]
                ):
                    raise CleanupError("native terminal endpoint contradicts fresh provider facts")
                if pr["head_oid"] != endpoint["head_oid"]:
                    successor = endpoint["authorized_successor_oid"]
                    if (
                        successor != pr["head_oid"]
                        or not (
                            self._git_ancestor(
                                endpoint["head_oid"], successor, root=primary_path,
                            )
                            if primary_path is not None
                            else self._git_ancestor(endpoint["head_oid"], successor)
                        )
                    ):
                        raise CleanupError("native delivered head differs from the merged PR head")
                return {
                    "task_id": task_id, "native_state": "terminal",
                    "outcome": "merged", "pull_request_url": pr["url"],
                    "head_oid": pr["head_oid"], "merged_at": pr["merged_at"],
                    "merge_commit": pr["merge_commit"],
                }
            transition = self._terminal_transition(task_id, detail)
            if transition in {"no_pr", "no-pr", "ordinary_delivered"}:
                return {
                    "task_id": task_id, "native_state": "terminal",
                    "outcome": "no-pr", "pull_request_url": None,
                    "head_oid": None, "merged_at": None, "merge_commit": None,
                }
            if transition in {"not_delivered", "non_delivery"}:
                return {
                    "task_id": task_id, "native_state": "terminal",
                    "outcome": "non-delivery", "pull_request_url": None,
                    "head_oid": None, "merged_at": None, "merge_commit": None,
                }
        return {
            "task_id": task_id, "native_state": normalized_state,
            "outcome": "unknown", "pull_request_url": None,
            "head_oid": None, "merged_at": None, "merge_commit": None,
        }

    def _terminal_endpoint(
        self, task_id: str, detail: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        transition = self._terminal_transition(task_id, detail)
        summary = detail.get("summary")
        project_id = summary.get("project_id") if isinstance(summary, dict) else None
        workflow_id = summary.get("workflow_id") if isinstance(summary, dict) else None
        if (
            not isinstance(summary, dict) or summary.get("id") != task_id
            or not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id)
            or not isinstance(workflow_id, str) or not _SESSION_ID.fullmatch(workflow_id)
        ):
            raise CleanupError("terminal Task native summary identity is incomplete")
        nodes = [
            row for row in self._snapshot_once((task_id,))["current_nodes"]
            if row["task_id"] == task_id
        ]
        if len(nodes) != 1:
            return None
        values = nodes[0]["current_input_values"]
        if not isinstance(values, dict):
            return None
        raw_result = values.get("cleanup_result")
        if isinstance(raw_result, dict):
            result = raw_result
        elif isinstance(raw_result, str) and raw_result:
            if len(raw_result.encode("utf-8")) > 65_536:
                raise CleanupError("terminal Task cleanup_result exceeds its bound")
            try:
                result = parse_json(raw_result.encode("utf-8"), maximum=65_536)
            except CleanupError:
                raise CleanupError("terminal Task cleanup_result is malformed") from None
        else:
            return None
        if not isinstance(result, dict):
            raise CleanupError("terminal Task cleanup_result is not an object")
        if result.get("schema") == "appsome-native-cleanup-result-v1":
            if detail.get("status", {}).get("native_state") != "terminal":
                return None
            if result.get("task_id") != task_id or (
                "project_id" in result and result["project_id"] != project_id
            ):
                raise CleanupError("legacy terminal result belongs to another native Task")
            return self._legacy_terminal_endpoint(result.get("endpoint"))
        if transition != "disposal_completed":
            return None
        result = validate_result(result)
        if (
            result["task_id"] != task_id
            or result["project_id"] != project_id
            or result["workflow_id"] != workflow_id
        ):
            raise CleanupError("terminal cleanup_result belongs to another native Task")
        endpoint = result.get("endpoint")
        if endpoint is None or not isinstance(endpoint, dict):
            return None
        repository = endpoint["repository"]
        number = endpoint["number"]
        base_ref = endpoint["base_ref"]
        head = endpoint["head_oid"]
        return {
            "repository": repository, "number": number,
            "base_ref": base_ref, "head_oid": head,
            "authorized_successor_oid": None,
        }

    @staticmethod
    def _legacy_terminal_endpoint(endpoint: Any) -> dict[str, Any] | None:
        """Read only the three observed typed legacy PR endpoint variants.

        These fields identify a candidate, not merge proof. Every consumer
        checks fresh provider facts and, when present, successor ancestry.
        """
        if not isinstance(endpoint, dict):
            return None
        shapes = (
            ({"repository", "pr", "base", "head", "state"},
             {"merge_commit", "merged_at", "observation", "original_delivery_proof"},
             "repository", "base", "head"),
            ({"repo", "pr", "base", "published_head", "later_authorized_successor"},
             {"ci_run", "attempt", "conclusion", "acceptance_ref", "merge_release"},
             "repo", "base", "published_head"),
            ({"repo", "pr", "base_ref", "head", "head_ref", "state"},
             {"ci", "ci_run", "download_consumed", "download_remaining",
              "supervisor_personal_actual_new_package"},
             "repo", "base_ref", "head"),
        )
        for keys, metadata, repo_key, base_key, head_key in shapes:
            if not keys <= set(endpoint) or set(endpoint) - keys - metadata:
                continue
            repository, number = endpoint[repo_key], endpoint["pr"]
            base_ref, head = endpoint[base_key], endpoint[head_key]
            successor = endpoint.get("later_authorized_successor")
            if (
                not isinstance(repository, str)
                or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
                or type(number) is not int or number <= 0
                or not isinstance(base_ref, str) or not base_ref or len(base_ref) > 256
                or not isinstance(head, str) or _OID.fullmatch(head) is None
                or (successor is not None and (
                    not isinstance(successor, str) or _OID.fullmatch(successor) is None
                ))
                or ("state" in endpoint and endpoint["state"] not in {"OPEN", "MERGED"})
                or ("head_ref" in endpoint and (
                    not isinstance(endpoint["head_ref"], str)
                    or not endpoint["head_ref"] or len(endpoint["head_ref"]) > 256
                ))
            ):
                raise CleanupError("legacy terminal endpoint fields are invalid")
            return {
                "repository": repository, "number": number, "base_ref": base_ref,
                "head_oid": head, "authorized_successor_oid": successor,
            }
        return None

    def _terminal_transition(
        self, task_id: str, detail: Mapping[str, Any],
    ) -> str | None:
        nodes = [
            row for row in self._snapshot_once((task_id,))["current_nodes"]
            if row["task_id"] == task_id
        ]
        if len(nodes) != 1 or not isinstance(nodes[0].get("entered_by_edge_id"), str):
            return None
        edge_id = nodes[0]["entered_by_edge_id"]
        summary = detail.get("summary")
        workflow_id = summary.get("workflow_id") if isinstance(summary, dict) else None
        if not isinstance(workflow_id, str) or not _SESSION_ID.fullmatch(workflow_id):
            return None
        graph_response = self._graph
        if (
            not isinstance(graph_response, dict)
            or graph_response.get("workflow_id") != workflow_id
            or not isinstance(graph_response.get("graph"), dict)
        ):
            graph_response = _json_object(self._read([
                "workflow", "graph", "inspect", workflow_id, "--json",
            ]), "terminal Task Workflow graph")
        graph = graph_response.get("graph")
        if (
            not isinstance(graph, dict)
            or graph_response.get("workflow_id") != workflow_id
            or not isinstance(graph.get("edges"), list)
            or not isinstance(graph.get("transition_groups"), list)
        ):
            return None
        edges = [
            edge for edge in graph["edges"]
            if isinstance(edge, dict) and edge.get("id") == edge_id
        ]
        if len(edges) != 1 or edges[0].get("target_node_id") != nodes[0]["node_id"]:
            return None
        groups = [
            group for group in graph["transition_groups"]
            if isinstance(group, dict)
            and group.get("id") == edges[0].get("transition_group_id")
            and isinstance(group.get("source_node_id"), str)
            and _SESSION_ID.fullmatch(group["source_node_id"])
            and group.get("workflow_id") == workflow_id
        ]
        return (
            groups[0].get("transition_id")
            if len(groups) == 1 and isinstance(groups[0].get("transition_id"), str)
            else None
        )

    def _git_ancestor(
        self, older: str, newer: str, *, root: Path | None = None,
    ) -> bool:
        code, _output, _error = _run_bounded(
            ["/usr/bin/git", "-C", str(root or self._primary_path()),
             "merge-base", "--is-ancestor", older, newer],
            cwd=self.workspace, environment=_safe_environment(),
            timeout=20.0, output_limit=4096,
        )
        if code not in (0, 1):
            raise CleanupError("Git ancestry verification is unavailable")
        return code == 0

    def _memory_call(
        self,
        operation: str,
        *,
        native_context: Mapping[str, Any] | None = None,
        body: bytes | None = None,
        digest: str | None = None,
    ) -> tuple[dict[str, Any], bytes]:
        if not self.memory_adapter.is_absolute() or self.memory_adapter.is_symlink():
            raise CleanupError("qualified workflow memory adapter is unavailable")
        adapter_info = self.memory_adapter.stat()
        if not stat.S_ISREG(adapter_info.st_mode) or not os.access(self.memory_adapter, os.X_OK):
            raise CleanupError("workflow memory adapter is not executable")
        request: dict[str, Any] = {
            "schema": "r2-workflow-request-v1",
            "operation": operation,
            "workspace_path": str(self.workspace),
        }
        if native_context is not None:
            request["native_context"] = dict(native_context)
        if operation == "artifact-write":
            if body is None:
                raise CleanupError("R2 artifact write body is absent")
            request["sha256"] = hashlib.sha256(body).hexdigest()
            request["byte_size"] = len(body)
        elif operation == "artifact-read":
            if digest is None:
                raise CleanupError("R2 artifact digest is absent")
            request["sha256"] = digest
        framed = canonical_bytes(request, maximum=65_535) + b"\n"
        if body is not None:
            framed += body
        code, output, error = _run_bounded(
            [str(self.memory_adapter), "workflow"], cwd=self.workspace,
            environment=self.environment, timeout=30.0,
            output_limit=_MAX_MEMORY_OUTPUT, input_bytes=framed,
        )
        if code:
            raise CleanupError(
                "qualified R2 attachment operation failed: "
                + error.decode("utf-8", "replace")[:512],
            )
        header, separator, content = output.partition(b"\n")
        if not separator:
            raise CleanupError("R2 attachment response framing is invalid")
        response = _json_object(header, "R2 attachment response")
        expected_operation = operation
        expected_digest = hashlib.sha256(body).hexdigest() if body is not None else digest
        if (
            response.get("schema") != "r2-artifact-response-v1"
            or response.get("operation") != expected_operation
            or response.get("object_sha256") != expected_digest
            or type(response.get("byte_size")) is not int
            or response["byte_size"] > MAX_INVENTORY_BYTES
        ):
            raise CleanupError("R2 attachment response identity differs")
        if operation == "artifact-write" and (
            response["byte_size"] != len(body or b"") or content
        ):
            raise CleanupError("R2 attachment write response is malformed")
        if operation == "artifact-read" and (
            response["byte_size"] != len(content)
            or hashlib.sha256(content).hexdigest() != expected_digest
        ):
            raise CleanupError("R2 attachment readback differs")
        return response, content

    def _bind_memory_adapter(self, primary: Path) -> None:
        profile_path = primary / ".kent/workflow-profile.toml"
        profile_info = _check_path_components(profile_path)
        if profile_info is None or not stat.S_ISREG(profile_info.st_mode):
            raise CleanupError("protected primary Project profile is unavailable")
        try:
            profile = tomllib.loads(profile_path.read_text(encoding="utf-8"))
            command = profile["commands"]["project_memory"]
        except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
            raise CleanupError("qualified workflow memory command is unavailable") from error
        if (
            not isinstance(command, str) or not command or command.startswith("/")
            or "\\" in command or "\x00" in command
            or any(part in {"", ".", ".."} for part in command.split("/"))
        ):
            raise CleanupError("workflow memory command path is unsafe")
        adapter = primary / command
        adapter_info = _check_path_components(adapter)
        if (
            adapter_info is None or not stat.S_ISREG(adapter_info.st_mode)
            or not os.access(adapter, os.X_OK)
        ):
            raise CleanupError("qualified workflow memory adapter is unavailable")
        self.memory_adapter = adapter

    def prepare(self, request: Any) -> dict[str, Any]:
        inventory = self._prepare_inventory(request)
        return make_handoff(inventory)

    def _prepare_inventory(
        self,
        request: Any,
        *,
        history_operation: Mapping[str, Any] | None = None,
        subject_task_id: str | None = None,
    ):
        # Explicitly retain the original v1 shape alongside its optional exact
        # owned-artifact carrier. No discovery or prefix-based ownership.
        keys = {"schema", "pull_request_url", "expected_base_ref", "release"}
        allowed = (keys, keys | {"owned_containers"}) if history_operation is None else ({"schema"},)
        if not isinstance(request, dict) or set(request) not in allowed:
            raise CleanupError("merged cleanup preparation request fields are invalid")
        if request["schema"] != "merged-cleanup-prepare-request-v1":
            raise CleanupError("merged cleanup preparation schema differs")
        session_id, session_row = self._current_agent_session()
        if history_operation is None:
            operator_task_id = os.environ.get("KENT_TASK_ID", "")
            _uuid4(operator_task_id, _TASK_ID, "current operator Task")
            if session_row["task_id"] != operator_task_id:
                raise CleanupError("current Agent Session is not owned by the current operator Task")
            task_id = subject_task_id or operator_task_id
            project_id = _uuid4(session_row["project_id"], _PROJECT_ID, "Task Project")
        else:
            operator_task_id = history_operation["operator_task_id"]
            if (
                session_id != history_operation["operator_session_id"]
                or session_row["project_id"] != history_operation["operator_project_id"]
                or session_row["task_id"] != operator_task_id
            ):
                raise CleanupError("historical operation differs from the genuine current Session")
            task_id = subject_task_id
            project_id = history_operation["project_id"]
        _uuid4(task_id, _TASK_ID, "cleanup subject Task")
        if history_operation is not None and (
            history_operation.get("project_id") != project_id
            or task_id not in history_operation.get("selected_task_ids", [])
            or (operator_task_id is not None and task_id == operator_task_id)
        ):
            raise CleanupError("historical request differs from genuine operator or selection")
        snapshot = self._snapshot_once((task_id,))
        task, _session_rows_by_id, _snapshot = self._task_and_session_rows(task_id, project_id)
        if history_operation is None and (
            session_row["workspace_id"] != task["workspace_id"]
            or session_row["worktree_id"] != task["worktree_id"]
        ):
            raise CleanupError("current Agent Session differs from its native Task workspace")
        if history_operation is not None and (
            session_id in {
                row["session_id"] for row in snapshot["sessions"]
                if row["task_id"] == task_id
            }
        ):
            raise CleanupError("historical operator Session is inside the victim Task closure")
        task_detail = self._native_task(task_id, project_id)
        summary = task_detail["summary"]
        workflow_id = summary.get("workflow_id")
        if not isinstance(workflow_id, str) or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            workflow_id,
        ):
            raise CleanupError("native Task Workflow identity is invalid")
        if task["workflow_id"] != workflow_id:
            raise CleanupError("native Task Workflow differs from the database")
        graph_response = _json_object(
            self._read(["workflow", "graph", "inspect", workflow_id, "--json"]),
            "native cleanup Workflow graph",
        )
        graph_body = graph_response.get("graph")
        if not isinstance(graph_body, dict) or not isinstance(graph_body.get("nodes"), list):
            raise CleanupError("native cleanup Workflow graph is incomplete")
        policy = policy_provenance = None
        if history_operation is None:
            policy, policy_provenance = _validate_delivered_policy_graph(
                {**graph_body, "workflow_id": graph_response.get("workflow_id")},
                project_id=project_id, workflow_id=workflow_id,
                entrypoint=Path(sys.argv[0]),
            )
        else:
            graph_nodes = [
                node for node in graph_body["nodes"]
                if isinstance(node, dict) and node.get("id")
            ]
            if len(graph_nodes) != len(graph_body["nodes"]):
                raise CleanupError("historical native Workflow nodes are malformed")
        current_node_rows = [
            row for row in snapshot["current_nodes"] if row["task_id"] == task_id
        ]
        if len(current_node_rows) != 1:
            raise CleanupError("native cleanup subject has no unique current node")
        current_inputs = current_node_rows[0]["current_input_values"]
        if not isinstance(current_inputs, dict):
            raise CleanupError("current native Task input values are unavailable")
        primary_rows = [
            item for item in snapshot["workspaces"]
            if item["workspace_id"] == task["workspace_id"]
        ]
        if len(primary_rows) != 1:
            raise CleanupError("subject Task primary workspace differs from native metadata")
        primary = Path(primary_rows[0]["path"])
        _check_path_components(primary)
        if history_operation is None:
            if (
                current_node_rows[0]["session_id"] != session_id
                or current_node_rows[0]["node_id"] != policy_provenance["source_node_id"]
            ):
                raise CleanupError(
                    "current Agent is not at the native delivered-policy source node",
                )
            cleanup_context = current_inputs.get("cleanup_context")
            if not isinstance(cleanup_context, str) or not cleanup_context:
                raise CleanupError("genuine native Cleanup context is absent")
            try:
                cleanup_context_bytes = cleanup_context.encode("utf-8")
            except UnicodeEncodeError as error:
                raise CleanupError("native Cleanup context is not valid UTF-8") from error
            if len(cleanup_context_bytes) > 65_536:
                raise CleanupError("native Cleanup context exceeds the string limit")
            pull_request_url = request["pull_request_url"]
            expected_base_ref = request["expected_base_ref"]
            if not isinstance(expected_base_ref, str) or not expected_base_ref:
                raise CleanupError("accepted PR base branch is absent")
        else:
            if (
                task_detail["status"]["native_state"] != "terminal"
                or current_node_rows[0]["session_id"] is not None
            ):
                raise CleanupError("historical Task is not terminal and unowned")
            terminal_nodes = [
                node for node in graph_nodes
                if node["id"] == current_node_rows[0]["node_id"]
                and node.get("kind") == "terminal"
            ]
            if len(terminal_nodes) != 1:
                raise CleanupError("historical Task terminal endpoint is unresolved")
            endpoint = self._terminal_endpoint(task_id, task_detail)
            if endpoint is None:
                raise CleanupError("historical Task has no typed merged delivery endpoint")
            pull_request_url = (
                f"https://github.com/{endpoint['repository']}/pull/{endpoint['number']}"
            )
            cleanup_context_bytes = canonical_bytes(
                history_operation["permission_evidence"], maximum=16_384,
            )
        pr = self._provider_pr(pull_request_url)
        if history_operation is None and pr["base_ref"] != expected_base_ref:
            raise CleanupError("provider PR base branch differs from accepted Delivery context")
        if history_operation is not None:
            endpoint = self._terminal_endpoint(task_id, task_detail)
            if (
                endpoint is None
                or pr["repository"] != endpoint["repository"]
                or pr["number"] != endpoint["number"]
                or pr["base_ref"] != endpoint["base_ref"]
            ):
                raise CleanupError("historical provider PR differs from its native endpoint")
            if pr["head_oid"] != endpoint["head_oid"]:
                successor = endpoint["authorized_successor_oid"]
                if (
                    successor != pr["head_oid"]
                    or not self._git_ancestor(endpoint["head_oid"], successor, root=primary)
                ):
                    raise CleanupError("historical delivered head differs from merged PR head")
        active_nodes = [
            row for row in snapshot["current_nodes"]
            if row["task_id"] == task_id
            and (row["session_id"] == session_id if history_operation is None
                 else row["session_id"] is not None)
        ]
        if history_operation is None and len(active_nodes) != 1:
            raise CleanupError("current Agent is not the unique active Task owner")
        if self._github_repository(primary) != pr["repository"]:
            raise CleanupError("provider PR repository differs from the native Project origin")
        task_worktree_inventory: list[dict[str, Any]] = []
        task_worktree_id = task["worktree_id"]
        inferred_historical_worktree = False
        if history_operation is not None and task_worktree_id is None:
            historical_ids = {
                row["worktree_id"] for row in snapshot["sessions"]
                if row["task_id"] == task_id
                and row["workspace_id"] == task["workspace_id"]
                and row["worktree_id"] is not None
            }
            if len(historical_ids) > 1:
                raise CleanupError("historical Task Sessions disagree on their worktree identity")
            if historical_ids:
                task_worktree_id = next(iter(historical_ids))
                inferred_historical_worktree = True
        subject_path: Path | None = None
        branch = head_oid = branch_oid = None
        worktree_rows: list[dict[str, str]] = []
        if task_worktree_id is not None:
            worktree_rows, _ = self._native_paths(
                [task_worktree_id], project_id=project_id,
                allow_missing=history_operation is not None,
            )
        registered_missing = (
            history_operation is not None and bool(worktree_rows)
            and _check_path_components(
                Path(worktree_rows[0]["path"]), allow_missing_tree=True,
            ) is None
        )
        if not worktree_rows or registered_missing:
            if history_operation is None:
                raise CleanupError("current Task has no native managed worktree")
            path_value = task_detail.get("worktree_path")
            if registered_missing:
                if (
                    worktree_rows[0]["workspace_id"] != task["workspace_id"]
                    or path_value not in (None, worktree_rows[0]["path"])
                ):
                    raise CleanupError("historical missing root differs from its native identity")
                path_value = worktree_rows[0]["path"]
            absent_path = None
            if path_value is not None:
                if not isinstance(path_value, str):
                    raise CleanupError("historical Task worktree path is malformed")
                absent_path = _clean_path(path_value, "historical Task worktree path")
                if _check_path_components(
                    absent_path, allow_missing_tree=True,
                ) is not None:
                    raise CleanupError(
                        "historical Task worktree path remains without native registration",
                    )
                if _overlap_path(absent_path, primary) or _overlap_path(
                    absent_path, self.workspace,
                ):
                    raise CleanupError("historical absent worktree overlaps a protected workspace")
            if task_worktree_id is not None:
                task_worktree_inventory.append({
                    "project_id": project_id,
                    "workspace_id": task["workspace_id"],
                    "worktree_id": task_worktree_id,
                    "path": str(absent_path) if absent_path is not None else None,
                    "branch": None, "head_oid": None, "branch_oid": None,
                    "role": "task", "disposition": "absent",
                    "owner_task_id": task_id,
                })
                self._verify_absent_historical_worktree(
                    task_worktree_inventory[-1], worktree_rows, primary=primary,
                )
            subject = {
                "task_short_id": summary.get("short_id"),
                "workspace_id": task["workspace_id"],
                "worktree_id": task_worktree_id,
                "worktree_path": str(absent_path) if absent_path is not None else None,
                "branch": None, "head_oid": None, "branch_oid": None,
            }
        else:
            if inferred_historical_worktree:
                raise CleanupError(
                    "historical Session worktree identity is still registered natively",
                )
            subject_worktree = worktree_rows[0]
            if subject_worktree["workspace_id"] != task["workspace_id"]:
                raise CleanupError("subject Task worktree differs from its native workspace")
            subject_path = Path(subject_worktree["path"])
            if history_operation is None and (
                subject_path != self.workspace
                or task_detail.get("worktree_path") not in (None, str(self.workspace))
            ):
                raise CleanupError("current Task worktree differs from native execution root")
            if history_operation is not None and (
                task_detail.get("worktree_path") not in (None, str(subject_path))
                or _overlap_path(subject_path, self.workspace)
            ):
                raise CleanupError("historical victim aliases or contains the operator workspace")
            if _overlap_path(primary, subject_path):
                raise CleanupError("task worktree aliases or nests the protected primary")
            _check_path_components(subject_path)
            branch = self._git("-C", str(subject_path), "symbolic-ref", "--short", "HEAD")
            head_oid = self._git("-C", str(subject_path), "rev-parse", "--verify", "HEAD")
            branch_oid = self._git(
                "-C", str(primary), "rev-parse", "--verify", f"refs/heads/{branch}",
            )
            if head_oid != pr["head_oid"] or branch_oid != head_oid:
                raise CleanupError("local Task worktree differs from the merged PR head")
            subject = {
                "task_short_id": summary.get("short_id"),
                "workspace_id": task["workspace_id"],
                "worktree_id": task_worktree_id,
                "worktree_path": str(subject_path),
                "branch": branch, "head_oid": head_oid, "branch_oid": branch_oid,
            }
            task_worktree_inventory.append({
                "project_id": project_id, "workspace_id": task["workspace_id"],
                "worktree_id": task_worktree_id, "path": str(subject_path),
                "branch": branch, "head_oid": head_oid, "branch_oid": branch_oid,
                "role": "task", "disposition": "remove", "owner_task_id": task_id,
            })
        if history_operation is None and subject_path is None:
            raise CleanupError("current Task has no native managed worktree")

        memberships = sorted(
            self._supervision_records(task_id), key=lambda item: canonical_bytes(item),
        )
        for membership in memberships:
            if membership["project_id"] != project_id:
                raise CleanupError("supervisor membership Project differs")
        member_ids = {task_id}
        for membership in memberships:
            member_ids.update(membership["task_ids"])
        if len(member_ids) > _MAX_MEMBER_TASKS:
            raise CleanupError("supervisor membership Task set exceeds its bound")
        snapshot = self._snapshot_once(sorted(member_ids))
        task_rows = {row["task_id"]: row for row in snapshot["tasks"]}
        if set(task_rows) != member_ids:
            raise CleanupError("one or more registered supervisor Tasks are missing")
        for membership in memberships:
            for member_id in membership["task_ids"]:
                copies = [
                    comment for comment in self._task_comment_records(member_id)
                    if comment["author_kind"] == "agent"
                    and comment["body"] == supervision_comment(membership)
                ]
                if not copies:
                    raise CleanupError("supervisor membership is not appended to every Task")
        member_outcomes = [
            self._task_outcome(
                member_id, project_id,
                current_task_id=task_id,
                current_pr=pr, known_detail=task_detail if member_id == task_id else None,
                current_disposition=history_operation is None,
                primary_path=primary,
            )
            for member_id in sorted(member_ids)
        ]
        unsettled = {
            item["task_id"] for item in member_outcomes
            if item["native_state"] == "active"
            or item["outcome"] not in {"merged", "no-pr", "non-delivery"}
        }
        held_roots = {
            root_id
            for membership in memberships
            if unsettled.intersection(membership["task_ids"])
            for root_id in [
                membership["supervisor_session_id"],
                *membership["reused_root_session_ids"],
            ]
        }
        supervisor_roots = {
            membership["supervisor_session_id"]
            for membership in memberships
        } | {
            root_id for membership in memberships
            for root_id in membership["reused_root_session_ids"]
        }
        children: dict[str, list[str]] = {}
        for row in snapshot["sessions"]:
            if row["parent_agent_session_id"] is not None:
                children.setdefault(row["parent_agent_session_id"], []).append(
                    row["session_id"],
                )
        descendants_by_root: dict[str, set[str]] = {}
        for root_id in supervisor_roots:
            descendants, pending = set(), [root_id]
            while pending:
                item_id = pending.pop()
                if item_id in descendants:
                    continue
                descendants.add(item_id)
                pending.extend(children.get(item_id, ()))
                if len(descendants) > MAX_SESSIONS:
                    raise CleanupError("supervisor Session closure exceeds its bound")
            descendants_by_root[root_id] = descendants
        session_rows = []
        for raw in snapshot["sessions"]:
            roots_for_row = sorted(
                root_id for root_id, descendants in descendants_by_root.items()
                if raw["session_id"] in descendants
            )
            held = any(root_id in held_roots for root_id in roots_for_row)
            session_rows.append({
                "session_id": raw["session_id"], "project_id": raw["project_id"],
                "task_id": raw["task_id"],
                "parent_agent_session_id": raw["parent_agent_session_id"],
                "previous_session_id": raw["previous_session_id"],
                "workspace_id": raw["workspace_id"],
                "worktree_id": raw["worktree_id"],
                "artifact_relpath": raw["artifact_relpath"],
                "device": None, "inode": None,
                "supervisor_root_ids": roots_for_row,
                "disposition": "shared-in-use" if held else "remove",
            })
        closure = build_session_closure(
            session_rows, task_id=task_id, project_id=project_id,
            supervision=memberships,
        )
        for row in closure:
            _, info = self._session_path(row)
            row["device"] = info.st_dev if info is not None else None
            row["inode"] = info.st_ino if info is not None else None
        current_owned = {
            row["session_id"] for row in snapshot["current_nodes"]
            if row["session_id"] is not None
        }
        session_by_id = {row["session_id"]: row for row in snapshot["sessions"]}
        active_worktree_ids = {
            session_by_id[owner_id]["worktree_id"]
            for owner_id in current_owned
            if owner_id in session_by_id and session_by_id[owner_id]["worktree_id"] is not None
        }
        foreign_task_worktree_owners = {
            owner_id for owner_id in current_owned
            if task_worktree_id is not None
            if owner_id in session_by_id
            and session_by_id[owner_id]["worktree_id"] == task_worktree_id
            and owner_id != session_id
        }
        if foreign_task_worktree_owners:
            raise CleanupError("another active Session owns the subject Task worktree")
        if history_operation is not None and session_id in {
            row["session_id"] for row in closure
        }:
            raise CleanupError("historical operator Session is inside the frozen victim closure")
        expected_owner_ids = sorted(
            session_id for session_id in current_owned
            if session_id in {row["session_id"] for row in closure}
            and next(row for row in closure if row["session_id"] == session_id)["disposition"] == "remove"
        )
        if history_operation is None:
            release_request = _closed(
                request["release"], {"prepared_by_session_id", "owner_session_ids"},
                "preparation release proof",
            )
            if (
                release_request["prepared_by_session_id"] != session_id
                or release_request["owner_session_ids"] != expected_owner_ids
            ):
                raise CleanupError("release owner list does not match fresh native ownership")
        release = {
            "prepared_by_session_id": session_id,
            "owner_session_ids": expected_owner_ids,
            "release_sha256": hashlib.sha256(canonical_bytes({
                "project_id": project_id, "task_id": task_id,
                "workflow_id": workflow_id, "owner_session_ids": expected_owner_ids,
                "operator_task_id": operator_task_id,
                "history_operation": history_operation,
                "current_nodes_sha256": hashlib.sha256(
                    canonical_bytes(snapshot["current_nodes"])
                ).hexdigest(),
                "supervision": memberships,
            })).hexdigest(),
        }
        _validate_release(release, require_preparer_owner=history_operation is None)
        worktrees = list(task_worktree_inventory)
        group_worktree_dispositions: dict[str, tuple[bool, str]] = {}
        for membership in memberships:
            held_group = bool(unsettled.intersection(membership["task_ids"]))
            group_roots = [
                membership["supervisor_session_id"],
                *membership["reused_root_session_ids"],
            ]
            group_sessions = set().union(
                *(descendants_by_root[root_id] for root_id in group_roots)
            )
            for group_session_id in sorted(group_sessions):
                session = session_by_id.get(group_session_id)
                if session is None or session["worktree_id"] is None:
                    continue
                worktree_id = session["worktree_id"]
                if worktree_id == task["worktree_id"]:
                    continue
                owner_task_id = (
                    session["task_id"]
                    if session["task_id"] in membership["task_ids"]
                    else membership["task_ids"][0]
                )
                previous = group_worktree_dispositions.get(worktree_id)
                group_worktree_dispositions[worktree_id] = (
                    held_group or worktree_id in active_worktree_ids
                    or (previous[0] if previous else False),
                    previous[1] if previous else owner_task_id,
                )
        if group_worktree_dispositions:
            native_rows, _ = self._native_paths(
                list(group_worktree_dispositions), project_id=project_id,
            )
            native_by_id = {row["worktree_id"]: row for row in native_rows}
            for worktree_id in sorted(group_worktree_dispositions):
                native = native_by_id[worktree_id]
                path = Path(native["path"])
                _check_path_components(path)
                branch = self._git("-C", str(path), "symbolic-ref", "--short", "HEAD")
                head = self._git("-C", str(path), "rev-parse", "--verify", "HEAD")
                branch_ref = self._git(
                    "-C", str(primary), "rev-parse", "--verify", f"refs/heads/{branch}",
                )
                self._verify_git_worktree({
                    "path": str(path), "branch": branch, "head_oid": head,
                    "branch_oid": branch_ref,
                }, expect_present=True)
                held, owner_task_id = group_worktree_dispositions[worktree_id]
                worktrees.append({
                    "project_id": project_id, "workspace_id": native["workspace_id"],
                    "worktree_id": worktree_id, "path": str(path),
                    "branch": branch, "head_oid": head, "branch_oid": branch_ref,
                    "role": "supervisor",
                    "disposition": "shared-in-use" if held else "remove",
                    "owner_task_id": owner_task_id,
                })
        protected_roots = [
            {"path": str(primary), "kind": "primary-workspace"},
            {"path": str(Path(self.context["persistence_root"])), "kind": "native-persistence"},
        ]
        profile_path = primary / ".kent/workflow-profile.toml"
        try:
            profile = tomllib.loads(profile_path.read_text(encoding="utf-8"))
            memory_root = profile["project_memory"]["root"]
        except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
            raise CleanupError("R2 root is unavailable from the primary Project profile") from error
        if history_operation is None:
            self._bind_memory_adapter(primary)
        protected_roots.append({
            "path": str(_clean_path(memory_root, "R2 root")), "kind": "r2-storage",
        })
        self._protected_roots = [Path(item["path"]) for item in protected_roots]
        if subject_path is not None and any(
            _overlap_path(root, subject_path)
            and not (
                protected["kind"] == "native-persistence"
                and _native_worktree_path(str(subject_path), str(root))
            )
            for protected, root in zip(protected_roots, self._protected_roots)
        ):
            raise CleanupError("task worktree overlaps a protected root")
        inventory_value = {
            "schema": "merged-cleanup-inventory-v1",
            "project_id": project_id, "task_id": task_id, "workflow_id": workflow_id,
            "subject": subject, "pull_request": pr,
            "policy": policy, "policy_provenance": policy_provenance,
            "cleanup_context_sha256": hashlib.sha256(cleanup_context_bytes).hexdigest(),
            "history_operation": (
                dict(history_operation) if history_operation is not None else None
            ),
            "release": release, "supervision": memberships, "sessions": closure,
            "member_outcomes": member_outcomes, "worktrees": worktrees,
            "owned_containers": self._freeze_owned_containers(
                request.get("owned_containers", []), [task_id],
            ),
            "protected_roots": protected_roots,
        }
        self._check_foreign_session_collisions(inventory_value, snapshot)
        self._check_owned_container_scope(inventory_value)
        from .cleanup import validate_inventory
        inventory = validate_inventory(inventory_value)
        if history_operation is not None:
            return inventory
        serialized = canonical_bytes(inventory.value)
        self._memory_call("artifact-write", body=serialized)
        _, readback = self._memory_call(
            "artifact-read", digest=inventory.sha256,
        )
        if readback != serialized:
            raise CleanupError("R2 inventory attachment readback bytes differ")
        return inventory

    @staticmethod
    def _is_descendant(
        session_id: str, root_id: str, rows: Sequence[Mapping[str, Any]],
    ) -> bool:
        parents = {row["session_id"]: row["parent_agent_session_id"] for row in rows}
        current = session_id
        seen = set()
        while current in parents and current not in seen:
            seen.add(current)
            current = parents[current]
            if current == root_id:
                return True
        return False

    def register_supervisor(self, request: Any) -> dict[str, Any]:
        fields = {
            "schema", "task_ids", "reused_root_session_ids",
        }
        request = _closed(request, fields, "supervisor registration request")
        if request["schema"] != "merged-cleanup-register-request-v1":
            raise CleanupError("supervisor registration schema differs")
        task_ids = request["task_ids"]
        reused = request["reused_root_session_ids"]
        if (
            not isinstance(task_ids, list) or not task_ids
            or len(task_ids) > _MAX_MEMBER_TASKS
            or not isinstance(reused, list) or len(reused) > MAX_SESSIONS
        ):
            raise CleanupError("supervisor membership exceeds its bounds")
        for task_id in task_ids:
            _uuid4(task_id, _TASK_ID, "supervisor member Task")
        for session_id in reused:
            _uuid4(session_id, _SESSION_ID, "reused helper Session")
        if task_ids != sorted(task_ids) or reused != sorted(reused):
            raise CleanupError("supervisor membership must be sorted")
        if len(set(task_ids)) != len(task_ids) or len(set(reused)) != len(reused):
            raise CleanupError("supervisor membership contains duplicates")
        supervisor_id, session_row = self._current_agent_session()
        project_id = session_row["project_id"]
        _uuid4(project_id, _PROJECT_ID, "supervisor project")
        record = {
            "schema": "merged-cleanup-supervision-v1",
            "project_id": project_id,
            "supervisor_session_id": supervisor_id,
            "task_ids": task_ids,
            "reused_root_session_ids": reused,
        }
        serialized = supervision_comment(record)
        snapshot = self._snapshot_once(task_ids)
        task_rows = {row["task_id"]: row for row in snapshot["tasks"]}
        if set(task_rows) != set(task_ids) or any(
            row["project_id"] != project_id for row in task_rows.values()
        ):
            raise CleanupError("supervisor member Tasks do not share the current Project")
        for reused_id in reused:
            matches = [row for row in snapshot["sessions"] if row["session_id"] == reused_id]
            if len(matches) != 1 or matches[0]["project_id"] != project_id:
                raise CleanupError("reused helper root is not a native same-Project Session")
        for task_id in task_ids:
            existing = [
                comment for comment in self._task_comment_records(task_id)
                if comment["author_kind"] == "agent" and comment["body"] == serialized
            ]
            if not existing:
                code, _output, error = self.runner([
                    "task", "comment", "add", task_id,
                    "--project", project_id, "--author", "agent", "--body", serialized,
                ], cwd=self.workspace, timeout=20.0, output_limit=16_384)
                if code:
                    raise CleanupError(
                        "native supervisor Task comment append failed: "
                        + error.decode("utf-8", "replace")[:512],
                    )
            self._snapshots = {}
            comments = self._task_comment_records(task_id)
            if not any(
                comment["author_kind"] == "agent" and comment["body"] == serialized
                for comment in comments
            ):
                raise CleanupError("supervisor membership comment readback differs")
        return {
            "schema": "merged-cleanup-supervision-result-v1",
            "project_id": project_id,
            "supervisor_session_id": supervisor_id,
            "task_ids": task_ids,
            "reused_root_session_ids": reused,
        }

    def _verify_history_operator(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        operator_session_id, session_row = self._current_agent_session()
        if (
            operator_session_id != operation["operator_session_id"]
            or session_row["task_id"] != operation["operator_task_id"]
            or session_row["project_id"] != operation["operator_project_id"]
        ):
            raise CleanupError("historical operation native operator identity changed")
        return {
            "project_id": session_row["project_id"],
            "task_id": session_row["task_id"],
            "session_id": operator_session_id,
        }

    def _all_native_workspace_roots(self) -> list[Path]:
        """Read the bounded native workspace/worktree path catalog for collision checks."""
        operations.validate_native_local_context(self.context)
        database = Path(self.context["persistence_root"]) / "db/main.sqlite3"
        before = database.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise CleanupError("native persistence database identity is unsafe")
        deadline = time.monotonic() + _SNAPSHOT_SECONDS
        connection = None
        roots: list[Path] = []
        try:
            connection = sqlite3.connect(
                database.as_uri() + "?mode=ro", uri=True,
                timeout=0.25, isolation_level=None,
            )
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 65_536)
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=250")
            connection.execute("BEGIN")
            actual = [
                row[2] for row in connection.execute("PRAGMA database_list")
                if row[1] == "main"
            ]
            if (
                connection.execute("PRAGMA query_only").fetchone() != (1,)
                or actual != [str(database)]
            ):
                raise CleanupError("native root catalog is not read-only and bound")
            queries = (
                "SELECT canonical_root_path FROM workspaces "
                "ORDER BY id LIMIT ?",
                "SELECT wt.canonical_root_path FROM worktrees wt "
                "JOIN workspaces w ON w.id=wt.workspace_id "
                "ORDER BY wt.id LIMIT ?",
            )
            total_bytes = 0
            for query in queries:
                rows = connection.execute(query, (_MAX_NATIVE_SESSIONS + 1,)).fetchall()
                if len(rows) > _MAX_NATIVE_SESSIONS:
                    raise CleanupError("native workspace/worktree catalog exceeds its bound")
                for (raw_path,) in rows:
                    if not isinstance(raw_path, str):
                        raise CleanupError("native workspace root is malformed")
                    path = _clean_path(raw_path, "native registered root")
                    _check_path_components(path, allow_missing_tree=True)
                    roots.append(path)
                    total_bytes += len(raw_path.encode("utf-8"))
                    if (
                        total_bytes > _MAX_SNAPSHOT_BYTES
                        or time.monotonic() > deadline
                    ):
                        raise CleanupError("native root catalog exceeds its byte/time bound")
        except (sqlite3.Error, OSError, TypeError, ValueError) as error:
            if isinstance(error, CleanupError):
                raise
            raise CleanupError("native workspace/worktree root catalog is unavailable") from error
        finally:
            if connection is not None:
                connection.close()
        after = database.lstat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise CleanupError("native persistence database changed during root observation")
        operations.validate_native_local_context(self.context)
        return roots

    def _profile_memory_roots(self, roots: Sequence[Path]) -> list[Path]:
        protected: set[Path] = set()
        deadline = time.monotonic() + _SNAPSHOT_SECONDS
        total_bytes = 0
        for root in sorted(set(roots)):
            profile_path = root / ".kent/workflow-profile.toml"
            info = _check_path_components(profile_path, allow_missing_tree=True)
            if info is None:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65_536:
                raise CleanupError("native Project profile is unsafe for collision checking")
            descriptor = -1
            try:
                descriptor = os.open(
                    profile_path,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                )
                before = os.fstat(descriptor)
                raw = os.read(descriptor, 65_537)
                after = os.fstat(descriptor)
                if (
                    len(raw) > 65_536
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or len(raw) != after.st_size
                ):
                    raise CleanupError("native Project profile changed during observation")
                total_bytes += len(raw)
                if total_bytes > _MAX_SNAPSHOT_BYTES:
                    raise CleanupError("native Project profiles exceed the byte bound")
                parsed = tomllib.loads(raw.decode("utf-8"))
            except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
                raise CleanupError("native Project profile cannot be inspected safely") from error
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            memory = parsed.get("project_memory", {})
            if not isinstance(memory, dict):
                raise CleanupError("native Project memory profile is malformed")
            memory_path = memory.get("root")
            if memory_path is not None:
                if not isinstance(memory_path, str):
                    raise CleanupError("native Project memory root is malformed")
                protected.add(_clean_path(memory_path, "native Project memory root"))
            if time.monotonic() > deadline:
                raise CleanupError("native Project profile scan exceeded its time bound")
        return sorted(protected)

    def _catalog_session_path(self, row: Mapping[str, Any]) -> Path:
        """Protect the actual native path, including legacy project reassignment.

        This does not admit the row for removal. Disposable rows still require
        strict current project/Session identity through ``_session_path``.
        """
        relative = row.get("artifact_relpath")
        parts = relative.split("/") if isinstance(relative, str) else []
        if (
            len(parts) != 4 or parts[0] != "projects" or parts[2] != "sessions"
            or _PROJECT_ID.fullmatch(parts[1]) is None
            or _SESSION_ID.fullmatch(parts[3]) is None
        ):
            raise CleanupError("native catalog Session exclusion path is unsafe")
        return Path(self.context["persistence_root"]) / relative

    def _verify_absent_historical_worktree(
        self, row: Mapping[str, Any], observed: Sequence[Mapping[str, Any]], *, primary: Path,
    ) -> None:
        registered = [item for item in observed if item["worktree_id"] == row["worktree_id"]]
        if registered and registered != [{
            "worktree_id": row["worktree_id"],
            "workspace_id": row["workspace_id"], "path": row["path"],
        }]:
            raise CleanupError("historical missing worktree native identity changed")
        if row["path"] is not None:
            if _check_path_components(Path(row["path"]), allow_missing_tree=True) is not None:
                raise CleanupError("historical absent worktree path reappeared")
            output = self._git("-C", str(primary), "worktree", "list", "--porcelain")
            if f"worktree {row['path']}" in output.splitlines():
                raise CleanupError("historical absent worktree remains registered in Git")

    def _check_foreign_session_collisions(
        self, inventory: Mapping[str, Any], snapshot: Mapping[str, Any],
    ) -> None:
        selected = {row["session_id"] for row in inventory["sessions"]}
        victims = [
            self._session_path(row)[0] for row in inventory["sessions"]
            if row["disposition"] == "remove"
        ] + [
            Path(row["path"]) for row in (
                *inventory["owned_containers"], *inventory["worktrees"],
            ) if row["disposition"] == "remove" and row["path"] is not None
        ]
        victim_paths = {str(path) for path in victims}
        ordered_victims = sorted(victim_paths)
        for row in snapshot["sessions"]:
            if row["session_id"] in selected:
                continue
            path = self._catalog_session_path(row)
            # Resolve only for exclusion: a foreign alias into a victim remains
            # protected; it never becomes a candidate for deletion.
            try:
                aliases = {path, path.resolve()}
            except (OSError, RuntimeError) as error:
                raise CleanupError("native catalog Session exclusion is ambiguous") from error
            for alias in aliases:
                prefix = str(alias) + "/"
                index = bisect_left(ordered_victims, prefix)
                if (
                    str(alias) in victim_paths
                    or any(str(parent) in victim_paths for parent in alias.parents)
                    or (index < len(ordered_victims)
                        and ordered_victims[index].startswith(prefix))
                ):
                    raise CleanupError("disposable resource overlaps a foreign native Session path")

    @staticmethod
    def _freeze_owned_containers(items: Any, task_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not isinstance(items, list) or len(items) > MAX_CONTAINERS:
            raise CleanupError("owned artifact selection is oversized")
        frozen = []
        for item in items:
            item = _closed(
                item, {"path", "kind", "owner_task_id", "evidence_reference"},
                "explicit owned sibling artifact",
            )
            path = _clean_path(item["path"], "owned artifact path")
            reference = item["evidence_reference"]
            if (
                item["owner_task_id"] not in task_ids
                or item["kind"] not in {"archive", "evidence"}
                or not isinstance(reference, str) or not reference
                or len(reference.encode("utf-8")) > 4096
            ):
                raise CleanupError("owned artifact kind, owner, or evidence reference is invalid")
            info = _check_path_components(path)
            parent = _check_path_components(path.parent)
            if (
                info is None or parent is None or not stat.S_ISDIR(parent.st_mode)
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)
            ):
                raise CleanupError("owned artifact is not one exact disposable file or directory")
            frozen.append({
                **item, "path": str(path), "device": info.st_dev, "inode": info.st_ino,
                "parent_device": parent.st_dev, "parent_inode": parent.st_ino,
                "disposition": "remove",
            })
        if len({row["path"] for row in frozen}) != len(frozen):
            raise CleanupError("owned artifact selection contains duplicate paths")
        return frozen

    def _check_owned_container_scope(self, inventory: Mapping[str, Any]) -> None:
        resources = inventory["owned_containers"]
        if not resources:
            self._owned_container_paths = set()
            return
        persistence = Path(self.context["persistence_root"])
        allowed_parents = {
            Path(row["path"]).parent for row in inventory["worktrees"]
            if row["role"] == "task" and row["path"] is not None
        }
        allowed_parents.update(
            (persistence / row["artifact_relpath"]).parent
            for row in inventory["sessions"]
            if row["project_id"] == inventory["project_id"]
            and row["task_id"] == inventory["task_id"]
        )
        native_roots = self._all_native_workspace_roots()
        protected = [
            self.workspace, self.kent, self.kent.parent, *native_roots,
            *self._profile_memory_roots(native_roots),
            *[
                Path(row["path"]) for row in inventory["protected_roots"]
                if row["kind"] != "native-persistence"
            ],
            *[
                self._catalog_session_path(row)
                for row in self._snapshot_once()["sessions"]
            ],
        ]
        for row in resources:
            path = Path(row["path"])
            if row["owner_task_id"] != inventory["task_id"]:
                raise CleanupError("owned artifact belongs to another Task")
            if (
                (inventory["history_operation"] is None or _overlap_path(path, persistence))
                and path.parent not in allowed_parents
            ):
                raise CleanupError("owned artifact is outside its native-derived sibling container")
            if any(_overlap_path(path, root) for root in protected):
                raise CleanupError("owned artifact collides with native or protected storage")
        self._owned_container_paths = {row["path"] for row in resources}

    def inspect_history(self, request: Any):
        if not isinstance(request, dict) or set(request) not in (
            {"schema", "project_id", "task_ids", "permission_evidence"},
            {"schema", "project_id", "task_ids", "permission_evidence", "owned_containers"},
        ):
            raise CleanupError("historical cleanup inspection request fields are invalid")
        if request["schema"] != "merged-cleanup-history-request-v1":
            raise CleanupError("historical cleanup request schema differs")
        project_id = _uuid4(request["project_id"], _PROJECT_ID, "historical Project")
        task_ids = request["task_ids"]
        if not isinstance(task_ids, list) or not task_ids or len(task_ids) > _MAX_MEMBER_TASKS:
            raise CleanupError("historical Task selection is empty or oversized")
        for task_id in task_ids:
            _uuid4(task_id, _TASK_ID, "historical Task")
        if task_ids != sorted(task_ids) or len(set(task_ids)) != len(task_ids):
            raise CleanupError("historical Task selection must be unique and sorted")
        normalized_requested = self._freeze_owned_containers(
            request.get("owned_containers", []), task_ids,
        )
        permission_evidence = _closed(
            request["permission_evidence"], {"reference", "context"},
            "original historical permission evidence",
        )
        for key, maximum in (("reference", 4096), ("context", 16_384)):
            if not isinstance(permission_evidence[key], str) or (
                not permission_evidence[key]
                or len(permission_evidence[key].encode("utf-8")) > maximum
            ):
                raise CleanupError(f"historical permission evidence {key} is invalid")
        operator_session_id, operator_row = self._current_agent_session()
        if (
            operator_row["project_id"] is None
            or (
                operator_row["task_id"] is not None
                and (
                    not _TASK_ID.fullmatch(operator_row["task_id"])
                    or operator_row["task_id"] in task_ids
                )
            )
        ):
            raise CleanupError("historical operator native Session identity is invalid")
        operator_project_id = _uuid4(
            operator_row["project_id"], _PROJECT_ID, "historical operator Project",
        )
        operator_task_id = operator_row["task_id"]
        operation = {
            "schema": "merged-cleanup-history-operation-v1",
            "project_id": project_id,
            "operator_project_id": operator_project_id,
            "operator_task_id": operator_task_id,
            "operator_session_id": operator_session_id,
            "selected_task_ids": task_ids,
            "permission_evidence": permission_evidence,
        }
        self._verify_history_operator(operation)
        inventories = []
        for task_id in task_ids:
            inventory = self._prepare_inventory(
                {"schema": "merged-cleanup-prepare-request-v1"},
                history_operation=operation, subject_task_id=task_id,
            )
            inventories.append(inventory.value)
        if normalized_requested:
            for resource in normalized_requested:
                owner_inventory = next(
                    item for item in inventories
                    if item["task_id"] == resource["owner_task_id"]
                )
                owner_inventory["owned_containers"].append(resource)
            for inventory in inventories:
                self._check_owned_container_scope(inventory)
            inventories = [
                __import__("workflowkit.cleanup", fromlist=["validate_inventory"])
                .validate_inventory(item).value
                for item in inventories
            ]
        finite = validate_history_inventory({
            "schema": "merged-cleanup-history-inventory-v1",
            "project_id": project_id,
            "operator_project_id": operator_project_id,
            "task_ids": task_ids,
            "operator_task_id": operator_task_id,
            "operator_session_id": operator_session_id,
            "permission_evidence": permission_evidence,
            "inventories": inventories,
        })
        operator_workspace = self.workspace
        if any(
            _overlap_path(operator_workspace, worktree["path"])
            for inventory in finite.inventories
            for worktree in inventory.value["worktrees"]
            if worktree["disposition"] == "remove"
        ):
            raise CleanupError("historical operator Workspace overlaps a victim root")
        return finite

    def load_handoff(self, handoff: Mapping[str, Any], native_context: Any):
        parsed = validate_handoff(dict(handoff))
        if not isinstance(native_context, dict):
            raise CleanupError("native Script identity is absent")
        identity = self._script_identity(native_context)
        if (
            identity["task_id"] != parsed["task_id"]
            or identity["project_id"] != parsed["project_id"]
        ):
            raise CleanupError("prepared handoff does not bind the current Task")
        task_detail = self._detail
        source = task_detail.get("source_workspace") if isinstance(task_detail, dict) else None
        if not isinstance(source, dict) or not isinstance(source.get("root_path"), str):
            raise CleanupError("protected primary workspace is unavailable")
        primary = _clean_path(source["root_path"], "protected primary")
        self._bind_memory_adapter(primary)
        _, raw = self._memory_call(
            "artifact-read", native_context=native_context,
            digest=parsed["inventory"]["sha256"],
        )
        if len(raw) != parsed["inventory"]["byte_size"]:
            raise CleanupError("prepared inventory size differs from its handoff")
        inventory = inventory_from_bytes(raw, parsed["inventory"]["sha256"])
        value = inventory.value
        if (
            value["project_id"] != parsed["project_id"]
            or value["task_id"] != parsed["task_id"]
            or value["workflow_id"] != parsed["workflow_id"]
        ):
            raise CleanupError("prepared inventory identity differs from its handoff")
        if identity["node_id"] != value["policy_provenance"]["entry_node_id"]:
            raise CleanupError("native Script is not the selected delivered route entry")
        graph_response = self._graph
        graph_body = graph_response.get("graph") if isinstance(graph_response, dict) else None
        if not isinstance(graph_body, dict):
            raise CleanupError("native Script Workflow graph is unavailable")
        current_policy, current_provenance = _validate_delivered_policy_graph(
            {**graph_body, "workflow_id": graph_response.get("workflow_id")},
            project_id=value["project_id"], workflow_id=value["workflow_id"],
            entrypoint=Path(sys.argv[0]),
        )
        if (
            current_policy != value["policy"]
            or current_provenance != value["policy_provenance"]
        ):
            raise CleanupError("prepared inventory policy or native route provenance changed")
        if any(
            row["disposition"] == "remove" and row["path"] is not None
            and _overlap_path(primary, row["path"])
            for row in value["worktrees"]
        ):
            raise CleanupError("protected primary overlaps a disposable worktree")
        os.chdir(primary)
        if Path.cwd().resolve() != primary:
            raise CleanupError("real Script cwd did not leave the retiring worktree")
        self.workspace = primary
        self.context = operations.native_local_context(
            kent=str(self.kent), project_root=str(primary),
        )
        operations.validate_native_local_context(self.context)
        self._protected_roots = [
            Path(row["path"]) for row in value["protected_roots"]
        ]
        return inventory

    @staticmethod
    def _selected_cleanup_session_ids(
        snapshot: Mapping[str, Any], inventory: Mapping[str, Any],
    ) -> set[str]:
        selected_tasks = {inventory["task_id"]}
        for membership in inventory["supervision"]:
            if inventory["task_id"] in membership["task_ids"]:
                selected_tasks.update(membership["task_ids"])
        rows = snapshot["sessions"]
        rows_by_id = {row["session_id"]: row for row in rows}
        if len(rows_by_id) != len(rows):
            raise CleanupError("fresh native Session identities are ambiguous")
        children: dict[str, list[str]] = {}
        for row in rows:
            parent = row["parent_agent_session_id"]
            if parent is not None:
                children.setdefault(parent, []).append(row["session_id"])
        pending = {
            row["session_id"] for row in inventory["sessions"]
        }
        pending.update(
            row["session_id"] for row in rows
            if row.get("task_id") == inventory["task_id"]
        )
        pending.update(
            row["session_id"] for row in snapshot["current_nodes"]
            if row["task_id"] in selected_tasks
            and isinstance(row["session_id"], str)
        )
        traversed: set[str] = set()
        observed: set[str] = set()
        while pending:
            session_id = pending.pop()
            if session_id in traversed:
                continue
            traversed.add(session_id)
            if len(traversed) > MAX_SESSIONS:
                raise CleanupError("fresh Session closure exceeds its bound")
            if session_id in rows_by_id:
                observed.add(session_id)
            pending.update(children.get(session_id, ()))
        return observed

    def revalidate(self, inventory: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
        operations.validate_native_local_context(self.context)
        if self._receipt_inventory is not None:
            if inventory != self._receipt_inventory.value:
                raise CleanupError("executor inventory differs from its receipt binding")
            self._load_receipts()
        primary_roots = [
            row for row in inventory["protected_roots"]
            if row["kind"] == "primary-workspace"
        ]
        if len(primary_roots) != 1:
            raise CleanupError("inventory has no unique protected primary workspace")
        self._active_primary_path = _clean_path(
            primary_roots[0]["path"], "protected primary workspace",
        )
        _check_path_components(self._active_primary_path)
        self._protected_roots = [
            Path(row["path"]) for row in inventory["protected_roots"]
        ]
        self._snapshot = None
        self._snapshots = {}
        project_id, task_id = inventory["project_id"], inventory["task_id"]
        history_operation = inventory["history_operation"]
        self._check_owned_container_scope(inventory)
        if history_operation is None:
            if self._script_native_context is None:
                raise CleanupError("fresh current Script context is unavailable")
            identity = self._script_identity(self._script_native_context)
            if (
                identity.get("task_id") != task_id
                or identity.get("project_id") != project_id
                or identity.get("node_id")
                != inventory["policy_provenance"]["entry_node_id"]
            ):
                raise CleanupError("fresh current Script binding differs from inventory")
            graph_response = _json_object(
                self._read([
                    "workflow", "graph", "inspect", inventory["workflow_id"], "--json",
                ]),
                "fresh cleanup Workflow graph",
            )
            graph_body = graph_response.get("graph")
            if not isinstance(graph_body, dict):
                raise CleanupError("fresh cleanup Workflow graph is incomplete")
            fresh_policy, fresh_policy_provenance = _validate_delivered_policy_graph(
                {**graph_body, "workflow_id": graph_response.get("workflow_id")},
                project_id=project_id, workflow_id=inventory["workflow_id"],
                entrypoint=Path(sys.argv[0]),
            )
            if (
                fresh_policy != inventory["policy"]
                or fresh_policy_provenance != inventory["policy_provenance"]
            ):
                raise CleanupError("native merged cleanup policy or route changed")
            if self._identity is not None and (
                self._identity.get("node_id") != inventory["policy_provenance"]["entry_node_id"]
            ):
                raise CleanupError("current final Script differs from the frozen policy entry")
        else:
            self._verify_history_operator(history_operation)
            if task_id not in history_operation["selected_task_ids"]:
                raise CleanupError("historical subject escaped the frozen exact selection")
        selected_tasks = {task_id}
        for membership in inventory["supervision"]:
            if task_id in membership["task_ids"]:
                selected_tasks.update(membership["task_ids"])
        query_tasks = set(selected_tasks)
        snapshot = self._snapshot_once(sorted(query_tasks))
        if any(
            row["session_id"] in getattr(self, "_session_settlements", {})
            for row in snapshot["sessions"]
        ):
            raise CleanupError("a durably settled Session acquired native metadata again")
        task_rows = {row["task_id"]: row for row in snapshot["tasks"]}
        if set(task_rows) != query_tasks:
            raise CleanupError("fresh native Task membership differs from the inventory")
        if any(
            row["project_id"] != project_id
            or row["workflow_id"] != inventory["workflow_id"]
            for row in task_rows.values()
        ):
            raise CleanupError("fresh native Task Project or Workflow differs from inventory")
        subject_task_row = task_rows[task_id]
        if (
            subject_task_row["workspace_id"] != inventory["subject"]["workspace_id"]
            or (
                subject_task_row["worktree_id"] != inventory["subject"]["worktree_id"]
                and not (
                    history_operation is not None
                    and subject_task_row["worktree_id"] is None
                )
                and not (
                    inventory["subject"]["worktree_id"] in self._worktree_settlements
                    and subject_task_row["worktree_id"] is None
                )
            )
        ):
            raise CleanupError("fresh native source workspace or worktree binding differs")
        fresh_detail = self._native_task(task_id, project_id)
        fresh_summary = fresh_detail.get("summary")
        if (
            not isinstance(fresh_summary, dict)
            or fresh_summary.get("workflow_id") != inventory["workflow_id"]
        ):
            raise CleanupError("fresh native Task summary Workflow differs")
        if history_operation is None:
            source = self._detail.get("source_workspace") if isinstance(self._detail, dict) else None
            if (
                not isinstance(source, dict)
                or source.get("workspace_id") != inventory["subject"]["workspace_id"]
                or source.get("root_path") != str(self._active_primary_path)
            ):
                raise CleanupError("fresh native Script source workspace differs")
        expected_memberships = {
            canonical_bytes(dict(row)) for row in inventory["supervision"]
        }
        fresh_memberships = {
            canonical_bytes(row) for row in self._supervision_records(task_id)
        }
        if fresh_memberships != expected_memberships:
            raise CleanupError("fresh native supervisor membership differs")
        for membership in inventory["supervision"]:
            for member_id in membership["task_ids"]:
                if not any(
                    comment["author_kind"] == "agent"
                    and comment["body"] == supervision_comment(membership)
                    for comment in self._task_comment_records(member_id)
                ):
                    raise CleanupError("registered supervisor membership comment is missing")

        frozen_outcomes = {
            row["task_id"]: row for row in inventory["member_outcomes"]
        }
        fresh_outcomes = {}
        for member_id in sorted(frozen_outcomes):
            if member_id == task_id and history_operation is None:
                fresh_outcomes[member_id] = frozen_outcomes[member_id]
                continue
            fresh_outcomes[member_id] = self._task_outcome(
                member_id, project_id, current_task_id=task_id,
                current_pr=inventory["pull_request"],
                current_disposition=history_operation is None,
            )
        fresh_unsettled = {
            member_id for member_id, outcome in fresh_outcomes.items()
            if outcome["native_state"] == "active"
            or outcome["outcome"] not in {"merged", "no-pr", "non-delivery"}
        }
        for membership in inventory["supervision"]:
            if not fresh_unsettled.intersection(membership["task_ids"]):
                continue
            root_ids = {
                membership["supervisor_session_id"],
                *membership["reused_root_session_ids"],
            }
            if any(
                root_ids.intersection(row["supervisor_root_ids"])
                and row["disposition"] != "shared-in-use"
                for row in inventory["sessions"]
            ):
                raise CleanupError("a supervisor Task became unsettled after inventory freeze")
            if any(
                row["role"] == "supervisor"
                and row["owner_task_id"] in membership["task_ids"]
                and row["disposition"] != "shared-in-use"
                for row in inventory["worktrees"]
            ):
                raise CleanupError("a supervisor worktree became shared-in-use")
        worktree_ids = [row["worktree_id"] for row in inventory["worktrees"]]
        observed_worktrees, _observed_workspaces = self._native_paths(
            worktree_ids, project_id=project_id, allow_missing=True,
        )
        observed_worktree_ids = {row["worktree_id"] for row in observed_worktrees}
        for worktree in inventory["worktrees"]:
            if worktree["disposition"] == "absent":
                self._verify_absent_historical_worktree(
                    worktree, observed_worktrees, primary=self._active_primary_path,
                )
                continue
            if worktree["worktree_id"] in observed_worktree_ids:
                continue
            if worktree["disposition"] != "remove":
                raise CleanupError("native worktree disappeared before its deletion settled")
            if worktree["worktree_id"] not in self._worktree_settlements:
                if self._receipt_inventory is not None:
                    raise CleanupError("missing native worktree has no durable settlement receipt")
                path_info = _check_path_components(
                    Path(worktree["path"]), allow_missing_leaf=True,
                )
                if path_info is not None:
                    raise CleanupError("missing native worktree still has a filesystem path")
                self._verify_git_worktree(worktree, expect_present=False)
                retained = self._git(
                    "-C", str(self._primary_path()), "rev-parse", "--verify",
                    f"refs/heads/{worktree['branch']}",
                )
                if retained != worktree["branch_oid"]:
                    raise CleanupError("missing native worktree branch ref did not settle")
                self._worktree_settlements.add(worktree["worktree_id"])
        snapshot["worktrees"] = observed_worktrees
        current_session_ids = {
            row["session_id"] for row in snapshot["current_nodes"]
            if isinstance(row["session_id"], str)
        }
        expected_owners = set(inventory["release"]["owner_session_ids"])
        if current_session_ids.intersection(expected_owners):
            raise CleanupError("a released Session still owns a native current node")
        removable_session_ids = {
            row["session_id"] for row in inventory["sessions"]
            if row["disposition"] == "remove"
        }
        if current_session_ids.intersection(removable_session_ids):
            raise CleanupError("a disposable Session acquired a current native owner")
        current_worktree_owners = {
            row["session_id"]: row["worktree_id"]
            for row in snapshot["sessions"]
            if row["session_id"] in current_session_ids
        }
        removable_worktree_ids = {
            row["worktree_id"] for row in inventory["worktrees"]
            if row["disposition"] == "remove"
        }
        if removable_worktree_ids.intersection(current_worktree_owners.values()):
            raise CleanupError("a disposable worktree acquired a current native owner")
        if self._identity is not None and (
            self._identity.get("task_id") != task_id
            or self._identity.get("project_id") != project_id
            or Path.cwd() in [
                Path(row["path"]) for row in inventory["worktrees"]
                if row["path"] is not None
            ]
        ):
            raise CleanupError("final Script identity or actual cwd overlaps the subject")

        self._check_foreign_session_collisions(inventory, snapshot)
        inventory_ids = {row["session_id"] for row in inventory["sessions"]}
        selected_fresh = self._selected_cleanup_session_ids(snapshot, inventory)
        rows_by_id: dict[str, dict[str, Any]] = {}
        for raw in snapshot["sessions"]:
            if raw["session_id"] not in selected_fresh:
                continue
            path, info = self._session_path(raw)
            rows_by_id[raw["session_id"]] = {
                **raw,
                "device": info.st_dev if info is not None else None,
                "inode": info.st_ino if info is not None else None,
            }
        if selected_fresh - inventory_ids:
            raise CleanupError("a new native Session appeared after inventory freeze")
        shared_session_ids = {
            row["session_id"] for row in inventory["sessions"]
            if row["disposition"] == "shared-in-use"
        }
        if shared_session_ids - selected_fresh:
            raise CleanupError("a retained shared Session disappeared after inventory freeze")
        for session_id in selected_fresh & inventory_ids:
            fresh = rows_by_id[session_id]
            frozen = next(row for row in inventory["sessions"]
                          if row["session_id"] == session_id)
            for key in (
                "project_id", "task_id", "parent_agent_session_id",
                "previous_session_id", "workspace_id", "worktree_id",
                "artifact_relpath",
            ):
                if fresh[key] != frozen[key]:
                    raise CleanupError("fresh native Session identity differs from inventory")
            if frozen["device"] is not None and fresh["device"] is not None and (
                fresh["device"], fresh["inode"]
            ) != (frozen["device"], frozen["inode"]):
                raise CleanupError("native Session container was substituted")
        primary = self._primary_path()
        for protected in inventory["protected_roots"]:
            protected_path = Path(protected["path"])
            if protected_path.exists() or protected_path.is_symlink():
                checked = _check_path_components(protected_path)
                if checked is None:
                    raise CleanupError("protected path observation is incomplete")
            if any(
                _overlap_path(protected_path, worktree["path"])
                for worktree in inventory["worktrees"]
                if worktree["disposition"] == "remove"
                and worktree["path"] is not None
                and not (
                    protected["kind"] == "native-persistence"
                    and _native_worktree_path(
                        worktree["path"], protected["path"],
                    )
                )
            ):
                raise CleanupError("fresh protected root overlaps a disposable worktree")
        if any(
            _overlap_path(primary, row["path"]) for row in inventory["worktrees"]
            if row["disposition"] == "remove" and row["path"] is not None
        ):
            raise CleanupError("protected primary overlaps a disposable worktree")

        fresh_pr = self._provider_pr(inventory["pull_request"]["url"])
        if not self._same_provider_pr_identity(
            inventory["pull_request"], fresh_pr,
        ):
            raise CleanupError("fresh provider PR facts differ from frozen inventory")

        # Verify native worktree registrations and exact current heads before
        # effects. Shared retained worktrees are observed but never deleted.
        for worktree in inventory["worktrees"]:
            if worktree["disposition"] == "absent":
                continue
            if worktree["disposition"] == "shared-in-use":
                continue
            task_row = task_rows.get(worktree["owner_task_id"])
            if task_row is None or task_row["project_id"] != worktree["project_id"]:
                raise CleanupError("worktree owner Task differs from native binding")
            native_worktree = next(
                (item for item in snapshot["worktrees"]
                 if item["worktree_id"] == worktree["worktree_id"]),
                None,
            )
            if worktree["worktree_id"] in self._worktree_settlements:
                if native_worktree is not None and (
                    native_worktree["workspace_id"] != worktree["workspace_id"]
                    or native_worktree["path"] != worktree["path"]
                ):
                    raise CleanupError("settled native worktree record changed")
                if _check_path_components(
                    Path(worktree["path"]), allow_missing_tree=True,
                ) is not None:
                    raise CleanupError("settled worktree path reappeared")
                self._verify_git_worktree(worktree, expect_present=False)
                retained = self._git(
                    "-C", str(primary), "rev-parse", "--verify",
                    f"refs/heads/{worktree['branch']}",
                )
                if retained != worktree["branch_oid"]:
                    raise CleanupError("settled worktree branch ref changed")
                continue
            if (
                native_worktree is None
                or native_worktree["workspace_id"] != worktree["workspace_id"]
                or native_worktree["path"] != worktree["path"]
            ):
                raise CleanupError("native worktree registry differs from inventory")
            self._verify_git_worktree(worktree, expect_present=True)
        return tuple(snapshot["sessions"])

    def delete_session(self, row: Mapping[str, Any]) -> str:
        session_id = row["session_id"]
        _uuid4(session_id, _SESSION_ID, "Session id")
        snapshot = self._snapshot_once()
        current = [item for item in snapshot["sessions"] if item["session_id"] == session_id]
        durable = getattr(self, "_session_settlements", {}).get(session_id)
        if self._receipt_inventory is not None and durable is not None:
            if current:
                raise CleanupError("a durably settled Session acquired native metadata again")
            return durable
        if current:
            if len(current) != 1:
                raise CleanupError("native Session identity is ambiguous")
            observed_path, observed = self._session_path(current[0])
            if (
                observed_path.as_posix() != row["artifact_relpath"]
                and f"projects/{row['project_id']}/sessions/{session_id}" != row["artifact_relpath"]
            ):
                raise CleanupError("native Session path changed before deletion")
            if observed is not None and (observed.st_dev, observed.st_ino) != (
                row["device"], row["inode"],
            ):
                raise CleanupError("native Session artifact was substituted before deletion")
        code, output, error = self.runner(
            ["session", "delete", "--confirm", "--json", session_id],
            cwd=self.workspace, timeout=120.0, output_limit=65_536,
        )
        try:
            envelope = _json_object(output, "native Session delete response")
        except CleanupError:
            raise CleanupError("native Session deletion outcome is unknown") from None
        status = envelope.get("status")
        result = envelope.get("result")
        failure = envelope.get("error")
        if status == "ok" and isinstance(result, dict) and result.get("session_id") == session_id:
            if code != 0:
                raise CleanupError("native Session delete exit status contradicts its result")
            disposition = "removed"
        elif status == "error" and isinstance(failure, dict) and (
            failure.get("session_id") == session_id
        ):
            error_code = failure.get("code")
            message = failure.get("message")
            if error_code == "session_in_use":
                raise CleanupError("native Kent refused deletion of a live Session")
            if error_code == "self_session_forbidden":
                raise CleanupError("native Kent refused deletion of the current Session")
            if error_code == "session_not_found":
                raise CleanupError(
                    "native Session absence does not prove prior artifact cleanup settled",
                )
            elif (
                error_code == "request_failed"
                and isinstance(message, str)
                and message.startswith("Session is gone from Kent, but artifact cleanup failed.")
            ):
                raise CleanupError("native Session Delete lacks a positive final success ACK")
            else:
                raise CleanupError("native Session delete returned an unsettled failure")
            if code == 0:
                raise CleanupError("native Session delete exit status contradicts its error")
        else:
            raise CleanupError("native Session delete response identity differs")
        self._snapshots = {}
        after = self._snapshot_once()
        if any(item["session_id"] == session_id for item in after["sessions"]):
            raise CleanupError("native Session metadata remains after lifecycle response")
        self._session_settlements = getattr(self, "_session_settlements", {})
        self._session_settlements[session_id] = disposition
        self._native_ack[session_id] = {"session_id": session_id, "native_status": "ok"}
        return disposition

    def remove_session_container(self, row: Mapping[str, Any]) -> str:
        session_id = row["session_id"]
        if getattr(self, "_session_settlements", {}).get(session_id) not in {"removed", "absent"}:
            raise CleanupError("Session residual cleanup lacks settled lifecycle evidence")
        if self._receipt_inventory is not None:
            index = next(
                index for index, item in enumerate(self._receipt_inventory.value["sessions"])
                if item["session_id"] == session_id
            )
            if ("session_native_settled", "session", index) not in self._receipt_entries:
                raise CleanupError("Session residual cleanup lacks a durable native ACK")
            if ("container_settled", "session", index) in self._receipt_entries:
                path, info = self._session_path(row)
                if info is not None:
                    raise CleanupError("a settled Session container reappeared")
                return "absent"
        snapshot = self._snapshot_once()
        if any(item["session_id"] == session_id for item in snapshot["sessions"]):
            raise CleanupError("Session metadata reappeared before residual cleanup")
        path = Path(self.context["persistence_root"]) / row["artifact_relpath"]
        path = _clean_path(path, "Session artifact path")
        info = _check_path_components(path, allow_missing_leaf=True)
        if info is None:
            return "absent"
        if (
            not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino) != (row["device"], row["inode"])
            or not shutil.rmtree.avoids_symlink_attacks
        ):
            raise CleanupError("Session residual directory identity is unsafe")
        parent = path.parent
        parent_info = _check_path_components(parent)
        if parent_info is None or not stat.S_ISDIR(parent_info.st_mode):
            raise CleanupError("Session parent directory is unavailable")
        if Path(self.context["persistence_root"]) not in path.parents:
            raise CleanupError("Session residual directory escapes native storage")
        shutil.rmtree(path)
        if path.exists() or path.is_symlink():
            raise CleanupError("Session residual directory remains after removal")
        parent_after = parent.lstat()
        if (parent_info.st_dev, parent_info.st_ino) != (
            parent_after.st_dev, parent_after.st_ino,
        ):
            raise CleanupError("Session parent directory identity changed")
        return "removed"

    def remove_owned_container(self, row: Mapping[str, Any]) -> str:
        path = _clean_path(row["path"], "owned container")
        if self._receipt_inventory is not None:
            index = next(
                index for index, item in enumerate(self._receipt_inventory.value["owned_containers"])
                if item["path"] == row["path"]
            )
            if ("container_settled", "owned", index) in self._receipt_entries:
                if _check_path_components(path, allow_missing_tree=True) is not None:
                    raise CleanupError("a settled owned container reappeared")
                return "absent"
        parent = path.parent
        parent_info = _check_path_components(parent)
        if (
            parent_info is None or not stat.S_ISDIR(parent_info.st_mode)
            or (parent_info.st_dev, parent_info.st_ino)
            != (row["parent_device"], row["parent_inode"])
        ):
            raise CleanupError("owned container parent identity is unsafe")
        if any(
            _overlap_path(path, root)
            and not (
                root == Path(self.context["persistence_root"])
                and str(path) in self._owned_container_paths
            )
            for root in self._protected_roots
        ):
            raise CleanupError("owned container overlaps a protected root")
        parent_fd = -1
        try:
            parent_fd = os.open(
                parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            opened_parent = os.fstat(parent_fd)
            if (opened_parent.st_dev, opened_parent.st_ino) != (
                row["parent_device"], row["parent_inode"],
            ):
                raise CleanupError("owned container parent changed before removal")
            try:
                info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return "absent"
            if (
                not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or (info.st_dev, info.st_ino) != (row["device"], row["inode"])
            ):
                raise CleanupError("owned container identity is unsafe")
            if stat.S_ISDIR(info.st_mode):
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise CleanupError("safe directory removal is unavailable")
                shutil.rmtree(path.name, dir_fd=parent_fd)
            else:
                os.unlink(path.name, dir_fd=parent_fd)
            try:
                os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise CleanupError("owned container remains after removal")
            parent_after = os.fstat(parent_fd)
            if (opened_parent.st_dev, opened_parent.st_ino) != (
                parent_after.st_dev, parent_after.st_ino,
            ):
                raise CleanupError("owned container parent changed during removal")
            return "removed"
        except OSError as error:
            raise CleanupError("owned container could not be safely removed") from error
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)

    def delete_worktree(self, row: Mapping[str, Any]) -> str:
        if row["role"] not in {"task", "supervisor"} or row["disposition"] != "remove":
            raise CleanupError("only an exclusive cleanup-owned worktree is disposable")
        if self._primary_path() == Path(row["path"]):
            raise CleanupError("refusing to remove the protected primary checkout")
        if self._receipt_inventory is not None:
            self.require_physical_coverage()
            if row["worktree_id"] in self._worktree_settlements:
                self._verify_git_worktree(row, expect_present=False)
                if self._git(
                    "-C", str(self._primary_path()), "rev-parse", "--verify",
                    f"refs/heads/{row['branch']}",
                ) != row["branch_oid"]:
                    raise CleanupError("receipt-backed worktree retained ref changed")
                return "removed"
        self._verify_git_worktree(row, expect_present=True)
        incarnation = _check_path_components(
            Path(row["path"]), allow_missing_tree=self._receipt_inventory is None,
        )
        if self._receipt_inventory is not None and incarnation is None:
            raise CleanupError("worktree incarnation is unavailable before deletion")
        code, output, error = self.runner([
            "worktree", "delete", "--project", row["project_id"],
            "--workspace", row["workspace_id"], "--force", "--json",
            row["worktree_id"],
        ], cwd=self.workspace, timeout=300.0, output_limit=65_536)
        if code:
            raise CleanupError(
                "native managed worktree deletion failed or is unsettled: "
                + error.decode("utf-8", "replace")[:512],
            )
        response = _json_object(output, "native worktree deletion response")
        if response.get("status") not in {None, "ok"}:
            raise CleanupError("native worktree deletion returned a non-success status")
        result = response.get("result", response)
        cleanup = result.get("cleanup") if isinstance(result, dict) else None
        if (
            not isinstance(cleanup, dict)
            or cleanup.get("kind") not in {
                "NOT_REQUESTED", "NOT_APPLICABLE", "RETAINED", "DELETED",
                "not_requested", "not_applicable", "retained", "deleted",
            }
            or (
                result.get("leftover_root") is not None
                and result.get("leftover_root") != ""
            )
        ):
            raise CleanupError("native worktree deletion response is unsettled")
        self._verify_git_worktree(row, expect_present=False)
        retained_branch = self._git(
            "-C", str(self._primary_path()), "rev-parse", "--verify",
            f"refs/heads/{row['branch']}",
        )
        if retained_branch != row["branch_oid"]:
            raise CleanupError("local worktree branch was not retained")
        self._snapshots = {}
        remaining, _ = self._native_paths(
            [row["worktree_id"]], project_id=row["project_id"], allow_missing=True,
        )
        if remaining and remaining != [{
            "worktree_id": row["worktree_id"],
            "workspace_id": row["workspace_id"], "path": row["path"],
        }]:
            raise CleanupError("native worktree record changed during deletion")
        self._worktree_settlements.add(row["worktree_id"])
        if incarnation is not None:
            self._pending_worktree_proofs[row["worktree_id"]] = {
                "incarnation": {
                    "device": incarnation.st_dev, "inode": incarnation.st_ino,
                    "ctime_ns": incarnation.st_ctime_ns,
                },
                "path_absent": True, "git_registration_absent": True,
                "branch_oid": retained_branch,
                "native_record": "missing" if remaining else "absent",
            }
        return "removed"

    def verify_postconditions(self, inventory: Mapping[str, Any]) -> Mapping[str, Any]:
        self._snapshots = {}
        selected_tasks = {inventory["task_id"]}
        for membership in inventory["supervision"]:
            if inventory["task_id"] in membership["task_ids"]:
                selected_tasks.update(membership["task_ids"])
        snapshot = self._snapshot_once(sorted(selected_tasks))
        selected_session_ids = self._selected_cleanup_session_ids(snapshot, inventory)
        inventory_session_ids = {
            row["session_id"] for row in inventory["sessions"]
        }
        if selected_session_ids - inventory_session_ids:
            raise CleanupError("an unexpected native Session descendant remains")
        shared_session_ids = {
            row["session_id"] for row in inventory["sessions"]
            if row["disposition"] == "shared-in-use"
        }
        if shared_session_ids - selected_session_ids:
            raise CleanupError("a retained shared Session disappeared before completion")
        remaining_sessions = {
            row["session_id"] for row in snapshot["sessions"]
        }
        expected_sessions = {
            row["session_id"] for row in inventory["sessions"]
            if row["disposition"] == "remove"
        }
        if remaining_sessions.intersection(expected_sessions):
            raise CleanupError("one or more disposable native Sessions remain")
        session_dirs_absent = 0
        for row in inventory["sessions"]:
            if row["disposition"] == "shared-in-use":
                continue
            path = Path(self.context["persistence_root"]) / row["artifact_relpath"]
            if path.exists() or path.is_symlink():
                raise CleanupError("a disposable Session artifact directory remains")
            session_dirs_absent += 1
        owned_absent = 0
        for row in inventory["owned_containers"]:
            if row["disposition"] == "shared-in-use":
                continue
            path = Path(row["path"])
            if path.exists() or path.is_symlink():
                raise CleanupError("an owned local container remains")
            owned_absent += 1
        worktrees_absent = 0
        branch_count = 0
        historical_absent_ids = [
            row["worktree_id"] for row in inventory["worktrees"]
            if row["disposition"] == "absent"
        ]
        if historical_absent_ids:
            present, _ = self._native_paths(
                historical_absent_ids, project_id=inventory["project_id"],
                allow_missing=True,
            )
            for row in inventory["worktrees"]:
                if row["disposition"] == "absent":
                    self._verify_absent_historical_worktree(
                        row, present, primary=self._primary_path(),
                    )
        for row in inventory["worktrees"]:
            if row["disposition"] == "shared-in-use":
                continue
            if row["disposition"] == "absent":
                if row["path"] is not None and _check_path_components(
                    Path(row["path"]), allow_missing_tree=True,
                ) is not None:
                    raise CleanupError("historical absent worktree path reappeared")
                worktrees_absent += 1
                continue
            if _check_path_components(Path(row["path"]), allow_missing_leaf=True) is not None:
                raise CleanupError("a disposable managed worktree path remains")
            self._verify_git_worktree(row, expect_present=False)
            worktrees_absent += 1
            branch = self._git(
                "-C", str(self._primary_path()), "rev-parse",
                "--verify", f"refs/heads/{row['branch']}",
            )
            if branch != row["branch_oid"]:
                raise CleanupError("local worktree branch ref changed or disappeared")
            branch_count += 1
        return {
            "sessions_absent": len(expected_sessions),
            "session_containers_absent": session_dirs_absent,
            "owned_containers_absent": owned_absent,
            "worktrees_absent": worktrees_absent,
            "branches_retained": branch_count,
        }

    def _git(self, *arguments: str) -> str:
        code, output, error = _run_bounded(
            ["/usr/bin/git", *arguments], cwd=self.workspace,
            environment=_safe_environment(), timeout=20.0, output_limit=65_536,
        )
        if code:
            raise CleanupError("Git worktree/ref observation failed: "
                               + error.decode("utf-8", "replace")[:512])
        return output.decode("utf-8").strip()

    def _verify_git_worktree(self, row: Mapping[str, Any], *, expect_present: bool) -> None:
        path = Path(row["path"])
        info = _check_path_components(path, allow_missing_leaf=True)
        if expect_present and (info is None or not stat.S_ISDIR(info.st_mode)):
            raise CleanupError("managed worktree directory is absent or not a directory")
        if not expect_present and info is not None:
            raise CleanupError("managed worktree directory remains")
        output = self._git(
            "-C", str(self._primary_path()), "worktree", "list", "--porcelain",
        )
        present = False
        branch_ref = f"refs/heads/{row['branch']}"
        current = None
        for line in output.splitlines():
            if line.startswith("worktree "):
                if current is not None and current["path"] == row["path"]:
                    present = True
                    if current.get("branch") != branch_ref:
                        raise CleanupError("Git worktree branch differs from inventory")
                current = {"path": line[len("worktree "):]}
            elif current is not None and line.startswith("branch "):
                current["branch"] = line[len("branch "):]
        if current is not None and current["path"] == row["path"]:
            present = True
            if current.get("branch") != branch_ref:
                raise CleanupError("Git worktree branch differs from inventory")
        if present != expect_present:
            raise CleanupError("Git worktree registration does not match expected disposition")
        if expect_present:
            head = self._git("-C", str(path), "rev-parse", "--verify", "HEAD")
            branch = self._git(
                "-C", str(self._primary_path()), "rev-parse", "--verify", branch_ref,
            )
            if head != row["head_oid"] or branch != row["branch_oid"]:
                raise CleanupError("managed worktree head or local ref changed")

    def _primary_path(self) -> Path:
        detail = self._detail
        source = detail.get("source_workspace") if isinstance(detail, dict) else None
        source_path = (
            source.get("root_path") if isinstance(source, dict) else None
        )
        active_path = getattr(self, "_active_primary_path", None)
        if source_path is None and active_path is None:
            raise CleanupError("native protected primary workspace is unavailable")
        primary = _clean_path(
            source_path if source_path is not None else active_path,
            "protected primary",
        )
        if active_path is not None and primary != active_path:
            raise CleanupError("native primary workspace differs from frozen inventory")
        info = _check_path_components(primary)
        if info is None or not stat.S_ISDIR(info.st_mode):
            raise CleanupError("protected primary workspace is unavailable")
        return primary


def _overlap_path(left: Path, right: str | Path) -> bool:
    a, b = left, Path(right)
    return a == b or a in b.parents or b in a.parents
