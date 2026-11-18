"""Bounded policy and orchestration for explicitly selected merged cleanup."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Mapping, Protocol, Sequence
import uuid


MAX_INVENTORY_BYTES = 8 * 1024 * 1024
MAX_HANDOFF_BYTES = 65_536
# Two bounded 64 KiB strings can each expand sixfold under JSON escaping,
# plus the native identity envelope. This is independent of output limits.
MAX_NATIVE_SCRIPT_INPUT_BYTES = 1_048_576
MAX_SCRIPT_OUTPUT_BYTES = 262_144
MAX_SESSIONS = 4096
MAX_WORKTREES = 32
MAX_CONTAINERS = 4096

SUPERVISION_SCHEMA = "merged-cleanup-supervision-v1"
INVENTORY_SCHEMA = "merged-cleanup-inventory-v1"
HANDOFF_SCHEMA = "merged-cleanup-handoff-v1"
RESULT_SCHEMA = "merged-cleanup-result-v1"
POLICY_SCHEMA = "merged-cleanup-policy-v1"
HISTORY_INVENTORY_SCHEMA = "merged-cleanup-history-inventory-v1"
RECEIPT_SCHEMA = "merged-cleanup-receipt-v2"
MAX_RECEIPT_BYTES = 16_384

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_OID = re.compile(r"[0-9a-f]{40}\Z")
_TASK_ID = re.compile(r"task-[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_PROJECT_ID = re.compile(r"project-[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_WORKFLOW_ID = _SESSION_ID

_INVENTORY_FIELDS = {
    "schema", "project_id", "task_id", "workflow_id", "subject",
    "pull_request", "cleanup_context_sha256", "policy", "policy_provenance",
    "history_operation", "release", "supervision", "sessions",
    "member_outcomes", "worktrees", "owned_containers", "protected_roots",
}
_SUBJECT_FIELDS = {
    "task_short_id", "workspace_id", "worktree_id", "worktree_path",
    "branch", "head_oid", "branch_oid",
}
_PR_FIELDS = {
    "repository", "number", "url", "base_ref", "base_oid", "head_ref",
    "head_oid", "state", "merged_at", "merge_commit",
}
_POLICY_FIELDS = {"schema", "project_id", "workflow_id", "mode"}
_POLICY_PROVENANCE_FIELDS = {
    "transition_group_id", "transition_id", "source_node_id", "entry_node_id",
    "edge_key", "policy_sha256",
}
_RELEASE_FIELDS = {
    "prepared_by_session_id", "owner_session_ids", "release_sha256",
}
_SESSION_FIELDS = {
    "session_id", "project_id", "task_id", "parent_agent_session_id",
    "previous_session_id", "workspace_id", "worktree_id",
    "artifact_relpath", "device", "inode",
    "supervisor_root_ids", "disposition",
}
_MEMBER_FIELDS = {
    "task_id", "native_state", "outcome", "pull_request_url", "head_oid",
    "merged_at", "merge_commit",
}
_WORKTREE_FIELDS = {
    "project_id", "workspace_id", "worktree_id", "path", "branch",
    "head_oid", "branch_oid", "role", "disposition", "owner_task_id",
}
_CONTAINER_FIELDS = {
    "path", "kind", "owner_task_id", "evidence_reference",
    "device", "inode", "parent_device", "parent_inode", "disposition",
}
_PROTECTED_FIELDS = {"path", "kind"}


class CleanupError(ValueError):
    """Invalid, incomplete, stale, or unsettled merged-cleanup input."""


class CleanupBackend(Protocol):
    """Effect boundary used by the deterministic executor and fixture tests."""

    def revalidate(self, inventory: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]: ...
    def begin_inventory(self, inventory: ImmutableInventory) -> dict[str, Any] | None: ...
    def admit_effects(self) -> None: ...
    def record_settlement(self, stage: str, kind: str, index: int, disposition: str) -> str: ...
    def require_physical_coverage(self) -> None: ...
    def delete_session(self, row: Mapping[str, Any]) -> str: ...
    def remove_session_container(self, row: Mapping[str, Any]) -> str: ...
    def remove_owned_container(self, row: Mapping[str, Any]) -> str: ...
    def delete_worktree(self, row: Mapping[str, Any]) -> str: ...
    def verify_postconditions(self, inventory: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ImmutableInventory:
    """Validated canonical inventory bytes, safe to reuse after interruption."""

    raw: bytes
    sha256: str
    value: Mapping[str, Any]


@dataclass(frozen=True)
class FiniteHistoryInventory:
    """One immutable, exact Task selection for a historical cleanup operation."""

    raw: bytes
    sha256: str
    value: Mapping[str, Any]
    inventories: tuple[ImmutableInventory, ...]


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise CleanupError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def canonical_bytes(value: Any, *, maximum: int = MAX_INVENTORY_BYTES) -> bytes:
    try:
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise CleanupError("value is not canonical JSON") from error
    if len(raw) > maximum:
        raise CleanupError("cleanup JSON exceeds its byte limit")
    return raw


def parse_json(raw: bytes, *, maximum: int = MAX_INVENTORY_BYTES) -> Any:
    if not isinstance(raw, bytes) or len(raw) > maximum:
        raise CleanupError("cleanup JSON is missing or oversized")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                CleanupError("nonfinite JSON value")),
        )
        canonical_bytes(value, maximum=maximum)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise CleanupError("cleanup JSON is invalid") from error
    return value


def _closed(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CleanupError(f"{label} has an invalid field set")
    return value


def _string(value: Any, label: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise CleanupError(f"{label} is invalid")
    return value


def _uuid(value: Any, pattern: re.Pattern[str], label: str) -> str:
    text = _string(value, label, maximum=64)
    if not pattern.fullmatch(text):
        raise CleanupError(f"{label} is not a native identity")
    return text


def _sha256(value: Any, label: str) -> str:
    text = _string(value, label, maximum=64)
    if not _SHA256.fullmatch(text):
        raise CleanupError(f"{label} is not a SHA-256 digest")
    return text


def _oid(value: Any, label: str) -> str:
    text = _string(value, label, maximum=64)
    if not _OID.fullmatch(text):
        raise CleanupError(f"{label} is not a full Git object ID")
    return text


def _absolute_path(value: Any, label: str, *, allow_root: bool = False) -> str:
    text = _string(value, label, maximum=4096)
    path = Path(text)
    if (
        not path.is_absolute() or str(path) != text or ".." in path.parts
        or (not allow_root and path == Path(path.anchor))
    ):
        raise CleanupError(f"{label} is not a canonical absolute path")
    return text


def _unique(values: Sequence[str], label: str) -> None:
    if len(set(values)) != len(values):
        raise CleanupError(f"{label} contains duplicate identities")


def validate_supervision_record(value: Any) -> dict[str, Any]:
    record = _closed(value, {
        "schema", "project_id", "supervisor_session_id", "task_ids",
        "reused_root_session_ids",
    }, "supervision record")
    if record["schema"] != SUPERVISION_SCHEMA:
        raise CleanupError("supervision record schema differs")
    _uuid(record["project_id"], _PROJECT_ID, "supervision project_id")
    _uuid(record["supervisor_session_id"], _SESSION_ID, "supervisor Session")
    task_ids = record["task_ids"]
    reused = record["reused_root_session_ids"]
    if (
        not isinstance(task_ids, list) or not task_ids or len(task_ids) > 256
        or not isinstance(reused, list) or len(reused) > MAX_SESSIONS
    ):
        raise CleanupError("supervision membership exceeds its bounds")
    for task_id in task_ids:
        _uuid(task_id, _TASK_ID, "supervision Task")
    for session_id in reused:
        _uuid(session_id, _SESSION_ID, "reused Session root")
    if task_ids != sorted(task_ids) or reused != sorted(reused):
        raise CleanupError("supervision membership must be sorted")
    _unique(task_ids, "supervision Tasks")
    _unique(reused, "reused roots")
    return record


def supervision_comment(record: Mapping[str, Any]) -> str:
    validated = validate_supervision_record(dict(record))
    return canonical_bytes(validated, maximum=16_384).decode("utf-8")


def _session_row(value: Any) -> dict[str, Any]:
    row = _closed(value, _SESSION_FIELDS, "native Session row")
    _uuid(row["session_id"], _SESSION_ID, "Session id")
    _uuid(row["project_id"], _PROJECT_ID, "Session project_id")
    if row["task_id"] is not None:
        _uuid(row["task_id"], _TASK_ID, "Session task_id")
    for key in ("parent_agent_session_id", "previous_session_id"):
        if row[key] is not None:
            _uuid(row[key], _SESSION_ID, key)
    for key in ("workspace_id", "worktree_id"):
        if row[key] is not None:
            _string(row[key], f"Session {key}", maximum=256)
    expected = f"projects/{row['project_id']}/sessions/{row['session_id']}"
    if row["artifact_relpath"] != expected:
        raise CleanupError("native Session artifact_relpath differs from its identity")
    device, inode = row["device"], row["inode"]
    if (device is None) != (inode is None):
        raise CleanupError("Session container identity is incomplete")
    if device is not None and (
        type(device) is not int or device < 0
        or type(inode) is not int or inode <= 0
    ):
        raise CleanupError("Session container identity is invalid")
    supervisor_roots = row["supervisor_root_ids"]
    if not isinstance(supervisor_roots, list) or len(supervisor_roots) > 256:
        raise CleanupError("Session supervisor roots are invalid")
    for root_id in supervisor_roots:
        _uuid(root_id, _SESSION_ID, "Session supervisor root")
    if supervisor_roots != sorted(supervisor_roots):
        raise CleanupError("Session supervisor roots must be sorted")
    _unique(supervisor_roots, "Session supervisor roots")
    if row["disposition"] not in {"remove", "shared-in-use"}:
        raise CleanupError("Session disposition is invalid")
    if row["disposition"] == "shared-in-use" and not supervisor_roots:
        raise CleanupError("shared Session has no registered supervisor root")
    return row


def build_session_closure(
    rows: Sequence[Mapping[str, Any]],
    *,
    task_id: str,
    project_id: str,
    supervision: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Return direct Task Sessions plus inverse-parent descendants everywhere."""
    _uuid(task_id, _TASK_ID, "Task id")
    _uuid(project_id, _PROJECT_ID, "project_id")
    if not isinstance(rows, Sequence) or len(rows) > 32_768:
        raise CleanupError("native Session catalog exceeds its bound")
    # The global catalog supplies ancestry, not disposal authority. Legacy
    # reassigned Sessions may retain paths in their original project. Validate
    # graph identities globally and full path/ownership only for the closure.
    normalized = [dict(row) for row in rows]
    for row in normalized:
        _uuid(row.get("session_id"), _SESSION_ID, "catalog Session")
        for key, pattern in (
            ("parent_agent_session_id", _SESSION_ID), ("task_id", _TASK_ID),
        ):
            if row.get(key) is not None:
                _uuid(row[key], pattern, f"catalog Session {key}")
    ids = [row["session_id"] for row in normalized]
    _unique(ids, "native Session catalog")
    by_id = {row["session_id"]: row for row in normalized}
    children: dict[str, list[str]] = {}
    for row in normalized:
        parent = row["parent_agent_session_id"]
        if parent is not None:
            children.setdefault(parent, []).append(row["session_id"])

    memberships = [validate_supervision_record(dict(item)) for item in supervision]
    registered_tasks = {task_id}
    for record in memberships:
        if task_id in record["task_ids"]:
            registered_tasks.update(record["task_ids"])
    roots = {
        row["session_id"] for row in normalized
        if row["project_id"] == project_id and row["task_id"] == task_id
    }
    registered_roots: set[str] = set()
    for record in memberships:
        if task_id in record["task_ids"]:
            registered_roots.add(record["supervisor_session_id"])
            registered_roots.update(record["reused_root_session_ids"])
    roots.update(registered_roots)
    if not roots:
        raise CleanupError("Task has no native Session roots")
    missing_roots = roots - set(by_id)
    if missing_roots:
        raise CleanupError("registered cleanup Session root is missing")

    selected: set[str] = set()
    pending = list(sorted(roots))
    while pending:
        session_id = pending.pop()
        if session_id in selected:
            continue
        selected.add(session_id)
        pending.extend(children.get(session_id, ()))
        if len(selected) > MAX_SESSIONS:
            raise CleanupError("Session descendant closure exceeds its bound")

    # Explicit supervisor/reused roots bound the authorized subtree; their
    # known parents supply ancestry, not authority to delete the coordinator.
    # Ordinary direct Task roots still require the complete registered chain.
    for session_id in roots:
        parent = by_id[session_id]["parent_agent_session_id"]
        if parent is not None and parent not in selected:
            if session_id not in registered_roots or parent not in by_id:
                raise CleanupError("Task Session ancestry is not registered")
    if any(
        by_id[session_id]["task_id"] is not None
        and by_id[session_id]["task_id"] not in registered_tasks
        for session_id in selected
    ):
        raise CleanupError("Session descendant has an unregistered Task owner")
    return [_session_row(by_id[session_id]) for session_id in sorted(selected)]


def _validate_subject(value: Any, *, historical: bool = False) -> dict[str, Any]:
    row = _closed(value, _SUBJECT_FIELDS, "cleanup subject")
    _string(row["task_short_id"], "Task short id", maximum=128)
    _string(row["workspace_id"], "workspace_id", maximum=256)
    local_values = (
        row["worktree_id"], row["worktree_path"], row["branch"],
        row["head_oid"], row["branch_oid"],
    )
    if historical and row["worktree_id"] is None:
        if any(value is not None for value in local_values[1:]):
            raise CleanupError("historical subject has local worktree facts without an identity")
        return row
    if historical and row["worktree_id"] is not None and (
        row["branch"] is None and row["head_oid"] is None and row["branch_oid"] is None
    ):
        _string(row["worktree_id"], "worktree_id", maximum=256)
        if row["worktree_path"] is not None:
            _absolute_path(row["worktree_path"], "worktree_path")
        return row
    _string(row["worktree_id"], "worktree_id", maximum=256)
    _absolute_path(row["worktree_path"], "worktree_path")
    _string(row["branch"], "branch", maximum=512)
    if row["branch"].startswith("/") or ".." in row["branch"].split("/"):
        raise CleanupError("worktree branch is invalid")
    _oid(row["head_oid"], "worktree HEAD")
    _oid(row["branch_oid"], "local branch ref")
    return row


def _validate_pr(value: Any) -> dict[str, Any]:
    row = _closed(value, _PR_FIELDS, "pull request proof")
    repository = _string(row["repository"], "PR repository", maximum=256)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise CleanupError("PR repository is invalid")
    if type(row["number"]) is not int or row["number"] <= 0:
        raise CleanupError("PR number is invalid")
    url = _string(row["url"], "PR URL", maximum=2048)
    if not re.fullmatch(
        rf"https://github\.com/{re.escape(repository)}/pull/{row['number']}", url,
    ):
        raise CleanupError("PR URL does not bind repository and number")
    _string(row["base_ref"], "PR base ref", maximum=512)
    _oid(row["base_oid"], "PR base OID")
    _string(row["head_ref"], "PR head ref", maximum=512)
    _oid(row["head_oid"], "PR delivered head OID")
    if row["state"] != "MERGED":
        raise CleanupError("PR is not provider-confirmed MERGED")
    _string(row["merged_at"], "provider merged_at", maximum=128)
    if row["merge_commit"] is not None:
        _oid(row["merge_commit"], "PR merge commit")
    return row


def validate_policy(
    value: Any, *, project_id: str, workflow_id: str,
) -> dict[str, Any]:
    """Validate the closed standing policy against genuine native identities."""
    row = _closed(value, _POLICY_FIELDS, "merged cleanup policy")
    if (
        row["schema"] != POLICY_SCHEMA
        or row["mode"] != "after_merge"
        or row["project_id"] != project_id
        or row["workflow_id"] != workflow_id
    ):
        raise CleanupError("merged cleanup policy does not bind the native subject")
    _uuid(row["project_id"], _PROJECT_ID, "policy project_id")
    _uuid(row["workflow_id"], _WORKFLOW_ID, "policy workflow_id")
    return dict(row)


def validate_policy_provenance(
    value: Any, *, policy: Mapping[str, Any],
) -> dict[str, Any]:
    row = _closed(value, _POLICY_PROVENANCE_FIELDS, "native policy provenance")
    for key in ("transition_group_id", "source_node_id", "entry_node_id"):
        _uuid(row[key], _SESSION_ID, f"policy provenance {key}")
    if row["transition_id"] != "delivered" or row["edge_key"] != "finish_delivered":
        raise CleanupError("native policy provenance route differs")
    expected = hashlib.sha256(canonical_bytes(dict(policy), maximum=1000)).hexdigest()
    if _sha256(row["policy_sha256"], "native policy digest") != expected:
        raise CleanupError("native policy digest differs from its parsed values")
    return dict(row)


def _validate_release(
    value: Any, *, require_preparer_owner: bool = True,
) -> dict[str, Any]:
    row = _closed(value, _RELEASE_FIELDS, "cleanup release proof")
    _uuid(row["prepared_by_session_id"], _SESSION_ID, "preparing Session")
    _sha256(row["release_sha256"], "release proof digest")
    ids = row["owner_session_ids"]
    if not isinstance(ids, list) or len(ids) > MAX_SESSIONS:
        raise CleanupError("owner Session list exceeds its bound")
    for session_id in ids:
        _uuid(session_id, _SESSION_ID, "owner Session")
    if ids != sorted(ids):
        raise CleanupError("owner Sessions must be sorted")
    _unique(ids, "owner Sessions")
    if require_preparer_owner and row["prepared_by_session_id"] not in ids:
        raise CleanupError("the preparing Cleanup Session is absent from expected owners")
    return row


def _validate_history_operation(value: Any, *, project_id: str, task_id: str) -> dict[str, Any]:
    row = _closed(value, {
        "schema", "project_id", "operator_project_id", "operator_task_id",
        "operator_session_id",
        "selected_task_ids", "permission_evidence",
    }, "historical cleanup operation")
    if row["schema"] != "merged-cleanup-history-operation-v1":
        raise CleanupError("historical cleanup operation schema differs")
    if _uuid(row["project_id"], _PROJECT_ID, "historical Project") != project_id:
        raise CleanupError("historical operation Project differs from inventory")
    _uuid(row["operator_project_id"], _PROJECT_ID, "historical operator Project")
    operator_task_id = row["operator_task_id"]
    if operator_task_id is not None:
        _uuid(operator_task_id, _TASK_ID, "historical operator Task")
    operator_session_id = _uuid(row["operator_session_id"], _SESSION_ID,
                                "historical operator Session")
    selected = row["selected_task_ids"]
    if not isinstance(selected, list) or not selected or len(selected) > 256:
        raise CleanupError("historical Task selection is empty or oversized")
    for selected_id in selected:
        _uuid(selected_id, _TASK_ID, "selected historical Task")
    if selected != sorted(selected) or len(set(selected)) != len(selected):
        raise CleanupError("historical Task selection must be unique and sorted")
    if task_id not in selected or (
        operator_task_id is not None and operator_task_id in selected
    ):
        raise CleanupError("historical operation does not bind this victim and distinct operator")
    evidence = _closed(row["permission_evidence"], {"reference", "context"},
                       "original historical permission evidence")
    _string(evidence["reference"], "historical permission reference", maximum=4096)
    _string(evidence["context"], "historical permission context", maximum=16_384)
    return dict(row)


def _validate_worktree(
    value: Any, task_id: str, *, historical: bool = False,
) -> dict[str, Any]:
    row = _closed(value, _WORKTREE_FIELDS, "managed worktree")
    _uuid(row["project_id"], _PROJECT_ID, "worktree project_id")
    _string(row["workspace_id"], "worktree workspace_id", maximum=256)
    _string(row["worktree_id"], "worktree id", maximum=256)
    if row["role"] not in {"task", "supervisor"}:
        raise CleanupError("worktree role is invalid")
    absent_without_local_facts = (
        historical and row["role"] == "task" and row["disposition"] == "absent"
        and row["branch"] is None and row["head_oid"] is None
        and row["branch_oid"] is None
    )
    if row["path"] is None:
        if not absent_without_local_facts:
            raise CleanupError("managed worktree path is unavailable")
    else:
        _absolute_path(row["path"], "worktree path")
    if not absent_without_local_facts:
        _string(row["branch"], "worktree branch", maximum=512)
        _oid(row["head_oid"], "worktree HEAD")
        _oid(row["branch_oid"], "worktree branch ref")
    allowed_dispositions = {"remove", "shared-in-use"}
    if historical and row["role"] == "task":
        allowed_dispositions.add("absent")
    if row["disposition"] not in allowed_dispositions:
        raise CleanupError("worktree disposition is invalid")
    _uuid(row["owner_task_id"], _TASK_ID, "worktree owner Task")
    if row["role"] == "task" and row["owner_task_id"] != task_id:
        raise CleanupError("task worktree owner differs")
    return row


def _validate_container(value: Any, task_id: str) -> dict[str, Any]:
    row = _closed(value, _CONTAINER_FIELDS, "owned local container")
    _absolute_path(row["path"], "owned container path")
    _string(row["kind"], "owned container kind", maximum=128)
    if row["kind"] not in {"archive", "evidence"}:
        raise CleanupError("owned container kind is not supported")
    _string(row["evidence_reference"], "owned-container evidence reference", maximum=4096)
    _uuid(row["owner_task_id"], _TASK_ID, "container owner Task")
    if row["owner_task_id"] != task_id:
        raise CleanupError("local container is not owned by the subject Task")
    if row["disposition"] not in {"remove", "shared-in-use"}:
        raise CleanupError("local container disposition is invalid")
    if (
        type(row["device"]) is not int or row["device"] < 0
        or type(row["inode"]) is not int or row["inode"] <= 0
        or type(row["parent_device"]) is not int or row["parent_device"] < 0
        or type(row["parent_inode"]) is not int or row["parent_inode"] <= 0
    ):
        raise CleanupError("owned container filesystem identity is invalid")
    return row


def _validate_protected(value: Any) -> dict[str, Any]:
    row = _closed(value, _PROTECTED_FIELDS, "protected root")
    _absolute_path(row["path"], "protected root", allow_root=True)
    _string(row["kind"], "protected root kind", maximum=128)
    return row


def _validate_member_outcome(value: Any) -> dict[str, Any]:
    row = _closed(value, _MEMBER_FIELDS, "supervisor Task outcome")
    _uuid(row["task_id"], _TASK_ID, "member Task")
    if row["native_state"] not in {"terminal", "final_disposal", "active"}:
        raise CleanupError("member Task native state is invalid")
    if row["outcome"] not in {"merged", "no-pr", "non-delivery", "unmerged", "unknown"}:
        raise CleanupError("member Task delivery outcome is invalid")
    if row["outcome"] == "merged":
        url = _string(row["pull_request_url"], "member PR URL", maximum=2048)
        if not url.startswith("https://github.com/"):
            raise CleanupError("member PR URL is invalid")
        _oid(row["head_oid"], "member delivered head")
        _string(row["merged_at"], "member provider merge time", maximum=128)
        if row["merge_commit"] is not None:
            _oid(row["merge_commit"], "member merge commit")
    elif any(row[key] is not None for key in (
        "pull_request_url", "head_oid", "merged_at", "merge_commit",
    )):
        raise CleanupError("non-merged member carries PR merge facts")
    return row


def _overlaps(left: str, right: str) -> bool:
    a, b = Path(left), Path(right)
    return a == b or a in b.parents or b in a.parents


def _validate_historical_residual(
    sessions: Sequence[Mapping[str, Any]], memberships: Sequence[Mapping[str, Any]],
    outcomes: Mapping[str, Mapping[str, Any]], *, task_id: str, project_id: str,
) -> None:
    """Prove directless residual rows through real registered inverse ancestry."""
    if not memberships or any(
        item["project_id"] != project_id or task_id not in item["task_ids"]
        for item in memberships
    ):
        raise CleanupError("historical residual has no exact registered Session roots")
    roots = {
        root for item in memberships
        for root in (item["supervisor_session_id"], *item["reused_root_session_ids"])
    }
    by_id = {row["session_id"]: row for row in sessions}
    if roots - by_id.keys():
        raise CleanupError("registered cleanup Session root is missing")
    registered_tasks = {member for item in memberships for member in item["task_ids"]}
    if any(row["task_id"] is not None and row["task_id"] not in registered_tasks for row in sessions):
        raise CleanupError("historical residual has an unregistered Task owner")
    children: dict[str, list[str]] = {}
    for row in sessions:
        if row["parent_agent_session_id"] is not None:
            children.setdefault(row["parent_agent_session_id"], []).append(row["session_id"])
    actual_roots: dict[str, set[str]] = {session_id: set() for session_id in by_id}
    for root in sorted(roots):
        pending, seen = [root], set()
        while pending:
            session_id = pending.pop()
            if session_id in seen:
                continue
            seen.add(session_id)
            actual_roots[session_id].add(root)
            pending.extend(children.get(session_id, ()))
    if any(
        not actual_roots[row["session_id"]]
        or actual_roots[row["session_id"]] != set(row["supervisor_root_ids"])
        for row in sessions
    ):
        raise CleanupError("historical residual is not its registered inverse-parent closure")
    held_roots = {
        root for item in memberships
        if any(
            outcomes[member]["native_state"] not in {"terminal", "final_disposal"}
            or outcomes[member]["outcome"] not in {"merged", "no-pr", "non-delivery"}
            for member in item["task_ids"]
        )
        for root in (item["supervisor_session_id"], *item["reused_root_session_ids"])
    }
    if any(actual_roots[row["session_id"]] & held_roots and row["disposition"] != "shared-in-use"
           for row in sessions):
        raise CleanupError("shared historical residual Session tree is not held whole")


def validate_inventory(value: Any) -> ImmutableInventory:
    row = _closed(value, _INVENTORY_FIELDS, "merged cleanup inventory")
    if row["schema"] != INVENTORY_SCHEMA:
        raise CleanupError("merged cleanup inventory schema differs")
    project_id = _uuid(row["project_id"], _PROJECT_ID, "inventory project_id")
    task_id = _uuid(row["task_id"], _TASK_ID, "inventory task_id")
    _uuid(row["workflow_id"], _WORKFLOW_ID, "inventory workflow_id")
    subject = _validate_subject(
        row["subject"], historical=row["history_operation"] is not None,
    )
    pr = _validate_pr(row["pull_request"])
    policy = validate_policy(
        row["policy"], project_id=project_id, workflow_id=row["workflow_id"],
    ) if row["history_operation"] is None else None
    policy_provenance = (
        validate_policy_provenance(row["policy_provenance"], policy=policy)
        if row["history_operation"] is None else None
    )
    _sha256(row["cleanup_context_sha256"], "native cleanup-context digest")
    history_operation = (
        _validate_history_operation(
            row["history_operation"], project_id=project_id, task_id=task_id,
        ) if row["history_operation"] is not None else None
    )
    release = _validate_release(
        row["release"], require_preparer_owner=history_operation is None,
    )
    if history_operation is not None and (
        release["prepared_by_session_id"] != history_operation["operator_session_id"]
    ):
        raise CleanupError("historical release proof differs from its native operator")
    if history_operation is None and (policy is None or policy_provenance is None):
        raise CleanupError("selected workflow inventory has no standing policy")

    supervision = row["supervision"]
    sessions = row["sessions"]
    member_outcomes = row["member_outcomes"]
    worktrees = row["worktrees"]
    containers = row["owned_containers"]
    protected = row["protected_roots"]
    if not isinstance(supervision, list) or len(supervision) > 256:
        raise CleanupError("supervision inventory exceeds its bound")
    if not isinstance(sessions, list) or not sessions or len(sessions) > MAX_SESSIONS:
        raise CleanupError("Session inventory is empty or oversized")
    if not isinstance(member_outcomes, list) or not member_outcomes or len(member_outcomes) > 256:
        raise CleanupError("supervisor Task outcome inventory is empty or oversized")
    if (
        not isinstance(worktrees, list)
        or (not worktrees and history_operation is None)
        or len(worktrees) > MAX_WORKTREES
    ):
        raise CleanupError("worktree inventory is empty or oversized")
    if not isinstance(containers, list) or len(containers) > MAX_CONTAINERS:
        raise CleanupError("owned-container inventory exceeds its bound")
    if not isinstance(protected, list) or not protected or len(protected) > 256:
        raise CleanupError("protected-root inventory is empty or oversized")

    memberships = [validate_supervision_record(item) for item in supervision]
    for item in memberships:
        if item["project_id"] == project_id and task_id not in item["task_ids"]:
            raise CleanupError("subject Task is absent from its supervision record")

    normalized_sessions = [_session_row(item) for item in sessions]
    _unique([item["session_id"] for item in normalized_sessions], "inventory Sessions")
    directless = not any(
        item["project_id"] == project_id and item["task_id"] == task_id
        for item in normalized_sessions
    )
    if directless and history_operation is None:
        raise CleanupError("inventory omits direct Task Sessions")
    outcomes = [_validate_member_outcome(item) for item in member_outcomes]
    _unique([item["task_id"] for item in outcomes], "supervisor Task outcomes")
    outcome_by_task = {item["task_id"]: item for item in outcomes}
    member_ids = {task_id}
    for membership in memberships:
        if task_id in membership["task_ids"]:
            member_ids.update(membership["task_ids"])
    if set(outcome_by_task) != member_ids:
        raise CleanupError("supervisor Task outcomes do not cover exact membership")
    current_outcome = outcome_by_task[task_id]
    if (
        current_outcome["native_state"] != (
            "terminal" if history_operation is not None else "final_disposal"
        )
        or current_outcome["outcome"] != "merged"
        or current_outcome["pull_request_url"] != pr["url"]
        or current_outcome["head_oid"] != pr["head_oid"]
        or current_outcome["merged_at"] != pr["merged_at"]
        or current_outcome["merge_commit"] != pr["merge_commit"]
    ):
        raise CleanupError("subject Task outcome differs from its merged PR proof")
    for member_id, outcome in outcome_by_task.items():
        if outcome["native_state"] == "final_disposal" and member_id != task_id:
            raise CleanupError("another Task cannot be in final disposal")
    if directless:
        _validate_historical_residual(
            normalized_sessions, memberships, outcome_by_task,
            task_id=task_id, project_id=project_id,
        )
    normalized_worktrees = [
        _validate_worktree(
            item, task_id, historical=history_operation is not None,
        )
        for item in worktrees
    ]
    task_worktrees = [item for item in normalized_worktrees if item["role"] == "task"]
    if history_operation is None or task_worktrees:
        if len(task_worktrees) != 1:
            raise CleanupError("inventory must contain exactly one task worktree")
        task_worktree = task_worktrees[0]
        expected_task_dispositions = (
            {"remove", "absent"} if history_operation is not None else {"remove"}
        )
        if (
            task_worktree["project_id"] != project_id
            or task_worktree["workspace_id"] != subject["workspace_id"]
            or task_worktree["worktree_id"] != subject["worktree_id"]
            or task_worktree["path"] != subject["worktree_path"]
            or task_worktree["branch"] != subject["branch"]
            or task_worktree["head_oid"] != subject["head_oid"]
            or task_worktree["branch_oid"] != subject["branch_oid"]
            or task_worktree["disposition"] not in expected_task_dispositions
        ):
            raise CleanupError("task worktree identity differs from the subject")
    elif subject["worktree_id"] is not None:
        raise CleanupError("historical subject worktree identity has no disposition")
    for worktree in normalized_worktrees:
        if worktree["role"] != "supervisor":
            continue
        memberships_for_owner = [
            membership for membership in memberships
            if task_id in membership["task_ids"]
            and worktree["owner_task_id"] in membership["task_ids"]
        ]
        linked_sessions = [
            session for session in normalized_sessions
            if session["workspace_id"] == worktree["workspace_id"]
            and session["worktree_id"] == worktree["worktree_id"]
            and any(
                root_id in session["supervisor_root_ids"]
                for membership in memberships_for_owner
                for root_id in [
                    membership["supervisor_session_id"],
                    *membership["reused_root_session_ids"],
                ]
            )
        ]
        if not linked_sessions:
            raise CleanupError("supervisor worktree is not linked to its registered Session tree")
    normalized_containers = [_validate_container(item, task_id) for item in containers]
    normalized_protected = [_validate_protected(item) for item in protected]
    _unique([item["path"] for item in normalized_worktrees], "worktree paths")
    _unique([item["path"] for item in normalized_containers], "owned container paths")
    _unique([item["path"] for item in normalized_protected], "protected roots")

    unsettled_members = {
        member_id for member_id, outcome in outcome_by_task.items()
        if outcome["native_state"] not in {"terminal", "final_disposal"}
        or outcome["outcome"] not in {"merged", "no-pr", "non-delivery"}
        or (
            outcome["native_state"] == "final_disposal"
            and outcome["outcome"] != "merged"
        )
    }
    for membership in memberships:
        group_unsettled = unsettled_members.intersection(membership["task_ids"])
        root_id = membership["supervisor_session_id"]
        if group_unsettled:
            if any(
                root_id in item["supervisor_root_ids"]
                and item["disposition"] != "shared-in-use"
                for item in normalized_sessions
            ):
                raise CleanupError("shared supervisor Session tree is not held whole")
            if any(
                item["role"] == "supervisor"
                and item["owner_task_id"] in membership["task_ids"]
                and item["disposition"] != "shared-in-use"
                for item in normalized_worktrees
            ):
                raise CleanupError("shared supervisor worktree is not retained")

    for disposable in [
        item["path"] for item in normalized_worktrees if item["disposition"] == "remove"
    ] + [item["path"] for item in normalized_containers]:
        matching_worktree = next(
            (item for item in normalized_worktrees
             if item["path"] == disposable and item["disposition"] == "remove"),
            None,
        )
        matching_container = next(
            (item for item in normalized_containers if item["path"] == disposable),
            None,
        )
        if any(
            _overlaps(disposable, item["path"])
            and not (
                item["kind"] == "native-persistence"
                and (
                    (matching_worktree is not None
                     and _native_worktree_path(disposable, item["path"]))
                    or (
                        matching_container is not None
                        and not _SESSION_ID.fullmatch(Path(disposable).name)
                        and any(
                            session["project_id"] == project_id
                            and session["task_id"] == task_id
                            and Path(disposable).parent
                            == (Path(item["path"]) / session["artifact_relpath"]).parent
                            for session in normalized_sessions
                        )
                        and not any(
                            _overlaps(
                                disposable,
                                str(Path(item["path"]) / session["artifact_relpath"]),
                            )
                            for session in normalized_sessions
                        )
                    )
                )
            )
            for item in normalized_protected
        ):
            raise CleanupError("disposable path overlaps a protected root")
    for index, left in enumerate(normalized_worktrees):
        if left["disposition"] != "remove":
            continue
        for right in normalized_worktrees[index + 1:]:
            if right["disposition"] == "remove" and _overlaps(left["path"], right["path"]):
                raise CleanupError("disposable worktree roots overlap")
    for container in normalized_containers:
        if any(
            item["path"] is not None and _overlaps(container["path"], item["path"])
            for item in normalized_worktrees
        ):
            raise CleanupError("standalone container overlaps a worktree root")

    normalized = {
        **row,
        "project_id": project_id,
        "task_id": task_id,
        "policy": policy,
        "policy_provenance": policy_provenance,
        "cleanup_context_sha256": row["cleanup_context_sha256"],
        "history_operation": history_operation,
        "subject": subject,
        "supervision": memberships,
        "sessions": normalized_sessions,
        "member_outcomes": outcomes,
        "worktrees": normalized_worktrees,
        "owned_containers": normalized_containers,
        "protected_roots": normalized_protected,
    }
    raw = canonical_bytes(normalized)
    digest = hashlib.sha256(raw).hexdigest()
    return ImmutableInventory(raw=raw, sha256=digest, value=normalized)


def _native_worktree_path(path: str, persistence_root: str) -> bool:
    """Permit only the native managed-worktree subtree, never persistence itself."""
    try:
        relative = Path(path).relative_to(Path(persistence_root))
    except ValueError:
        return False
    return len(relative.parts) >= 3 and relative.parts[0] == "worktrees"


def inventory_from_bytes(raw: bytes, expected_sha256: str | None = None) -> ImmutableInventory:
    inventory = validate_inventory(parse_json(raw))
    if expected_sha256 is not None and inventory.sha256 != _sha256(
        expected_sha256, "inventory digest",
    ):
        raise CleanupError("inventory digest differs from the immutable handoff")
    return inventory


def validate_history_inventory(value: Any) -> FiniteHistoryInventory:
    row = _closed(value, {
        "schema", "project_id", "operator_project_id", "task_ids",
        "operator_task_id",
        "operator_session_id", "permission_evidence", "inventories",
    }, "finite historical inventory")
    if row["schema"] != HISTORY_INVENTORY_SCHEMA:
        raise CleanupError("historical inventory schema differs")
    project_id = _uuid(row["project_id"], _PROJECT_ID, "historical inventory Project")
    task_ids = row["task_ids"]
    if not isinstance(task_ids, list) or not task_ids or len(task_ids) > 256:
        raise CleanupError("historical inventory Task selection is empty or oversized")
    for task_id in task_ids:
        _uuid(task_id, _TASK_ID, "historical inventory Task")
    if task_ids != sorted(task_ids) or len(set(task_ids)) != len(task_ids):
        raise CleanupError("historical inventory Task selection must be unique and sorted")
    operator_project_id = _uuid(
        row["operator_project_id"], _PROJECT_ID, "historical operator Project",
    )
    operator_task_id = row["operator_task_id"]
    if operator_task_id is not None:
        _uuid(operator_task_id, _TASK_ID, "historical operator Task")
    operator_session_id = _uuid(
        row["operator_session_id"], _SESSION_ID, "historical operator Session",
    )
    if operator_task_id in task_ids:
        raise CleanupError("historical operator Task cannot be in the victim selection")
    evidence = _closed(row["permission_evidence"], {"reference", "context"},
                       "historical permission evidence")
    _string(evidence["reference"], "historical permission reference", maximum=4096)
    _string(evidence["context"], "historical permission context", maximum=16_384)
    raw_inventories = row["inventories"]
    if (
        not isinstance(raw_inventories, list) or not raw_inventories
        or len(raw_inventories) != len(task_ids)
    ):
        raise CleanupError("historical per-Task inventories do not cover the exact selection")
    inventories = tuple(validate_inventory(item) for item in raw_inventories)
    by_task = {item.value["task_id"]: item for item in inventories}
    if set(by_task) != set(task_ids):
        raise CleanupError("historical per-Task inventory identities differ from selection")
    expected_operation = {
        "schema": "merged-cleanup-history-operation-v1",
        "project_id": project_id,
        "operator_project_id": operator_project_id,
        "operator_task_id": operator_task_id,
        "operator_session_id": operator_session_id,
        "selected_task_ids": task_ids,
        "permission_evidence": evidence,
    }
    for task_id, inventory in by_task.items():
        if (
            inventory.value["project_id"] != project_id
            or inventory.value["history_operation"] != expected_operation
            or inventory.value["policy"] is not None
            or inventory.value["policy_provenance"] is not None
        ):
            raise CleanupError("historical subject inventory has incompatible operation provenance")
    normalized = {
        **row,
        "project_id": project_id,
        "operator_project_id": operator_project_id,
        "task_ids": task_ids,
        "operator_task_id": operator_task_id,
        "operator_session_id": operator_session_id,
        "permission_evidence": evidence,
        "inventories": [by_task[task_id].value for task_id in task_ids],
    }
    raw = canonical_bytes(normalized)
    return FiniteHistoryInventory(
        raw=raw, sha256=hashlib.sha256(raw).hexdigest(),
        value=normalized, inventories=tuple(by_task[task_id] for task_id in task_ids),
    )


def history_inventory_from_bytes(
    raw: bytes, expected_sha256: str | None = None,
) -> FiniteHistoryInventory:
    inventory = validate_history_inventory(parse_json(raw))
    if expected_sha256 is not None and inventory.sha256 != _sha256(
        expected_sha256, "historical inventory digest",
    ):
        raise CleanupError("historical inventory digest differs from confirmation")
    return inventory


def make_handoff(inventory: ImmutableInventory) -> dict[str, Any]:
    value = inventory.value
    handoff = {
        "schema": HANDOFF_SCHEMA,
        "phase": "prepared",
        "project_id": value["project_id"],
        "task_id": value["task_id"],
        "workflow_id": value["workflow_id"],
        "inventory": {
            "namespace": "attachments",
            "sha256": inventory.sha256,
            "byte_size": len(inventory.raw),
        },
    }
    validate_handoff(handoff)
    if len(canonical_bytes(handoff, maximum=MAX_HANDOFF_BYTES)) > MAX_HANDOFF_BYTES:
        raise CleanupError("handoff exceeds the native string limit")
    return handoff


def validate_handoff(value: Any) -> dict[str, Any]:
    row = _closed(value, {
        "schema", "phase", "project_id", "task_id", "workflow_id", "inventory",
    }, "merged cleanup handoff")
    if row["schema"] != HANDOFF_SCHEMA or row["phase"] != "prepared":
        raise CleanupError("only a prepared merged-cleanup handoff is accepted")
    _uuid(row["project_id"], _PROJECT_ID, "handoff project_id")
    _uuid(row["task_id"], _TASK_ID, "handoff task_id")
    _uuid(row["workflow_id"], _WORKFLOW_ID, "handoff workflow_id")
    attachment = _closed(
        row["inventory"], {"namespace", "sha256", "byte_size"}, "inventory attachment",
    )
    if attachment["namespace"] != "attachments":
        raise CleanupError("inventory attachment namespace differs")
    _sha256(attachment["sha256"], "inventory attachment digest")
    if (
        type(attachment["byte_size"]) is not int
        or not 0 < attachment["byte_size"] <= MAX_INVENTORY_BYTES
    ):
        raise CleanupError("inventory attachment size is invalid")
    return row


def make_result(
    inventory: ImmutableInventory,
    *,
    session_dispositions: Sequence[Mapping[str, Any]],
    container_dispositions: Sequence[Mapping[str, Any]],
    worktree_dispositions: Sequence[Mapping[str, Any]],
    postconditions: Mapping[str, Any],
) -> dict[str, Any]:
    value = inventory.value
    expected_dispositions = {
        "sessions": {
            row["session_id"]: (
                "shared-in-use" if row["disposition"] == "shared-in-use" else None
            )
            for row in value["sessions"]
        },
        "containers": {
            row["path"]: (
                "shared-in-use" if row["disposition"] == "shared-in-use" else None
            )
            for row in value["owned_containers"]
        },
        "worktrees": {
            row["worktree_id"]: (
                row["disposition"]
            )
            for row in value["worktrees"]
        },
    }
    observed_dispositions = {
        "sessions": {row["session_id"]: row["disposition"] for row in session_dispositions},
        "containers": {row["path"]: row["disposition"] for row in container_dispositions},
        "worktrees": {row["worktree_id"]: row["disposition"] for row in worktree_dispositions},
    }
    for key, expected in expected_dispositions.items():
        observed = observed_dispositions[key]
        if set(observed) != set(expected) or any(
            observed[identity] not in (
                {disposition} if disposition == "absent"
                else {"shared-in-use"} if disposition == "shared-in-use"
                else {"removed", "absent"}
            )
            for identity, disposition in expected.items()
        ):
            raise CleanupError(f"{key} dispositions do not cover the frozen inventory")
    expected_counts = {
        "sessions_absent": sum(item["disposition"] == "remove" for item in value["sessions"]),
        "session_containers_absent": sum(item["disposition"] == "remove" for item in value["sessions"]),
        "owned_containers_absent": sum(item["disposition"] == "remove" for item in value["owned_containers"]),
        "worktrees_absent": sum(
            item["disposition"] in {"remove", "absent"} for item in value["worktrees"]
        ),
        "branches_retained": sum(item["disposition"] == "remove" for item in value["worktrees"]),
    }
    for key, expected in expected_counts.items():
        if postconditions.get(key) != expected:
            raise CleanupError(f"cleanup postcondition {key} differs from the frozen inventory")
    result = {
        "schema": RESULT_SCHEMA,
        "phase": "completed",
        "project_id": value["project_id"],
        "task_id": value["task_id"],
        "workflow_id": value["workflow_id"],
        "inventory": {
            "namespace": "attachments", "sha256": inventory.sha256,
            "byte_size": len(inventory.raw),
        },
        "endpoint": {
            "repository": value["pull_request"]["repository"],
            "number": value["pull_request"]["number"],
            "base_ref": value["pull_request"]["base_ref"],
            "head_oid": value["pull_request"]["head_oid"],
        },
        "dispositions": {
            "sessions": _compact_dispositions(
                value["sessions"], session_dispositions, "session_id",
            ),
            "containers": _compact_dispositions(
                value["owned_containers"], container_dispositions, "path",
            ),
            "worktrees": _compact_dispositions(
                value["worktrees"], worktree_dispositions, "worktree_id",
            ),
        },
        "postconditions": dict(postconditions),
    }
    validate_result(result)
    canonical_bytes(result, maximum=MAX_HANDOFF_BYTES)
    return result


def validate_result(value: Any) -> dict[str, Any]:
    row = _closed(value, {
        "schema", "phase", "project_id", "task_id", "workflow_id",
        "inventory", "endpoint", "dispositions", "postconditions",
    }, "merged cleanup result")
    if row["schema"] != RESULT_SCHEMA or row["phase"] != "completed":
        raise CleanupError("cleanup result is not completed")
    _uuid(row["project_id"], _PROJECT_ID, "result project_id")
    _uuid(row["task_id"], _TASK_ID, "result task_id")
    _uuid(row["workflow_id"], _WORKFLOW_ID, "result workflow_id")
    attachment = _closed(
        row["inventory"], {"namespace", "sha256", "byte_size"}, "result inventory",
    )
    if attachment["namespace"] != "attachments":
        raise CleanupError("result inventory namespace differs")
    _sha256(attachment["sha256"], "result inventory digest")
    if type(attachment["byte_size"]) is not int or not 0 < attachment["byte_size"] <= MAX_INVENTORY_BYTES:
        raise CleanupError("result inventory size is invalid")
    endpoint = _closed(
        row["endpoint"], {"repository", "number", "base_ref", "head_oid"},
        "result PR endpoint",
    )
    _string(endpoint["repository"], "result repository", maximum=256)
    if type(endpoint["number"]) is not int or endpoint["number"] <= 0:
        raise CleanupError("result PR number is invalid")
    _string(endpoint["base_ref"], "result base ref", maximum=512)
    _oid(endpoint["head_oid"], "result delivered head")
    dispositions = _closed(row["dispositions"], {"sessions", "containers", "worktrees"},
                           "cleanup dispositions")
    for key in ("sessions", "containers", "worktrees"):
        entries = dispositions[key]
        if not isinstance(entries, str) or len(entries) > MAX_CONTAINERS:
            raise CleanupError("cleanup disposition list is invalid")
        if any(code not in "ras" for code in entries):
            raise CleanupError("cleanup disposition code is invalid")
    post = row["postconditions"]
    expected = {
        "sessions_absent", "session_containers_absent", "owned_containers_absent",
        "worktrees_absent", "branches_retained",
    }
    _closed(post, expected, "cleanup postconditions")
    for key in expected:
        if type(post[key]) is not int or post[key] < 0:
            raise CleanupError("cleanup postcondition count is invalid")
    return row


def receipt_subject(inventory: Mapping[str, Any], kind: str, index: int) -> dict[str, Any]:
    lists = {"session": "sessions", "owned": "owned_containers", "worktree": "worktrees"}
    if (
        not isinstance(kind, str) or kind not in lists or type(index) is not int
        or not 0 <= index < len(inventory[lists[kind]])
    ):
        raise CleanupError("receipt exact inventory index is invalid")
    row = inventory[lists[kind]][index]
    if row["disposition"] != "remove":
        raise CleanupError("receipt subject is not eligible for an effect")
    return row


def validate_receipt(
    value: Any, inventory: ImmutableInventory, producer: Mapping[str, Any],
) -> dict[str, Any]:
    row = _closed(value, {
        "schema", "task_id", "inventory_sha256", "producer", "stage", "entries",
    }, "executor receipt")
    canonical_bytes(row, maximum=MAX_RECEIPT_BYTES)
    if (
        not isinstance(row["schema"], str)
        or row["schema"] not in {"merged-cleanup-receipt-v1", RECEIPT_SCHEMA}
        or row["task_id"] != inventory.value["task_id"]
        or row["inventory_sha256"] != inventory.sha256 or row["producer"] != producer
    ):
        raise CleanupError("executor receipt has a foreign subject, inventory, or producer")
    stage = row["stage"]
    if not isinstance(stage, str) or stage not in {"session_native_settled", "container_settled", "worktree_settled",
                     "worktree_native_ack"} or (
        stage == "worktree_native_ack" and row["schema"] != RECEIPT_SCHEMA
    ):
        raise CleanupError("executor receipt stage is invalid")
    if not isinstance(row["entries"], list) or not 1 <= len(row["entries"]) <= 16:
        raise CleanupError("executor receipt entries are empty or oversized")
    for entry in row["entries"]:
        _closed(entry, {"kind", "index", "subject_sha256", "disposition", "proof"},
                "executor receipt entry")
        subject = receipt_subject(inventory.value, entry["kind"], entry["index"])
        if (
            entry["subject_sha256"] != hashlib.sha256(canonical_bytes(subject)).hexdigest()
            or not isinstance(entry["disposition"], str) or entry["disposition"] not in (
                {"acknowledged"} if stage == "worktree_native_ack" else {"removed", "absent"}
            )
        ):
            raise CleanupError("executor receipt does not bind its exact immutable subject")
        proof = entry["proof"]
        if stage == "session_native_settled":
            _closed(proof, {"session_id", "native_status"}, "native Session ACK")
            if (
                entry["kind"] != "session" or entry["disposition"] != "removed"
                or proof != {"session_id": subject["session_id"], "native_status": "ok"}
            ):
                raise CleanupError("receipt lacks the positive native Session ACK")
        elif stage == "container_settled":
            if entry["kind"] not in {"session", "owned"} or proof != {"path_absent": True}:
                raise CleanupError("receipt lacks exact container physical postconditions")
            if type(proof["path_absent"]) is not bool:
                raise CleanupError("receipt physical postcondition is not typed")
        elif stage == "worktree_native_ack":
            _closed(proof, {"incarnation", "native_outcome"}, "native worktree ACK")
            incarnation = _closed(
                proof["incarnation"], {"device", "inode", "ctime_ns"}, "root incarnation",
            )
            if (
                entry["kind"] != "worktree"
                or any(type(v) is not int or v < 0 for v in incarnation.values())
                or incarnation["inode"] == 0
                or not isinstance(proof["native_outcome"], str) or proof["native_outcome"] not in {
                    "NOT_REQUESTED", "NOT_APPLICABLE", "RETAINED", "DELETED",
                    "not_requested", "not_applicable", "retained", "deleted",
                }
            ):
                raise CleanupError("receipt lacks a qualified worktree native ACK")
        else:
            _closed(proof, {
                "incarnation", "path_absent", "git_registration_absent",
                "branch_oid", "native_record",
            }, "worktree physical proof")
            incarnation = _closed(
                proof["incarnation"], {"device", "inode", "ctime_ns"}, "root incarnation",
            )
            if (
                entry["kind"] != "worktree"
                or proof["path_absent"] is not True
                or proof["git_registration_absent"] is not True
                or proof["branch_oid"] != subject["branch_oid"]
                or not isinstance(proof["native_record"], str)
                or proof["native_record"] not in {"absent", "missing"}
                or any(type(v) is not int or v < 0 for v in incarnation.values())
                or incarnation["inode"] == 0
            ):
                raise CleanupError("receipt lacks qualified worktree postconditions")
    return row


def _ordered_sessions(
    sessions: Sequence[Mapping[str, Any]],
) -> list[tuple[int, Mapping[str, Any]]]:
    by_id = {row["session_id"]: row for row in sessions}
    depths = {}
    for start in by_id:
        trail, seen, current = [], set(), start
        while current in by_id and current not in depths:
            if current in seen:
                raise CleanupError("native Session parent graph contains a cycle")
            seen.add(current)
            trail.append(current)
            current = by_id[current]["parent_agent_session_id"]
        depth = depths.get(current, -1)
        for current in reversed(trail):
            depth += 1
            depths[current] = depth
    return sorted(enumerate(sessions), key=lambda pair: (
        -depths[pair[1]["session_id"]], pair[1]["session_id"],
    ))


def execute_inventory(
    inventory: ImmutableInventory,
    backend: CleanupBackend,
) -> dict[str, Any]:
    """Run in dependency order; raise before emitting any terminal transition."""
    value = inventory.value
    # Build the maximum-size result shape before the first native effect. Each
    # settled disposition has a one-byte code, so this also bounds the final
    # terminal carrier independent of which exact disposition is observed.
    _preflight_result_capacity(inventory)
    completed = backend.begin_inventory(inventory)
    if completed is not None:
        return validate_result(completed)
    backend.revalidate(value)
    backend.admit_effects()
    session_dispositions: list[dict[str, str]] = []
    container_dispositions: list[dict[str, str]] = []
    worktree_dispositions: list[dict[str, str]] = []

    # Descendants are deleted before parents. The immutable inventory remains
    # the retry source if any native operation or postcondition is uncertain.
    sessions = list(value["sessions"])
    for index, row in _ordered_sessions(sessions):
        if row["disposition"] == "shared-in-use":
            session_dispositions.append({
                "session_id": row["session_id"], "disposition": "shared-in-use",
            })
            continue
        backend.revalidate(value)
        settled = backend.delete_session(row)
        if settled not in {"removed", "absent"}:
            raise CleanupError("native Session delete did not settle")
        backend.record_settlement(
            "session_native_settled", "session", index, settled,
        )
        backend.revalidate(value)
        physical = backend.remove_session_container(row)
        if physical not in {"removed", "absent"}:
            raise CleanupError("Session residual container did not settle")
        physical = backend.record_settlement(
            "container_settled", "session", index, physical,
        )
        session_dispositions.append({
            "session_id": row["session_id"],
            "disposition": "removed" if settled == "removed" or physical == "removed" else "absent",
        })

    for index, row in enumerate(value["owned_containers"]):
        if row["disposition"] == "shared-in-use":
            container_dispositions.append({"path": row["path"], "disposition": "shared-in-use"})
            continue
        backend.revalidate(value)
        disposition = backend.remove_owned_container(row)
        if disposition not in {"removed", "absent"}:
            raise CleanupError("owned local container did not settle")
        disposition = backend.record_settlement(
            "container_settled", "owned", index, disposition,
        )
        container_dispositions.append({"path": row["path"], "disposition": disposition})

    backend.require_physical_coverage()
    for index, row in enumerate(value["worktrees"]):
        if row["disposition"] == "shared-in-use":
            worktree_dispositions.append({
                "worktree_id": row["worktree_id"], "disposition": "shared-in-use",
            })
            continue
        if row["disposition"] == "absent":
            worktree_dispositions.append({
                "worktree_id": row["worktree_id"], "disposition": "absent",
            })
            continue
        backend.revalidate(value)
        disposition = backend.delete_worktree(row)
        if disposition not in {"removed", "absent"}:
            raise CleanupError("managed worktree deletion did not settle")
        disposition = backend.record_settlement(
            "worktree_settled", "worktree", index, disposition,
        )
        worktree_dispositions.append({
            "worktree_id": row["worktree_id"], "disposition": disposition,
        })

    postconditions = backend.verify_postconditions(value)
    return make_result(
        inventory,
        session_dispositions=session_dispositions,
        container_dispositions=container_dispositions,
        worktree_dispositions=worktree_dispositions,
        postconditions=postconditions,
    )


def output_transition(result: Mapping[str, Any]) -> bytes:
    validate_result(dict(result))
    payload = {
        "transition": "disposal_completed",
        "cleanup_result": canonical_bytes(dict(result), maximum=MAX_HANDOFF_BYTES).decode("utf-8"),
    }
    return canonical_bytes(payload, maximum=MAX_SCRIPT_OUTPUT_BYTES - 1) + b"\n"


def _compact_dispositions(
    inventory_rows: Sequence[Mapping[str, Any]],
    observed_rows: Sequence[Mapping[str, Any]],
    identity_key: str,
) -> str:
    by_id = {row[identity_key]: row["disposition"] for row in observed_rows}
    if len(by_id) != len(observed_rows):
        raise CleanupError("cleanup dispositions contain duplicate identities")
    expected = {row[identity_key] for row in inventory_rows}
    if set(by_id) != expected:
        raise CleanupError("cleanup dispositions do not cover the frozen inventory")
    codes = {"removed": "r", "absent": "a", "shared-in-use": "s"}
    return "".join(codes[by_id[row[identity_key]]] for row in sorted(
        inventory_rows, key=lambda item: item[identity_key],
    ))


def _preflight_result_capacity(inventory: ImmutableInventory) -> dict[str, Any]:
    value = inventory.value
    result = {
        "schema": RESULT_SCHEMA,
        "phase": "completed",
        "project_id": value["project_id"],
        "task_id": value["task_id"],
        "workflow_id": value["workflow_id"],
        "inventory": {
            "namespace": "attachments", "sha256": inventory.sha256,
            "byte_size": len(inventory.raw),
        },
        "endpoint": {
            "repository": value["pull_request"]["repository"],
            "number": value["pull_request"]["number"],
            "base_ref": value["pull_request"]["base_ref"],
            "head_oid": value["pull_request"]["head_oid"],
        },
        "dispositions": {
            "sessions": "r" * len(value["sessions"]),
            "containers": "r" * len(value["owned_containers"]),
            "worktrees": "r" * len(value["worktrees"]),
        },
        "postconditions": {
            "sessions_absent": len(value["sessions"]),
            "session_containers_absent": len(value["sessions"]),
            "owned_containers_absent": len(value["owned_containers"]),
            "worktrees_absent": len(value["worktrees"]),
            "branches_retained": len(value["worktrees"]),
        },
    }
    validate_result(result)
    canonical_bytes(result, maximum=MAX_HANDOFF_BYTES)
    canonical_bytes({
        "transition": "disposal_completed",
        "cleanup_result": canonical_bytes(
            result, maximum=MAX_HANDOFF_BYTES,
        ).decode("utf-8"),
    }, maximum=MAX_SCRIPT_OUTPUT_BYTES - 1)
    return result


def _preflight_history(inventory: FiniteHistoryInventory, backend: CleanupBackend) -> None:
    """Admit every member and the aggregate before any member can mutate."""
    results = [_preflight_result_capacity(item) for item in inventory.inventories]
    canonical_bytes({
        "schema": "merged-cleanup-history-result-v1", "phase": "completed",
        "project_id": inventory.value["project_id"],
        "task_ids": inventory.value["task_ids"], "inventory_sha256": inventory.sha256,
        "task_results": results,
    }, maximum=MAX_SCRIPT_OUTPUT_BYTES - 1)
    previous = []
    previous_sessions = set()
    for member_index, item in enumerate(inventory.inventories):
        value = item.value
        paths = [
            Path(row["path"]) for rows in (
                value["worktrees"], value["owned_containers"],
            ) for row in rows if row.get("path")
        ]
        protected = [(Path(row["path"]), row["kind"]) for row in value["protected_roots"]]
        persistence = getattr(backend, "context", {}).get("persistence_root")
        if persistence:
            paths.extend(Path(persistence) / row["artifact_relpath"] for row in value["sessions"])
        operation = value["history_operation"]
        required_sessions = {row["session_id"] for row in value["sessions"]}
        if operation is not None:
            required_sessions.add(operation["operator_session_id"])
            if persistence:
                paths.append(
                    Path(persistence) / "projects" / operation["operator_project_id"]
                    / "sessions" / operation["operator_session_id"])
        if previous_sessions.intersection(required_sessions):
            raise CleanupError("historical members share removable Sessions")
        if any(a == b or a in b.parents or b in a.parents for a in previous for b in paths):
            raise CleanupError("earlier historical removal invalidates a later member dependency")
        if any(
            a == b or a in b.parents or (kind != "native-persistence" and b in a.parents)
            for a in previous for b, kind in protected
        ):
            raise CleanupError("earlier historical removal invalidates a later protected read")
        current = [Path(row["path"]) for rows in (
            value["worktrees"], value["owned_containers"],
        ) for row in rows if row["disposition"] == "remove" and row["path"]]
        # Also reject later removals overlapping an earlier removable resource.
        previous.extend(current)
        previous_sessions.update(row["session_id"] for row in value["sessions"]
                                 if row["disposition"] == "remove")
        if persistence:
            previous.extend(Path(persistence) / row["artifact_relpath"]
                            for row in value["sessions"] if row["disposition"] == "remove")
        _ordered_sessions(value["sessions"])
        completed = backend.begin_inventory(item)
        if completed is not None:
            results[member_index] = validate_result(completed)
            continue
        backend.revalidate(value)
        backend.admit_effects()
    # Native terminal replay can return an already-applied result. Bound those
    # actual bytes too, rather than assuming its endpoint equals our template.
    canonical_bytes({
        "schema": "merged-cleanup-history-result-v1", "phase": "completed",
        "project_id": inventory.value["project_id"],
        "task_ids": inventory.value["task_ids"], "inventory_sha256": inventory.sha256,
        "task_results": results,
    }, maximum=MAX_SCRIPT_OUTPUT_BYTES - 1)


def _read_bounded(stream, maximum: int) -> bytes:
    raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise CleanupError("cleanup input exceeds its byte limit")
    return raw


def _control_path(path: Path) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute() or str(target) != str(path.expanduser()) or ".." in target.parts:
        raise CleanupError("historical control path must be absolute and normalized")
    current = Path(target.anchor)
    for index, part in enumerate(target.parts[1:]):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if index == len(target.parts[1:]) - 1:
                continue
            raise CleanupError("historical control path parent is absent") from None
        except OSError as error:
            raise CleanupError("historical control path cannot be inspected") from error
        if os.path.islink(current):
            raise CleanupError("historical control path traverses a symlink")
        if index < len(target.parts[1:]) - 1 and not current.is_dir():
            raise CleanupError("historical control path parent is not a directory")
    return target


def _read_control_file(path: Path, maximum: int) -> bytes:
    target = _control_path(path)
    try:
        before = target.lstat()
    except OSError as error:
        raise CleanupError("historical control file is unavailable") from error
    if (
        not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
        or before.st_uid != os.getuid() or before.st_mode & 0o077
        or before.st_size < 0 or before.st_size > maximum
    ):
        raise CleanupError("historical control file identity or permissions are unsafe")
    try:
        descriptor = os.open(
            target,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
    except OSError as error:
        raise CleanupError("historical control file changed while opening") from error
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1 or opened.st_uid != os.getuid()
            or opened.st_mode & 0o077 or opened.st_size > maximum
        ):
            raise CleanupError("historical control file changed while opening")
        chunks = bytearray()
        while len(chunks) <= maximum:
            part = os.read(descriptor, min(65_536, maximum + 1 - len(chunks)))
            if not part:
                break
            chunks.extend(part)
        after = os.fstat(descriptor)
        if (
            len(chunks) > maximum
            or (opened.st_dev, opened.st_ino, opened.st_size)
            != (after.st_dev, after.st_ino, after.st_size)
            or len(chunks) != after.st_size
        ):
            raise CleanupError("historical control file changed or exceeded its bound")
        return bytes(chunks)
    finally:
        os.close(descriptor)


def _assert_control_file_outside_inventory(
    path: Path, inventory: ImmutableInventory | FiniteHistoryInventory,
) -> Path:
    target = _control_path(path)
    inventories = (
        inventory.inventories
        if isinstance(inventory, FiniteHistoryInventory)
        else (inventory,)
    )
    victim_roots: list[Path] = []
    for item in inventories:
        value = item.value
        victim_roots.extend(
            Path(row["path"]) for row in value["worktrees"]
            if row["disposition"] == "remove"
        )
        victim_roots.extend(
            Path(row["path"]) for row in value["owned_containers"]
            if row["disposition"] == "remove"
        )
        persistence = next(
            (Path(row["path"]) for row in value["protected_roots"]
             if row["kind"] == "native-persistence"),
            None,
        )
        if persistence is not None:
            victim_roots.extend(
                persistence / Path(*row["artifact_relpath"].split("/"))
                for row in value["sessions"] if row["disposition"] == "remove"
            )
    if any(_overlaps(str(target), str(root)) for root in victim_roots):
        raise CleanupError("historical control file overlaps a disposable root")
    return target


def _native_backend():
    from .cleanup_native import NativeCleanupBackend
    return NativeCleanupBackend()


def _command_main(arguments: Sequence[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="cleanup-merged-task",
        description="Opt-in merged Task cleanup; native identity and policy remain authoritative.",
        epilog=(
            "Modes: prepare and register-supervisor read one closed JSON object from stdin. "
            "history-inspect reads a private request file and writes a finite inventory; "
            "history-apply requires that inventory's exact SHA-256 confirmation. "
            "No mode infers approval from request prose or digest confirmation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)
    prepare = subparsers.add_parser(
        "prepare",
        help="prepare the current native Task from a JSON request on stdin",
        description="Read a closed prepare request from stdin; cleanup_context comes from native input.",
        epilog=(
            '{"schema":"merged-cleanup-prepare-request-v1",'
            '"pull_request_url":"https://github.com/OWNER/REPO/pull/123",'
            '"expected_base_ref":"release/1.0","release":'
            '{"prepared_by_session_id":"00000000-0000-4000-8000-000000000001",'
            '"owner_session_ids":["00000000-0000-4000-8000-000000000001"]}}\n'
            "Optional owned_containers: exact {path,kind,owner_task_id,evidence_reference} "
            "objects for proven current-Task siblings; kind is archive or evidence. "
            "Paths must be in the native task-worktree parent or its native Sessions "
            "parent, never a whole parent or a native Session directory. No globs.\n"
            "Invocation: scripts/cleanup-merged-task prepare < request.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    register = subparsers.add_parser(
        "register-supervisor",
        help="register native supervisor Task membership from JSON on stdin",
        description="Read one closed supervisor-membership request from stdin.",
        epilog=(
            '{"schema":"merged-cleanup-register-request-v1",'
            '"task_ids":["task-00000000-0000-4000-8000-000000000001"],'
            '"reused_root_session_ids":[]}\n'
            "Invocation: scripts/cleanup-merged-task register-supervisor < request.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    inspect = subparsers.add_parser(
        "history-inspect",
        help="freeze an exact finite historical Task selection",
        description="Inspect terminal Tasks without moving or reopening them.",
        epilog=(
            '{"schema":"merged-cleanup-history-request-v1",'
            '"project_id":"project-00000000-0000-4000-8000-000000000001",'
            '"task_ids":["task-00000000-0000-4000-8000-000000000001"],'
            '"permission_evidence":{"reference":"original decision locator",'
            "\"context\":\"assigned operator's verified scope\"},"
            '"owned_containers":[{"path":"/private/exact/archive.json",'
            '"kind":"archive","owner_task_id":"task-00000000-0000-4000-8000-000000000001",'
            '"evidence_reference":"original ownership map locator"}]}\n'
            "Invocation: scripts/cleanup-merged-task history-inspect "
            "--request /private/history-request.json "
            "--inventory-out /private/history-inventory.json\n"
            "The assigned operator verifies original permission and each exact artifact owner. "
            "Only archive/evidence paths are accepted; no globs or discovery. "
            "Evidence references are audit locators, not authority."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    inspect.add_argument(
        "--request", required=True, type=Path,
        help="absolute private JSON request file; never place it in a victim root",
    )
    inspect.add_argument(
        "--inventory-out", required=True, type=Path,
        help="new absolute private inventory file outside all disposable roots",
    )
    apply = subparsers.add_parser(
        "history-apply",
        help="apply a previously inspected finite inventory",
        description="Apply only the exact inventory after native revalidation.",
        epilog=(
            "Invocation: scripts/cleanup-merged-task history-apply "
            "--inventory /private/history-inventory.json --confirm INVENTORY_SHA256\n"
            "--confirm must equal the SHA-256 emitted by history-inspect; it grants no authority."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    apply.add_argument("--inventory", required=True, type=Path)
    apply.add_argument(
        "--confirm", required=True, metavar="INVENTORY_SHA256",
        help="exact digest emitted by history-inspect (identity binding only)",
    )
    args = parser.parse_args(list(arguments))
    backend = _native_backend()

    if args.mode == "prepare":
        request = parse_json(_read_bounded(sys.stdin.buffer, MAX_HANDOFF_BYTES),
                             maximum=MAX_HANDOFF_BYTES)
        handoff = backend.prepare(request)
        output = canonical_bytes(handoff, maximum=MAX_HANDOFF_BYTES) + b"\n"
        sys.stdout.buffer.write(output)
        return 0
    if args.mode == "register-supervisor":
        request = parse_json(_read_bounded(sys.stdin.buffer, MAX_HANDOFF_BYTES),
                             maximum=MAX_HANDOFF_BYTES)
        result = backend.register_supervisor(request)
        sys.stdout.buffer.write(canonical_bytes(result, maximum=MAX_SCRIPT_OUTPUT_BYTES) + b"\n")
        return 0
    if args.mode == "history-inspect":
        request = parse_json(_read_control_file(args.request, MAX_HANDOFF_BYTES),
                             maximum=MAX_HANDOFF_BYTES)
        inventory = backend.inspect_history(request)
        _assert_control_file_outside_inventory(args.request, inventory)
        _assert_control_file_outside_inventory(args.inventory_out, inventory)
        _write_inventory_file(args.inventory_out, inventory)
        sys.stdout.buffer.write(canonical_bytes({
            "schema": HISTORY_INVENTORY_SCHEMA, "sha256": inventory.sha256,
            "byte_size": len(inventory.raw), "task_count": len(inventory.inventories),
            "session_count": sum(len(item.value["sessions"]) for item in inventory.inventories),
        }) + b"\n")
        return 0
    if args.mode == "history-apply":
        raw_inventory = _read_control_file(args.inventory, MAX_INVENTORY_BYTES)
        inventory = history_inventory_from_bytes(raw_inventory, args.confirm)
        _assert_control_file_outside_inventory(args.inventory, inventory)
        _preflight_history(inventory, backend)
        results = [
            execute_inventory(item, backend) for item in inventory.inventories
        ]
        result = {
            "schema": "merged-cleanup-history-result-v1",
            "phase": "completed",
            "project_id": inventory.value["project_id"],
            "task_ids": inventory.value["task_ids"],
            "inventory_sha256": inventory.sha256,
            "task_results": results,
        }
        sys.stdout.buffer.write(
            canonical_bytes(result, maximum=MAX_SCRIPT_OUTPUT_BYTES - 1) + b"\n",
        )
        return 0
    raise CleanupError("unsupported cleanup mode")


def _write_inventory_file(
    path: Path, inventory: ImmutableInventory | FiniteHistoryInventory,
) -> None:
    target = _assert_control_file_outside_inventory(path, inventory)
    if target.exists() or target.is_symlink():
        raise CleanupError("historical inventory destination already exists")
    parent_info = target.parent.lstat()
    if (
        not os.path.isdir(target.parent) or os.path.islink(target.parent)
        or parent_info.st_uid != os.getuid() or parent_info.st_mode & 0o022
    ):
        raise CleanupError("historical inventory parent is not canonical")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    try:
        created = os.fstat(descriptor)
        if (
            created.st_nlink != 1 or created.st_uid != os.getuid()
            or created.st_mode & 0o077
        ):
            raise CleanupError("historical inventory file permissions are unsafe")
        offset = 0
        while offset < len(inventory.raw):
            count = os.write(descriptor, inventory.raw[offset:])
            if count <= 0:
                raise CleanupError("historical inventory write made no progress")
            offset += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    parent_descriptor = os.open(
        target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def main() -> int:
    try:
        if sys.argv[1:]:
            return _command_main(sys.argv[1:])
        payload = parse_json(_read_bounded(sys.stdin.buffer, MAX_NATIVE_SCRIPT_INPUT_BYTES),
                             maximum=MAX_NATIVE_SCRIPT_INPUT_BYTES)
        required = {"_kent", "cleanup_result"}
        if not isinstance(payload, dict) or set(payload) not in (
            required, required | {"commentary"},
        ):
            raise CleanupError("native final Script input is incomplete")
        if "commentary" in payload and (
            not isinstance(payload["commentary"], str)
            or len(payload["commentary"].encode("utf-8")) > 65_536
        ):
            raise CleanupError("native final Script commentary is invalid")
        backend = _native_backend()
        handoff = payload["cleanup_result"]
        if not isinstance(handoff, str) or len(handoff.encode("utf-8")) > MAX_HANDOFF_BYTES:
            raise CleanupError("native final Script handoff is invalid")
        parsed_handoff = validate_handoff(parse_json(
            handoff.encode("utf-8"), maximum=MAX_HANDOFF_BYTES,
        ))
        inventory = backend.load_handoff(parsed_handoff, payload["_kent"])
        result = execute_inventory(inventory, backend)
        sys.stdout.buffer.write(output_transition(result))
        return 0
    except (CleanupError, OSError, ValueError, RuntimeError) as error:
        print(f"cleanup-merged-task: blocked: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
