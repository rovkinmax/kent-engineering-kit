"""Dynamic CI preparation, report archival and GitHub source normalization.

Ordinary preparation binds a stable GitHub pull-request identity and creates
the dynamic CI cycle contract. It does not inspect release source jobs,
materialize commits, or infer expected checks. The standalone source
normalizer remains available to release-only callers.
Input is duplicate-free JSON, <=1 MiB/depth 100, read within two seconds.
Workspace, canonical GitHub PR URL, branch, strategy and task short ID are real
nonempty strings. Invalid identity exits nonzero with a safe error, no transition.
Errors never contain source snippets, authenticated output or subprocess stderr.

ci_prepare_failed is preparation diagnosis, NOT a new CI report schema.
It preserves all identity/cursor/packet strings; absent contract/report packets
are empty strings ONLY on diagnosis and its retry back to preparation. Supplied
invalid reports remain invalid. CI Monitor has no positive-green route.
Ready-initial omits the absent report; ready-retry carries a validated same-cycle
github-ci-report-v3. Both carry the contract, task ID and cursor.

Metadata matches the exact PR URL/head branch; re-reading preserves the base
branch and both commits. Merged recovery requires timestamp and merge-commit
proof. Same head/base preserves report attempts; a changed head or base starts
a new cycle, retaining old reports in the task evidence area.

The archive_ci_report CLI operation validates identity/report and returns only
ci_report_artifact, or a nonzero safe error. Artifacts are content-addressed
ci-report-<sha256>.json files under .kent/runtime/<task>, <=64 reports of <=64 KiB,
with an existing evidence-ledger entry, no new journal or policy authority store.
The ledger must be ignored, unsealed and safe; failures cannot yield green CI.
Terminal CI observations archive before transition; preparation retains prior
reports before retry/new-cycle transitions. Archive reads are bounded to 1 MiB
of existing ledger/256 task-directory entries. Runtime validation remains pure.

Supported runner assertion: unconditional, non-continue-on-error, standalone
POSIX test in a default/sh/bash step, using either $RUNNER_ENVIRONMENT or the
existing Puber ${RUNNER_ENVIRONMENT:-github-hosted} convention. Exact quoted
spellings are below. Substrings, conditional steps, masks and other shells do
not assert runner trust. Empty shell remains empty in the normalized DTO.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import selectors
import signal
import shutil
import stat
import subprocess
import sys
import time
import tomllib
import types
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from . import revision as revision_contracts
from . import github_observation as provider_observation
from .github_observation import ObservationError
from .revision import (
    owned_process_group_exists,
)
from .runtime import (
    RuntimeContractError,
    canonical_bytes,
    validate_ci_contract,
    validate_dynamic_ci_report,
    validate_dynamic_pr_feedback_cursor,
    MAX_CI_REPORT_BYTES,
    delivery_context_fields,
    validate_delivery_context,
)
from . import runtime as runtime_contracts


MAX_GITHUB_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_WORKFLOW_BYTES = 1024 * 1024
MAX_RUBY_OUTPUT_BYTES = 2 * 1024 * 1024
PREPARE_TIMEOUT_SECONDS = 60.0
MAX_PREPARATION_INPUT_BYTES = 1024 * 1024
SHA1_HEX = set("0123456789abcdef")
SCRIPT_CI_ARCHIVE_READER_PROOF_SCHEMA = "script-ci-archive-reader-proof-v1"
MAX_ARCHIVE_READER_PATHS = 128
MAX_ARCHIVE_READER_FILE_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_READER_TOTAL_BYTES = 32 * 1024 * 1024
MAX_RERUN_SOURCE_JOBS = 10_000
CI_RERUN_ALLOWED_EFFECTS = frozenset({"infrastructure", "test"})

_ARCHIVE_PROJECT_COMMANDS = {
    "runtime_contracts": "workflowkit/runtime.py",
    "evidence": "templates/project/workflow-evidence-ledger",
    "checkpoint": "templates/project/workflow-checkpoint",
    "janitor": "templates/project/workflow-task-janitor",
    "plan_contract": "templates/project/workflow-plan-contract",
    "plan_contract_accept": "templates/project/workflow-plan-contract-accept",
    "plan_contract_continue": "templates/project/workflow-plan-contract-continue",
    "plan_contract_fix_continue": "templates/project/workflow-plan-contract-fix-continue",
    "plan_contract_verify": "templates/project/workflow-plan-contract-verify",
    "prepare_ci": "templates/project/workflow-prepare-github-ci",
    "wait_ci": "templates/project/workflow-wait-github-ci",
    "wait_pr": "templates/project/workflow-wait-github-pr",
    "github_observation": "workflowkit/github_observation.py",
}
_ARCHIVE_KIT_MODULE_PATHS = (
    "scripts/prepare-github-ci",
    "workflowkit/__init__.py",
    "workflowkit/ci_contract.py",
    "workflowkit/delivery.py",
    "workflowkit/github_observation.py",
    "workflowkit/runtime.py",
    "workflowkit/revision.py",
    "workflowkit/profile.py",
    "workflowkit/release.py",
    "workflowkit/model.py",
    "workflowkit/kent.py",
    "workflowkit/graph.py",
)
_ARCHIVE_KIT_TEMPLATE_PATHS = (
    "templates/project/workflow-evidence-ledger",
    "templates/project/workflow-checkpoint",
    "templates/project/workflow-task-janitor",
    "templates/project/workflow-plan-contract",
    "templates/project/workflow-plan-contract-accept",
    "templates/project/workflow-plan-contract-continue",
    "templates/project/workflow-plan-contract-fix-continue",
    "templates/project/workflow-plan-contract-verify",
    "templates/project/workflow-prepare-github-ci",
    "templates/project/workflow-wait-github-ci",
    "templates/project/workflow-wait-github-pr",
    ".kent/commands/cleanup-task.md",
)


class CiContractError(RuntimeError):
    """Raised when CI preparation or source admission cannot prove its contract."""


class BoundedCommandError(CiContractError):
    def __init__(
        self,
        code: str,
        *,
        exit_code: int | None = None,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def _native_read_adapter(root: Path):
    executable = shutil.which("kent")
    if executable is None:
        raise CiContractError("native_identity_unavailable")
    kent = Path(executable).resolve(strict=True)
    if not kent.is_absolute() or not kent.is_file():
        raise CiContractError("native_identity_unavailable")

    def native_read(
        workspace_root: Path,
        arguments: list[str],
        *,
        structured: bool = True,
    ):
        if workspace_root != root:
            raise CiContractError("native_identity_workspace_mismatch")
        try:
            result = run_bounded_command(
                [str(kent), *arguments],
                cwd=root,
                timeout=15.0,
                output_limit=MAX_PREPARATION_INPUT_BYTES,
            )
            if result.returncode != 0:
                raise CiContractError("native_identity_unavailable")
            if structured:
                value = parse_preparation_json(result.stdout)
                if not isinstance(value, dict):
                    raise CiContractError("native_identity_invalid")
                return value
            return result.stdout.decode("utf-8").strip()
        except (OSError, UnicodeError, ValueError) as error:
            raise CiContractError("native_identity_unavailable") from error

    return native_read


def observe_script_delivery_protocol(
    payload: Mapping[str, Any],
    *,
    allow_legacy_archive_without_native_identity: bool = False,
) -> tuple[object | None, str | None, bool, str | None, object | None]:
    """Select current CI/target contracts from a fresh native Script graph."""
    workspace = payload.get("workspace_path")
    task = payload.get("task_short_id")
    if (
        not isinstance(workspace, str)
        or not Path(workspace).is_absolute()
    ):
        raise CiContractError("input_identity_invalid")
    root = Path(workspace).resolve()
    profile_path = root / ".kent/workflow-profile.toml"
    try:
        profile_metadata = profile_path.lstat()
    except FileNotFoundError:
        return None, None, False, None, None
    if not stat.S_ISREG(profile_metadata.st_mode):
        raise CiContractError("profile_unsafe")
    try:
        descriptor = os.open(
            profile_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or metadata.st_mode & 0o7022
                or metadata.st_size > 64 * 1024
            ):
                raise CiContractError("profile_unsafe")
            raw_profile = os.read(descriptor, 64 * 1024 + 1)
            after = os.fstat(descriptor)
            if (
                len(raw_profile) > 64 * 1024
                or len(raw_profile) != metadata.st_size
                or (metadata.st_dev, metadata.st_ino, metadata.st_size,
                    metadata.st_mtime_ns, metadata.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size,
                    after.st_mtime_ns, after.st_ctime_ns)
            ):
                raise CiContractError("profile_changed")
        finally:
            os.close(descriptor)
        if len(raw_profile) > 64 * 1024:
            raise CiContractError("profile_limit")
        selected_values = tomllib.loads(raw_profile.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise CiContractError("profile_invalid") from error
    if not isinstance(selected_values, dict):
        raise CiContractError("profile_invalid")
    profile_policies = selected_values.get("policies", {})
    command_versions = selected_values.get("command_versions", {})
    managed_commands = selected_values.get("kit_managed_commands", [])
    if (
        allow_legacy_archive_without_native_identity
        and payload.get("operation") == "archive_ci_report"
        and "_kent" not in payload
        and selected_values.get("schema_version") == 4
        and isinstance(profile_policies, dict)
        and profile_policies.get("delivery_reliability") == "v1"
        and isinstance(command_versions, dict)
        and command_versions.get("runtime_contracts") == "2.0.0"
        and command_versions.get("prepare_ci") == "2.0.0"
        and isinstance(managed_commands, list)
        and {"runtime_contracts", "prepare_ci"} <= set(managed_commands)
    ):
        # Existing schema-4 CI helpers use archive_ci_report without native
        # identity. Preserve that explicit legacy boundary; a typed-version
        # profile still takes the native Script/graph path below.
        return None, "legacy", False, None, None
    current_family = (
        isinstance(profile_policies, dict)
        and profile_policies.get("delivery_reliability") == "v1"
    ) or (
        isinstance(command_versions, dict)
        and (
            command_versions.get("runtime_contracts") in {"2.1.0", "2.2.0"}
            or command_versions.get("prepare_ci") in {"3.0.0", "3.1.0"}
        )
    )
    if not current_family:
        return None, None, False, None, None
    if (
        str(root) != workspace
        or Path.cwd().resolve() != root
        or not isinstance(task, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", task)
    ):
        raise CiContractError("input_identity_invalid")
    from .profile import ProjectProfile

    profile = ProjectProfile.from_toml(
        root,
        raw_profile.decode("utf-8"),
        source=str(profile_path),
    )
    native_context = payload.get("_kent")
    if native_context is None:
        raise CiContractError("native_script_identity_required")
    if not isinstance(native_context, dict):
        raise CiContractError("native_script_identity_invalid")
    native_read = _native_read_adapter(root)
    from . import delivery

    observed = delivery.observe_script_delivery_graph(
        root,
        task,
        native_context,
        native_read=native_read,
    )
    protocol = delivery.prepare_ci_protocol(observed)
    require_target = delivery.pr_base_branch_required(observed)
    context_schema = delivery.delivery_context_schema(observed)
    return observed, protocol, require_target, context_schema, native_read


def _archive_relative_path(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\\" in value
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or Path(value).as_posix() != value
    ):
        raise CiContractError(f"{label}_invalid")
    return value


def _archive_project_reader_bindings(profile, *, script_key: str, script_path: str):
    required = {
        "runtime_contracts",
        "evidence",
        "checkpoint",
        "plan_contract",
    }
    if profile.capability("managed_worktrees"):
        required.add("janitor")
    if profile.capability("pull_requests"):
        required.add("wait_pr")
    if profile.capability("ci_monitoring"):
        required.add("wait_ci")
    if profile.source_ci_contract():
        required.add("prepare_ci")
    if profile.command("github_observation"):
        required.add("github_observation")
    required.update(
        key for key in (
            "plan_contract_accept",
            "plan_contract_continue",
            "plan_contract_fix_continue",
            "plan_contract_verify",
        )
        if profile.command(key)
    )

    project_to_kit: dict[str, str] = {
        ".kent/workflow-profile.toml": "",
    }

    def bind_project_reader(project_path: str, canonical_path: str) -> None:
        previous = project_to_kit.get(project_path)
        if previous is not None and previous != canonical_path:
            raise CiContractError("archive_reader_route_unknown")
        project_to_kit[project_path] = canonical_path

    managed_commands = set(profile.kit_managed_commands)
    for key in sorted(required):
        configured = profile.command(key)
        if not configured:
            raise CiContractError("archive_reader_route_unknown")
        project_path = _archive_relative_path(
            configured,
            label="archive_reader_path",
        )
        canonical = ""
        if key in managed_commands:
            canonical = _ARCHIVE_PROJECT_COMMANDS.get(key, "")
            if not canonical:
                raise CiContractError("archive_reader_route_unknown")
        bind_project_reader(project_path, canonical)

    cleanup = profile.procedure("cleanup")
    if not cleanup:
        raise CiContractError("archive_reader_route_unknown")
    cleanup_path = _archive_relative_path(cleanup, label="archive_reader_path")
    bind_project_reader(cleanup_path, "")

    configured_script = profile.command(script_key)
    if not configured_script:
        raise CiContractError("archive_reader_route_unknown")
    configured_script = _archive_relative_path(
        configured_script,
        label="archive_reader_path",
    )
    selected_script = _archive_relative_path(
        script_path,
        label="archive_reader_path",
    )
    canonical_script = _ARCHIVE_PROJECT_COMMANDS.get(script_key)
    if script_key in managed_commands and canonical_script is None:
        raise CiContractError("archive_reader_route_unknown")
    if configured_script == selected_script:
        bind_project_reader(
            configured_script,
            canonical_script if script_key in managed_commands else "",
        )
    else:
        # Some projects bind a native CI Script node to a project-owned
        # wrapper while keeping the configured Kit command as its managed
        # child. Admit only wrappers explicitly included in the bounded
        # archive-reader closure; the native graph still supplies the active
        # script identity, and the configured managed child retains parity.
        if selected_script not in profile.ci_archive_reader_sources:
            raise CiContractError("native_script_path_mismatch")
        bind_project_reader(selected_script, "")

    for extra in profile.ci_archive_reader_sources:
        extra_path = _archive_relative_path(
            extra,
            label="archive_reader_path",
        )
        project_to_kit.setdefault(extra_path, "")
    if len(project_to_kit) > MAX_ARCHIVE_READER_PATHS:
        raise CiContractError("archive_reader_limit")
    return project_to_kit


def _archive_protocol_projection(graph: dict, delivery) -> dict[str, Any]:
    definition = graph.get("graph") if isinstance(graph, dict) else None
    if not isinstance(definition, dict):
        raise CiContractError("native_graph_invalid")
    nodes = definition.get("nodes")
    groups = definition.get("transition_groups")
    edges = definition.get("edges")
    if (
        not isinstance(nodes, list)
        or len(nodes) > 4096
        or not isinstance(groups, list)
        or len(groups) > 8192
        or not isinstance(edges, list)
        or len(edges) > 16384
    ):
        raise CiContractError("native_graph_invalid")
    by_id = {}
    by_key = {}
    for node in nodes:
        if not isinstance(node, dict):
            raise CiContractError("native_graph_invalid")
        identifier = node.get("id")
        key = node.get("key")
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in by_id
            or not isinstance(key, str)
            or not key
            or key in by_key
        ):
            raise CiContractError("native_graph_invalid")
        by_id[identifier] = node
        by_key[key] = node
    relevant_keys = {
        "prepare_pr",
        "waiting_pr",
        "merge_watch",
        "ci_prepare",
        "ci_watch",
        "ci_monitor",
        "task_janitor",
    }
    relevant_ids = {
        node["id"] for key, node in by_key.items() if key in relevant_keys
    }
    projected_nodes = [
        node for key, node in by_key.items() if key in relevant_keys
    ]
    group_by_id = {}
    for group in groups:
        if not isinstance(group, dict):
            raise CiContractError("native_graph_invalid")
        identifier = group.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in group_by_id:
            raise CiContractError("native_graph_invalid")
        group_by_id[identifier] = group
    projected_groups = [
        group
        for group in groups
        if group.get("source_node_id") in relevant_ids
    ]
    projected_edges = [
        edge
        for edge in edges
        if isinstance(edge, dict)
        and (
            edge.get("target_node_id") in relevant_ids
            or group_by_id.get(edge.get("transition_group_id"), {}).get(
                "source_node_id"
            ) in relevant_ids
        )
    ]
    if len(projected_edges) > 16384:
        raise CiContractError("native_graph_invalid")
    projection = {
        "workflow_id": graph.get("workflow_id"),
        "nodes": sorted(projected_nodes, key=lambda item: item["key"]),
        "transition_groups": sorted(
            projected_groups,
            key=lambda item: item["id"],
        ),
        "edges": sorted(projected_edges, key=lambda item: item.get("id", "")),
    }
    try:
        encoded = canonical_bytes(projection)
    except (TypeError, ValueError) as error:
        raise CiContractError("native_graph_invalid") from error
    if len(encoded) > MAX_PREPARATION_INPUT_BYTES:
        raise CiContractError("native_graph_invalid")
    return projection


def _read_archive_source(root: Path, path: str) -> tuple[str, bytes]:
    candidate = root
    parts = path.split("/")
    for index, part in enumerate(parts):
        candidate = candidate / part
        try:
            metadata = candidate.lstat()
        except OSError as error:
            raise CiContractError("archive_reader_missing") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise CiContractError("archive_reader_unsafe")
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise CiContractError("archive_reader_unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(candidate, flags)
    except OSError as error:
        raise CiContractError("archive_reader_unsafe") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.getuid()
            or before.st_mode & 0o7022
            or before.st_mode & 0o7000
            or before.st_size < 0
            or before.st_size > MAX_ARCHIVE_READER_FILE_BYTES
        ):
            raise CiContractError("archive_reader_unsafe")
        raw = bytearray()
        while True:
            chunk = os.read(
                descriptor,
                min(65536, MAX_ARCHIVE_READER_FILE_BYTES + 1 - len(raw)),
            )
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_ARCHIVE_READER_FILE_BYTES:
                raise CiContractError("archive_reader_limit")
        after = os.fstat(descriptor)
        if (
            len(raw) != before.st_size
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
                before.st_mode,
                before.st_uid,
                before.st_nlink,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_mode,
                after.st_uid,
                after.st_nlink,
            )
        ):
            raise CiContractError("archive_reader_changed")
        mode = "100755" if before.st_mode & 0o111 else "100644"
        return mode, bytes(raw)
    finally:
        os.close(descriptor)


def _archive_reader_snapshot(
    root: Path,
    task: str,
    native_context: dict,
    *,
    native_read,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    if (
        not root.is_absolute()
        or root.resolve() != root
        or not root.is_dir()
        or not isinstance(task, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", task)
        or not isinstance(native_context, dict)
    ):
        raise CiContractError("archive_reader_identity_invalid")
    from . import delivery
    from .profile import ProjectProfile

    try:
        with revision_contracts.source_read_budget():
            observation = delivery.observe_script_delivery_graph(
                root,
                task,
                native_context,
                native_read=native_read,
            )
            profile = observation.profile
            identity = observation.identity
            task_detail = observation.task_detail
            graph = observation.graph
            if delivery.prepare_ci_protocol(observation) != "typed-v1":
                raise CiContractError("archive_protocol_mismatch")
            target_required = delivery.pr_base_branch_required(observation)
            context_schema = delivery.delivery_context_schema(observation)
            if target_required and context_schema != "workflow-delivery-context-v2":
                raise CiContractError("archive_protocol_mismatch")

            node_rows = graph.get("graph", {}).get("nodes", [])
            current_nodes = [
                node for node in node_rows
                if isinstance(node, dict) and node.get("id") == identity["node_id"]
            ]
            if len(current_nodes) != 1:
                raise CiContractError("native_script_identity_invalid")
            current_node = current_nodes[0]
            script_keys = {
                "ci_prepare": "prepare_ci",
                "ci_watch": "wait_ci",
            }
            node_key = current_node.get("key")
            script_key = script_keys.get(node_key)
            if current_node.get("kind") != "script" or script_key is None:
                raise CiContractError("archive_script_route_invalid")

            summary = task_detail.get("summary")
            target = task_detail.get("execution_target")
            commit_oid = target.get("commit_oid") if isinstance(target, dict) else None
            if (
                not isinstance(summary, dict)
                or summary.get("workflow_id") != graph.get("workflow_id")
                or not isinstance(commit_oid, str)
                or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit_oid)
            ):
                raise CiContractError("archive_execution_target_invalid")

            project_to_kit = _archive_project_reader_bindings(
                profile,
                script_key=script_key,
                script_path=current_node.get("script_path"),
            )
            project_root = root.expanduser().resolve(strict=True)
            kit_root = Path(__file__).resolve(strict=True).parents[1]
            if (
                not kit_root.is_absolute()
                or Path(__file__).resolve(strict=True)
                != kit_root / "workflowkit" / "ci_contract.py"
            ):
                raise CiContractError("serving_kit_identity_invalid")
            project_git_root = Path(
                revision_contracts.run_git(
                    project_root,
                    "rev-parse",
                    "--show-toplevel",
                ).stdout.strip()
            ).resolve()
            if project_git_root != project_root:
                raise CiContractError("archive_project_root_invalid")
            kit_git_root = Path(
                revision_contracts.run_git(
                    kit_root,
                    "rev-parse",
                    "--show-toplevel",
                ).stdout.strip()
            ).resolve()
            if kit_git_root != kit_root:
                raise CiContractError("serving_kit_root_invalid")
            kit_commit = revision_contracts.run_git(
                kit_root,
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
            ).stdout.strip()
            if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", kit_commit):
                raise CiContractError("serving_kit_revision_invalid")

            profile_path = ".kent/workflow-profile.toml"
            project_paths = {profile_path, *project_to_kit}
            kit_paths = set(_ARCHIVE_KIT_MODULE_PATHS)
            kit_paths.update(_ARCHIVE_KIT_TEMPLATE_PATHS)
            kit_paths.update(
                canonical
                for canonical in project_to_kit.values()
                if canonical
            )
            if len(project_paths) + len(kit_paths) > MAX_ARCHIVE_READER_PATHS:
                raise CiContractError("archive_reader_limit")

            snapshots: dict[str, bytes] = {}
            records: list[dict[str, Any]] = []
            total_bytes = 0

            def qualify_source(source_root: Path, source_commit: str, path: str, kind: str):
                nonlocal total_bytes
                mode, actual = _read_archive_source(source_root, path)
                expected_mode = revision_contracts.tree_mode(
                    source_root,
                    source_commit,
                    path,
                )
                if expected_mode not in {"100644", "100755"} or mode != expected_mode:
                    raise CiContractError("archive_reader_mode_mismatch")
                expected = revision_contracts.read_blob_bytes(
                    source_root,
                    source_commit,
                    path,
                    label="CI archive reader",
                    byte_limit=MAX_ARCHIVE_READER_FILE_BYTES,
                )
                if actual != expected:
                    raise CiContractError("archive_reader_source_mismatch")
                total_bytes += len(actual)
                if total_bytes > MAX_ARCHIVE_READER_TOTAL_BYTES:
                    raise CiContractError("archive_reader_limit")
                records.append({
                    "kind": kind,
                    "path": path,
                    "mode": expected_mode,
                    "size": len(actual),
                    "sha256": _sha256(actual),
                })
                snapshots[f"{kind}:{path}"] = actual

            for path in sorted(project_paths):
                _archive_relative_path(path, label="archive_reader_path")
                qualify_source(project_root, commit_oid, path, "project")
            for path in sorted(kit_paths):
                _archive_relative_path(path, label="archive_reader_path")
                qualify_source(kit_root, kit_commit, path, "kit")

            project_sources = {
                path: snapshots[f"project:{path}"] for path in project_to_kit
            }
            for project_path, kit_path in project_to_kit.items():
                if kit_path and project_sources[project_path] != snapshots.get(
                    f"kit:{kit_path}"
                ):
                    raise CiContractError("archive_reader_family_mismatch")

            raw_profile = project_sources[profile_path]
            try:
                selected_profile = ProjectProfile.from_toml(
                    project_root,
                    raw_profile.decode("utf-8"),
                    source=f"{commit_oid}:{profile_path}",
                    check_files=False,
                )
            except (UnicodeError, ValueError) as error:
                raise CiContractError("archive_profile_invalid") from error
            profile_projection_keys = (
                "schema_version",
                "kit_managed_commands",
                "command_versions",
                "ci_archive_reader_sources",
                "commands",
                "procedures",
                "capabilities",
                "policies",
            )
            if any(
                getattr(profile, key) != getattr(selected_profile, key)
                for key in profile_projection_keys
            ):
                raise CiContractError("archive_profile_changed")
            graph_projection = _archive_protocol_projection(graph, delivery)
            proof_inputs = {
                "schema": SCRIPT_CI_ARCHIVE_READER_PROOF_SCHEMA,
                "native_script_identity": identity,
                "project_root": str(project_root),
                "project_commit_oid": commit_oid,
                "kit_root": str(kit_root),
                "kit_commit_oid": kit_commit,
                "protocol": "typed-v1",
                "target_branch_required": target_required,
                "delivery_context_schema": context_schema,
                "graph_projection": graph_projection,
                "sources": sorted(records, key=lambda row: (
                    row["kind"], row["path"],
                )),
            }
            proof_digest = _sha256(canonical_bytes(proof_inputs))
            proof = {
                "schema": SCRIPT_CI_ARCHIVE_READER_PROOF_SCHEMA,
                "reader_proof_sha256": proof_digest,
                "native_script_identity": identity,
            }
            return proof, snapshots
    except CiContractError:
        raise
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        revision_contracts.RevisionPreflightError,
    ) as error:
        raise CiContractError("archive_reader_proof_failed") from error


def resolve_script_ci_archive_readers(
    root: Path,
    task: str,
    native_context: dict,
    *,
    native_read,
) -> dict[str, Any]:
    """Prove current CI archive readers against the native C and serving K."""
    proof, _ = _archive_reader_snapshot(
        root,
        task,
        native_context,
        native_read=native_read,
    )
    return proof


def _load_qualified_archive_module(name: str, path: Path, raw: bytes):
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    try:
        exec(compile(raw, str(path), "exec", dont_inherit=True), module.__dict__)
    except (SyntaxError, TypeError, ValueError) as error:
        raise CiContractError("archive_reader_module_invalid") from error
    return module


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parse_preparation_json(raw: bytes | str) -> Any:
    """Bounded duplicate-free JSON; stdin need not be canonical."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > MAX_PREPARATION_INPUT_BYTES:
        raise CiContractError("input_limit")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise CiContractError("input_invalid")
            result[key] = value
        return result

    def constant(_):
        raise CiContractError("input_invalid")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        canonical_bytes(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as error:
        raise CiContractError("input_invalid") from error


def read_preparation_input(stream) -> dict[str, Any]:
    deadline = time.monotonic() + 2.0
    chunks = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
            raise CiContractError("input_timeout")
        chunk = os.read(stream.fileno(), min(65536, MAX_PREPARATION_INPUT_BYTES + 1 - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > MAX_PREPARATION_INPUT_BYTES:
            raise CiContractError("input_limit")
    value = parse_preparation_json(bytes(chunks))
    if not isinstance(value, dict):
        raise CiContractError("input_invalid")
    return value


def validate_preparation_identity(payload: Mapping[str, Any]) -> dict[str, str]:
    identity = {}
    for key in ("workspace_path", "pr_url", "branch_name", "merge_strategy", "task_short_id"):
        value = payload.get(key)
        if not isinstance(value, str) or not value or value != value.strip():
            raise CiContractError("input_identity_invalid")
        if len(value) > 4096 or any(ord(char) < 32 for char in value):
            raise CiContractError("input_identity_invalid")
        identity[key] = value
    _repository_from_url(identity["pr_url"])
    if (
        not Path(identity["workspace_path"]).is_absolute()
        or identity["merge_strategy"] not in {"merge", "squash", "rebase"}
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", identity["task_short_id"])
        or identity["branch_name"].startswith("-")
    ):
        raise CiContractError("input_identity_invalid")
    return identity


def unwrap_delivery_context_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Unwrap an explicitly supplied lifecycle context at the CI boundary."""

    try:
        fields = delivery_context_fields(payload)
    except RuntimeContractError as error:
        raise CiContractError("delivery_context_invalid") from error
    result = dict(payload)
    if not fields:
        return result
    try:
        context = validate_delivery_context(
            parse_preparation_json(fields["delivery_context"])
        )
    except (CiContractError, RuntimeContractError, TypeError, ValueError) as error:
        raise CiContractError("delivery_context_invalid") from error

    flat_fields = (
        "pr_url",
        "branch_name",
        "pr_base_branch",
        "merge_strategy",
        "pr_feedback_cursor",
        "ci_contract",
        "ci_report",
    )
    for key in flat_fields:
        if key not in payload:
            continue
        if key in context:
            if payload[key] != context[key]:
                raise CiContractError("delivery_context_conflict")
        elif payload[key] != "":
            raise CiContractError("delivery_context_conflict")

    result.pop("delivery_context", None)
    for key in flat_fields:
        result.pop(key, None)
        if key in context:
            result[key] = context[key]
    return result


def preparation_failure(payload: Mapping[str, Any]) -> dict[str, str]:
    """Diagnosis transport only: empty strings denote absent packets, not reports."""
    payload = unwrap_delivery_context_payload(payload)
    identity = validate_preparation_identity(payload)
    values = {}
    for key in ("ci_contract", "ci_report"):
        value = payload.get(key, "")
        if not isinstance(value, str):
            raise CiContractError("input_invalid")
        values[key] = value
    cursor = payload.get("pr_feedback_cursor", "uninitialized")
    if not isinstance(cursor, str):
        raise CiContractError("input_invalid")
    result = {
        "transition": "ci_prepare_failed", **identity, **values,
        "pr_feedback_cursor": cursor,
    }
    if "pr_base_branch" in payload:
        target = payload["pr_base_branch"]
        if (
            not isinstance(target, str)
            or not target
            or target != target.strip()
            or target.startswith("-")
        ):
            raise CiContractError("target_branch_invalid")
        result["pr_base_branch"] = target
    return result


CI_PREPARATION_DIAGNOSTIC_SCHEMA = "workflow-ci-preparation-diagnostic-v1"
CI_PREPARATION_DIAGNOSTIC_CODES = {
    "archive_blocked",
    "delivery_context_conflict",
    "delivery_context_invalid",
    "github_query_failed",
    "preparation_failed",
    "previous_ci_packet_invalid",
    "report_identity_invalid",
    "target_branch_changed",
}
CI_PREPARATION_CLASSIFICATIONS = {
    "archive_blocked",
    "identity_conflict",
    "invalid_input",
    "transient",
}


def _validated_resume_context(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        fields = delivery_context_fields({"delivery_context": raw})
        context = validate_delivery_context(parse_preparation_json(fields["delivery_context"]))
        if context["phase"] != "post_pr":
            return None
        repository = _repository_from_url(context["pr_url"])
        pull_number = _dynamic_pull_number(context["pr_url"])
        contract = None
        if "ci_contract" in context:
            contract = validate_ci_contract(
                runtime_contracts.parse_canonical_json(
                    context["ci_contract"],
                    label="resume CI contract",
                    max_bytes=MAX_CI_REPORT_BYTES,
                )
            )
            if (
                contract["repository"] != repository
                or contract["pull_number"] != pull_number
            ):
                return None
        if "ci_report" in context:
            report = validate_dynamic_ci_report(
                runtime_contracts.parse_canonical_json(
                    context["ci_report"],
                    label="resume CI report",
                    max_bytes=runtime_contracts.MAX_DYNAMIC_CI_REPORT_BYTES,
                )
            )
            if (
                report["contract"]["repository"] != repository
                or report["contract"]["pull_number"] != pull_number
                or contract is None
                or report["contract"] != contract
            ):
                return None
        return raw
    except (CiContractError, RuntimeContractError, TypeError, ValueError):
        return None


def validate_ci_preparation_diagnostic(value: object) -> dict[str, Any]:
    """Validate the closed typed preparation-diagnostic carrier."""
    if isinstance(value, str):
        value = runtime_contracts.parse_canonical_json(
            value,
            label="CI preparation diagnostic",
            max_bytes=MAX_CI_REPORT_BYTES,
        )
    if not isinstance(value, Mapping):
        raise CiContractError("preparation_diagnostic_invalid")
    allowed = {"schema", "code", "classification", "input_strings_sha256", "resume_context"}
    if set(value) - allowed or set(value) - {"resume_context"} != {
        "schema", "code", "classification", "input_strings_sha256",
    }:
        raise CiContractError("preparation_diagnostic_invalid")
    if value.get("schema") != CI_PREPARATION_DIAGNOSTIC_SCHEMA:
        raise CiContractError("preparation_diagnostic_invalid")
    code = value.get("code")
    classification = value.get("classification")
    expected_classification = {
        "archive_blocked": "archive_blocked",
        "delivery_context_conflict": "identity_conflict",
        "delivery_context_invalid": "invalid_input",
        "github_query_failed": "transient",
        "preparation_failed": "transient",
        "previous_ci_packet_invalid": "invalid_input",
        "report_identity_invalid": "identity_conflict",
        "target_branch_changed": "identity_conflict",
    }
    if (
        code not in CI_PREPARATION_DIAGNOSTIC_CODES
        or classification not in CI_PREPARATION_CLASSIFICATIONS
        or expected_classification.get(code) != classification
    ):
        raise CiContractError("preparation_diagnostic_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("input_strings_sha256", ""))):
        raise CiContractError("preparation_diagnostic_invalid")
    result = {
        "schema": CI_PREPARATION_DIAGNOSTIC_SCHEMA,
        "code": code,
        "classification": classification,
        "input_strings_sha256": value["input_strings_sha256"],
    }
    if "resume_context" in value:
        context = value["resume_context"]
        if _validated_resume_context(context) != context:
            raise CiContractError("preparation_diagnostic_invalid")
        result["resume_context"] = context
    return result


def typed_preparation_failure(
    original_payload: Mapping[str, Any],
    error: BaseException,
) -> dict[str, str]:
    """Build a diagnosis without inventing a CI packet or accepting bad identity."""
    workspace = original_payload.get("workspace_path")
    task = original_payload.get("task_short_id")
    if (
        not isinstance(workspace, str)
        or not Path(workspace).is_absolute()
        or str(Path(workspace).resolve()) != workspace
        or Path.cwd().resolve() != Path(workspace)
        or not isinstance(task, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", task)
    ):
        raise CiContractError("input_identity_invalid")

    error_text = str(getattr(error, "code", "") or str(error)).lower()
    if isinstance(error, BoundedCommandError) or "github" in error_text or "query" in error_text:
        code, classification = "github_query_failed", "transient"
    elif "archive" in error_text:
        code, classification = "archive_blocked", "archive_blocked"
    elif "target_branch_changed" in error_text:
        code, classification = "target_branch_changed", "identity_conflict"
    elif any(word in error_text for word in ("identity", "conflict")):
        code = (
            "delivery_context_conflict"
            if "delivery_context" in error_text
            else "report_identity_invalid"
        )
        classification = "identity_conflict"
    elif any(word in error_text for word in ("invalid", "packet", "contract", "report")):
        code = (
            "delivery_context_invalid"
            if "delivery_context" in error_text or "target_branch" in error_text
            else "previous_ci_packet_invalid"
        )
        classification = "invalid_input"
    else:
        code, classification = "preparation_failed", "transient"

    input_strings = {
        key: value
        for key, value in original_payload.items()
        if isinstance(key, str) and isinstance(value, str)
    }
    diagnostic: dict[str, Any] = {
        "schema": CI_PREPARATION_DIAGNOSTIC_SCHEMA,
        "code": code,
        "classification": classification,
        "input_strings_sha256": _sha256(canonical_bytes(input_strings)),
    }
    if "delivery_context" in original_payload:
        try:
            unwrap_delivery_context_payload(original_payload)
        except (CiContractError, RuntimeContractError, TypeError, ValueError):
            resume = None
        else:
            resume = _validated_resume_context(original_payload["delivery_context"])
        if resume is not None:
            diagnostic["resume_context"] = resume
    diagnostic = validate_ci_preparation_diagnostic(diagnostic)
    return {
        "transition": "ci_prepare_failed",
        "workspace_path": workspace,
        "task_short_id": task,
        "ci_preparation_diagnostic": canonical_bytes(diagnostic).decode("utf-8"),
    }


def _terminate(
    process: subprocess.Popen[bytes],
    process_group_id: int,
) -> None:
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except OSError:
        pass
    try:
        process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        pass
    # The leader may exit on TERM while a descendant ignores it and retains
    # our pipe. Cleanup owns the entire newly-created session, not just Popen.
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_bounded_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    stdin: bytes = b"",
    timeout: float = PREPARE_TIMEOUT_SECONDS,
    output_limit: int = MAX_GITHUB_OUTPUT_BYTES,
    env: Mapping[str, str] | None = None,
    provider_guard: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    if provider_guard is not None:
        provider_guard()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    process_group_id = process.pid
    selector = selectors.DefaultSelector()
    os.set_blocking(process.stdin.fileno(), False)
    if stdin:
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    else:
        process.stdin.close()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    input_offset = 0
    deadline = time.monotonic() + timeout
    failure: str | None = None
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "github_query_failed"
                break
            events = selector.select(remaining)
            if not events:
                failure = "github_query_failed"
                break
            for key, _ in events:
                if key.data == "stdin":
                    try:
                        written = os.write(
                            key.fileobj.fileno(),
                            stdin[input_offset:],
                        )
                    except (BrokenPipeError, OSError) as error:
                        failure = "github_query_failed"
                        break
                    input_offset += written
                    if input_offset == len(stdin):
                        selector.unregister(key.fileobj)
                        process.stdin.close()
                    continue
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                buffer = output[key.data]
                if len(buffer) + len(data) > output_limit:
                    failure = "hard_limit"
                    break
                buffer.extend(data)
            if failure:
                break
    except BaseException:
        _terminate(process, process_group_id)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
        raise
    finally:
        selector.close()
    if failure:
        _terminate(process, process_group_id)
        try:
            process.stdin.close()
        except OSError:
            pass
        process.stdout.close()
        process.stderr.close()
        raise BoundedCommandError(
            failure,
            stdout=bytes(output["stdout"]),
            stderr=bytes(output["stderr"]),
        )
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _terminate(process, process_group_id)
        try:
            process.stdin.close()
        except OSError:
            pass
        process.stdout.close()
        process.stderr.close()
        raise BoundedCommandError(
            "github_query_failed",
            stdout=bytes(output["stdout"]),
            stderr=bytes(output["stderr"]),
        )
    stdout = bytes(output["stdout"])
    stderr = bytes(output["stderr"])
    process.stdout.close()
    process.stderr.close()
    if owned_process_group_exists(process_group_id):
        _terminate(process, process_group_id)
        raise BoundedCommandError(
            "github_query_failed", stdout=stdout, stderr=stderr,
        )
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout,
        stderr,
    )


def _github_bin() -> str:
    configured = os.environ.get("KENT_GH_BIN")
    candidates = (configured,) if configured else ("gh",)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
        if candidate:
            from shutil import which

            resolved = which(candidate)
            if resolved:
                return resolved
    raise CiContractError("GitHub CLI was not found")


def github_provider_read_guard(
    root: Path,
    payload: Mapping[str, Any],
    identity: Mapping[str, Any],
) -> Callable[[], None]:
    """Bind each provider read to verified incoming producer ancestry."""
    from . import delivery as delivery_contracts

    commentary = payload.get("commentary")
    references = []
    if commentary is not None:
        if not isinstance(commentary, str):
            raise CiContractError("delivery_context_invalid")
        locator = delivery_contracts.producer_locator(commentary)
        if locator is not None:
            references.append(locator)
    require_read = getattr(
        delivery_contracts,
        "require_github_provider_read",
        None,
    )
    if not callable(require_read):
        raise CiContractError("github_provider_guard_unavailable")

    def require() -> None:
        require_read(root, references, dict(identity))

    return require


def read_pull_request(
    workspace: Path,
    pr_url: str,
    *,
    gh_bin: str | None = None,
    provider_guard: Callable[[], None] | None = None,
) -> dict[str, Any]:
    child_env = None
    if provider_guard is not None:
        child_env = os.environ.copy()
        child_env["GH_DEBUG"] = "api"
    command = [
        gh_bin or _github_bin(),
        "pr",
        "view",
        pr_url,
        "--json",
        "state,headRefOid,baseRefOid,baseRefName,headRefName,url,mergedAt,mergeCommit",
    ]
    try:
        result = run_bounded_command(
            command,
            cwd=workspace,
            env=child_env,
            provider_guard=provider_guard,
        )
    except BoundedCommandError as error:
        if provider_guard is None:
            raise
        observed_at_ns = time.time_ns()
        retry_evidence, retry_after = provider_observation._provider_retry_metadata(
            error.stderr,
            now_unix_ns=observed_at_ns,
        )
        retry_unknown = (
            retry_evidence is None
            or error.code == "hard_limit"
        )
        raise ObservationError(
            "github_query_failed",
            exit_code=error.exit_code,
            stdout=error.stdout,
            stderr=error.stderr,
            rate_limited=retry_evidence is True and not retry_unknown,
            retry_after_seconds=retry_after,
            provider_retry_unknown=retry_unknown,
            provider_retry_captured=True,
            provider_retry_observed_at_ns=observed_at_ns,
        ) from error
    if result.returncode != 0:
        if provider_guard is None:
            raise BoundedCommandError(
                "github_query_failed",
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        observed_at_ns = time.time_ns()
        retry_evidence, retry_after = provider_observation._provider_retry_metadata(
            result.stderr,
            now_unix_ns=observed_at_ns,
        )
        retry_unknown = retry_evidence is None
        raise ObservationError(
            "github_query_failed",
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            rate_limited=retry_evidence is True and not retry_unknown,
            retry_after_seconds=retry_after,
            provider_retry_unknown=retry_unknown,
            provider_retry_captured=True,
            provider_retry_observed_at_ns=observed_at_ns,
        )
    try:
        value = parse_preparation_json(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        if provider_guard is not None:
            observed_at_ns = time.time_ns()
            raise ObservationError(
                "github_query_failed",
                stdout=result.stdout,
                stderr=result.stderr,
                provider_retry_unknown=True,
                provider_retry_captured=True,
                provider_retry_observed_at_ns=observed_at_ns,
            ) from error
        raise CiContractError("GitHub PR metadata is not valid JSON") from error
    if not isinstance(value, dict):
        if provider_guard is not None:
            observed_at_ns = time.time_ns()
            raise ObservationError(
                "github_query_failed",
                stdout=result.stdout,
                stderr=result.stderr,
                provider_retry_unknown=True,
                provider_retry_captured=True,
                provider_retry_observed_at_ns=observed_at_ns,
            )
        raise CiContractError("GitHub PR metadata must be an object")
    return value


def _repository_from_url(pr_url: str) -> str:
    parsed = urlparse(pr_url)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query or parsed.fragment or parsed.params
        or len(parts) != 4
        or parts[2] != "pull"
        or not parts[3].isdigit()
        or int(parts[3]) <= 0
        or parsed.path != "/" + "/".join(parts)
        or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts[:2])
    ):
        raise CiContractError("pr_url must identify one GitHub pull request")
    return f"{parts[0]}/{parts[1]}"


def _validate_pr_metadata(pr: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    if (
        pr.get("url") != payload["pr_url"]
        or pr.get("headRefName") != payload["branch_name"]
        or pr.get("state") not in {"OPEN", "MERGED"}
        or not isinstance(pr.get("baseRefName"), str)
        or not pr["baseRefName"]
    ):
        raise CiContractError("pull_request_identity_invalid")
    for key in ("headRefOid", "baseRefOid"):
        if not isinstance(pr.get(key), str) or not re.fullmatch(r"[0-9a-f]{40}", pr[key]):
            raise CiContractError("pull_request_identity_invalid")
    if pr["state"] == "MERGED":
        proof = pr.get("mergeCommit")
        if (
            not isinstance(pr.get("mergedAt"), str)
            or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", pr["mergedAt"])
            or not isinstance(proof, dict)
            or not isinstance(proof.get("oid"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", proof["oid"])
        ):
            raise CiContractError("merged_proof_missing")
    elif pr.get("mergedAt") or pr.get("mergeCommit"):
        raise CiContractError("pull_request_identity_invalid")


def archive_ci_report(root: Path, task: str, report: Mapping[str, Any]) -> str:
    """Retain a validated report in the existing locked task evidence area."""
    import fcntl

    normalized = validate_dynamic_ci_report(report)
    raw = canonical_bytes(normalized)
    if len(raw) > MAX_CI_REPORT_BYTES:
        raise CiContractError("report_archive_limit")
    template = Path(__file__).resolve().parents[1] / "templates/project/workflow-evidence-ledger"
    loader = importlib.machinery.SourceFileLoader("_ci_evidence_ledger", str(template))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    ledger = importlib.util.module_from_spec(spec)
    loader.exec_module(ledger)
    path = ledger.ledger_path(root, task)
    ledger.require_ignored(root, path)
    name = f"ci-report-{_sha256(raw)}.json"
    artifact = str(path.parent.relative_to(root) / name)
    descriptors, _, _, _, task_fd = ledger._open_runtime_fds(
        root, task, create_task=True, lock_operation=fcntl.LOCK_EX | fcntl.LOCK_NB,
    )
    try:
        # A bounded artifact set and bounded ledger read are not a new journal.
        names = []
        with os.scandir(task_fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > 256:
                    raise CiContractError("report_archive_limit")
        if len([item for item in names if item.startswith("ci-report-")]) >= 64 and name not in names:
            raise CiContractError("report_archive_limit")
        entries = []
        if set(names) & {"evidence-ledger.jsonl", *runtime_contracts.EVIDENCE_RECOVERY_FILES}:
            entries = ledger.load_evidence_view(
                task_fd, task=task, runtime=runtime_contracts,
            )["records"]
            if entries and entries[-1].get("record_kind") == runtime_contracts.TERMINAL_SEAL_SCHEMA:
                raise CiContractError("report_archive_sealed")
        try:
            descriptor = ledger._open_runtime_file(task_fd, name, create=False)
        except FileNotFoundError:
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=task_fd,
            )
            try:
                with os.fdopen(os.dup(descriptor), "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(task_fd)
            finally:
                os.close(descriptor)
        else:
            try:
                if os.fstat(descriptor).st_size != len(raw) or os.read(descriptor, len(raw) + 1) != raw:
                    raise CiContractError("report_archive_invalid")
            finally:
                os.close(descriptor)
        if any(artifact in entry.get("artifacts", []) for entry in entries):
            return artifact
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    manifest_path = next(
        (
            relative
            for relative in (".kent/context/delivery.md", ".kent/project-contract.md")
            if (root / relative).is_file()
        ),
        None,
    )
    if manifest_path is None:
        raise CiContractError("report_archive_context_invalid")
    try:
        _, duplicate_suppressed = ledger.append_entry(
            root, path, task=task, _runtime=runtime_contracts,
            _payload={
                "node_key": "ci_prepare", "evidence_type": "ci_report",
                "summary": "Validated CI cycle report retained before preparation re-entry.",
                "artifacts": [artifact], "checks": ["github-ci-report-v3 validated"],
                "context": {
                    "manifest_path": manifest_path,
                    "files_read": [], "model_calls": 0, "compaction_count": 0,
                },
            },
        )
    except (OSError, ValueError) as error:
        raise CiContractError("report_archive_failed") from error
    if duplicate_suppressed:
        # The ledger owns run-level idempotency; never claim a different report
        # is archived merely because another event used the same run identity.
        raise CiContractError("report_archive_conflict")
    return artifact


def archive_script_ci_report(
    payload: Mapping[str, Any],
    *,
    evidence_command: str,
) -> str:
    """Qualify source readers, then archive through the in-process ledger API."""
    identity = validate_preparation_identity(payload)
    root = Path(identity["workspace_path"])
    if (
        root.resolve() != root
        or not root.is_dir()
        or Path.cwd().resolve() != root
    ):
        raise CiContractError("archive_blocked")
    task = identity["task_short_id"]
    try:
        raw_report = payload["ci_report"]
        if not isinstance(raw_report, str):
            raise ValueError("report is not text")
        report = validate_dynamic_ci_report(parse_preparation_json(raw_report))
        repository = _repository_from_url(identity["pr_url"])
        pull_number = _dynamic_pull_number(identity["pr_url"])
        if (
            report["contract"]["repository"] != repository
            or report["contract"]["pull_number"] != pull_number
        ):
            raise ValueError("report identity does not match PR")
        native_context = payload["_kent"]
        if not isinstance(native_context, dict):
            raise ValueError("native Script context is unavailable")
        if evidence_command != ".kent/scripts/workflow-evidence-ledger":
            raise ValueError("profile evidence command is not the fixed ledger helper")
        native_read = _native_read_adapter(root)
        initial_proof, source_snapshots = _archive_reader_snapshot(
            root,
            task,
            native_context,
            native_read=native_read,
        )
        ledger_relative = evidence_command
        runtime_relative = ".kent/scripts/workflow_runtime_contracts.py"
        ledger_source = source_snapshots.get(f"project:{ledger_relative}")
        runtime_source = source_snapshots.get(f"project:{runtime_relative}")
        if ledger_source is None or runtime_source is None:
            raise ValueError("qualified ledger/runtime sources are unavailable")
        helper = root / ledger_relative
        runtime_helper = root / runtime_relative
        runtime_module = _load_qualified_archive_module(
            "kent_qualified_workflow_runtime_contracts",
            runtime_helper,
            runtime_source,
        )
        ledger_module = _load_qualified_archive_module(
            "kent_qualified_workflow_evidence_ledger",
            helper,
            ledger_source,
        )
        ledger_module._runtime_module = lambda: runtime_module
        request = {
            "schema": "script-ci-archive-request-v1",
            "workspace_path": str(root),
            "task_short_id": task,
            "ci_report": report,
            "native_context": native_context,
        }
        def bound_reader_resolver(callback_root, callback_task, callback_context):
            if (
                Path(callback_root) != root
                or callback_task != task
                or callback_context != native_context
            ):
                raise CiContractError("archive_reader_identity_changed")
            return resolve_script_ci_archive_readers(
                root,
                task,
                native_context,
                native_read=native_read,
            )

        result = ledger_module.archive_script_ci_report(
            root,
            task=task,
            _payload=request,
            _reader_resolver=bound_reader_resolver,
            _initial_reader_proof=initial_proof,
        )
        if not isinstance(result, dict) or set(result) != {"ci_report_artifact"}:
            raise ValueError("native Script report archive result is invalid")
        artifact = result["ci_report_artifact"]
        expected = (
            f".kent/runtime/{task}/ci-report-"
            f"{_sha256(canonical_bytes(report))}.json"
        )
        if artifact != expected:
            raise ValueError("native Script report artifact locator is invalid")
        return artifact
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        raise CiContractError("archive_blocked") from error


def _ruby_normalizer() -> str:
    return r"""
require "json"
require "yaml"

class StrictHash < Hash
  def []=(key, value)
    raise "json_duplicate_key" if key?(key)
    super
  end
end

def reject(node, depth = 0)
  raise "yaml_depth" if depth > 64
  return unless node
  if node.is_a?(Psych::Nodes::Alias)
    raise "yaml_alias"
  end
  if node.is_a?(Psych::Nodes::Mapping)
    keys = []
    node.children.each_slice(2) do |key, value|
      if key.is_a?(Psych::Nodes::Scalar)
        raise "yaml_duplicate_key" if keys.include?(key.value)
        keys << key.value
      end
      raise "yaml_key" unless key.is_a?(Psych::Nodes::Scalar)
      reject(key, depth + 1)
      reject(value, depth + 1)
    end
  elsif node.respond_to?(:children)
    children = node.children
    children.each { |child| reject(child, depth + 1) } if children.respond_to?(:each)
  end
end

def string_value(value, fallback = "")
  return fallback if value.nil?
  return value if value.is_a?(String)
  raise "string_required"
end

def string_list(value)
  return [] if value.nil?
  value = [value] unless value.is_a?(Array)
  raise "string_list" unless value.all? { |item| item.is_a?(String) }
  raise "duplicate_value" unless value.uniq == value
  value.sort
end

def mapping(value)
  return {} if value.nil?
  raise "mapping_required" unless value.is_a?(Hash)
  raise "mapping_key" unless value.keys.all? { |key| key.is_a?(String) }
  value
end

def boolean(value, fallback = false)
  return fallback if value.nil?
  raise "boolean_required" unless value == true || value == false
  value
end

def condition(value)
  return value.to_s if value == true || value == false
  string_value(value)
end

def permissions(value)
  # Broad shorthand cannot be represented without binding a GitHub permission
  # catalog; fail closed instead of pretending it is an empty/read-only map.
  mapping(value)
end

def defaults(value)
  run = mapping(mapping(value)["run"])
  raise "defaults_key" unless (run.keys - ["shell", "working-directory"]).empty?
  run
end

def service(value)
  value = {"image" => value} if value.is_a?(String)
  value = mapping(value)
  raise "service_key" unless (value.keys - ["image", "env", "ports", "options"]).empty?
  {
    "image" => string_value(value["image"]),
    "environment" => scalar_map(value["env"]),
    "ports" => string_list(value["ports"]),
    "options" => string_value(value["options"])
  }
end

def event_record(name, config)
  config = mapping(config)
  raise "event_key" unless (config.keys - [
    "branches", "branches-ignore", "tags", "tags-ignore", "paths",
    "paths-ignore", "types", "inputs"
  ]).empty?
  inputs = mapping(config["inputs"]).sort.map do |input_name, raw|
    input = mapping(raw)
    raise "input_key" unless (input.keys - [
      "type", "required", "default", "description", "options"
    ]).empty?
    {
      "name" => input_name, "type" => string_value(input["type"], "string"),
      "required" => boolean(input["required"]),
      "default_present" => input.key?("default")
    }.merge(input.key?("default") ? {"default" => input["default"]} : {})
  end
  {
    "name" => name.to_s,
    "branches" => string_list(config["branches"]),
    "branches_ignore" => string_list(config["branches-ignore"]),
    "tags" => string_list(config["tags"]),
    "tags_ignore" => string_list(config["tags-ignore"]),
    "paths" => string_list(config["paths"]),
    "paths_ignore" => string_list(config["paths-ignore"]),
    "types" => string_list(config["types"]),
    "dispatch_inputs" => inputs
  }
end

def scalar_map(value)
  value = mapping(value)
  raise "scalar_map" unless value.values.all? do |v|
    v.is_a?(String) || v.is_a?(Integer) || v == true || v == false
  end
  value
end

def secret_refs(value)
  if value.is_a?(String)
    value.scan(
      /(?:\$\{\{\s*)?secrets\.([A-Za-z_][A-Za-z0-9_-]*)(?:\s*\}\})?/
    ).flatten
  elsif value.is_a?(Hash)
    value.flat_map { |key, item| secret_refs(key) + secret_refs(item) }
  elsif value.is_a?(Array)
    value.flat_map { |item| secret_refs(item) }
  else
    []
  end.uniq.sort
end

raw = STDIN.read
value =
  if ARGV.fetch(0) == "json"
    JSON.parse(raw, object_class: StrictHash)
  else
    stream = Psych.parse_stream(raw)
    raise "yaml_documents" unless stream.children.length == 1
    stream.children.each { |document| reject(document.root) }
    Psych.safe_load(raw, aliases: false)
  end
raise "workflow_object" unless value.is_a?(Hash)
jobs = value.fetch("jobs")
raise "jobs_object" unless jobs.is_a?(Hash)
rows = []
workflow_permissions = permissions(value["permissions"])
workflow_environment = scalar_map(value["env"])
workflow_defaults = defaults(value["defaults"])
raise "ambiguous_on" if value.key?("on") && value.key?(true)
trigger_value = value.key?("on") ? value["on"] : value[true]
workflow_events =
  if trigger_value.is_a?(String)
    [event_record(trigger_value, {})]
  elsif trigger_value.is_a?(Array)
    trigger_value.map { |item| event_record(item, {}) }
  elsif trigger_value.is_a?(Hash)
    trigger_value.map { |event, config| event_record(event, config) }
  else
    raise "workflow_events"
  end
selected = JSON.parse(ARGV.fetch(2))
if selected
  raise "missing_job" unless (selected - jobs.keys).empty?
  jobs = jobs.select { |key, _| selected.include?(key) }
end
jobs.each do |key, job|
  raise "job_key" unless key.is_a?(String) && key.match?(/\A[A-Za-z0-9_.-]+\z/)
  raise "job_object" unless job.is_a?(Hash)
  name = job["name"] || key
  raise "job_name" unless name.is_a?(String) && !name.strip.empty?
  raise "job_expression" if name.include?("${{") || key.include?("${{")
  strategy = mapping(job["strategy"])
  matrix = strategy["matrix"]
  raise "unsupported_matrix" unless matrix.nil? || matrix == {}
  needs = string_list(job["needs"])
  if selected && !job.key?("permissions") && !value.key?("permissions")
    # Repository default token permissions are not immutable source policy.
    raise "unknown_default_permissions"
  end
  effective_permissions = job.key?("permissions") ?
    permissions(job["permissions"]) : workflow_permissions
  job_environment = scalar_map(job["env"])
  effective_environment = workflow_environment.merge(job_environment)
  job_defaults = defaults(job["defaults"])
  effective_defaults = workflow_defaults.merge(job_defaults)
  effective_defaults_run = {
    "shell" => string_value(effective_defaults["shell"]),
    "working_directory" => string_value(
      effective_defaults["working-directory"]
    )
  }
  steps = job["steps"]
  raise "job_steps" unless steps.is_a?(Array) && !steps.empty?
  normalized_steps = steps.map do |step|
    raise "step_object" unless step.is_a?(Hash)
    uses = step["uses"]
    run = step["run"]
    raise "step_kind" unless uses.is_a?(String) ^ run.is_a?(String)
    step_environment = scalar_map(step["env"])
    effective_step_environment = effective_environment.merge(step_environment)
    run_step = run.is_a?(String)
    normalized = {
      "kind" => (run_step ? "run" : "uses"),
      "name" => string_value(step["name"]),
      "condition" => condition(step["if"]),
      "continue_on_error" => boolean(step["continue-on-error"]),
      "uses" => uses.is_a?(String) ? uses : "",
      "with" => scalar_map(step["with"]),
      "run" => run_step ? run : "",
      "effective_shell" => (
        if run_step
          string_value(
            step["shell"] || effective_defaults["shell"]
          )
        else
          ""
        end
      ),
      "effective_working_directory" => (
        if run_step
          string_value(
            step["working-directory"] ||
            effective_defaults["working-directory"]
          )
        else
          ""
        end
      ),
      "effective_environment" => effective_step_environment
    }
    normalized.merge("secret_refs" => secret_refs(normalized))
  end
  checkouts = normalized_steps.select do |step|
    step["kind"] == "uses" && step["uses"].start_with?("actions/checkout@")
  end
  checkout_persist_credentials = checkouts.any? do |checkout|
    if checkout["with"].key?("persist-credentials")
      v = checkout["with"]["persist-credentials"]
      raise "checkout_credentials" unless [true, false, "true", "false"].include?(v)
      v == true || v == "true"
    else
      true
    end
  end
  runner_environment_asserted = normalized_steps.any? do |step|
    step["kind"] == "run" && step["condition"].empty? &&
      !step["continue_on_error"] &&
      ["", "bash", "sh"].include?(step["effective_shell"]) &&
      [
        'test "$RUNNER_ENVIRONMENT" = github-hosted',
        'test "${RUNNER_ENVIRONMENT:-github-hosted}" = github-hosted'
      ].include?(step["run"].strip)
  end
  environment = job["environment"]
  # An environment object is not the empty environment. The current normalized
  # contract has only a name, so reject richer objects rather than discard them.
  raise "github_environment_object" unless environment.nil? || environment.is_a?(String)
  observed_job = {
    "job_key" => key,
    "job_display_name" => name,
    "condition" => condition(job["if"]),
    "needs" => needs,
    "matrix" => (matrix || {}),
    "continue_on_error" => boolean(job["continue-on-error"]),
    "runs_on" => string_value(job["runs-on"]),
    "effective_defaults_run" => effective_defaults_run,
    "effective_permissions" => effective_permissions,
    "effective_environment" => effective_environment,
    "services" => mapping(job["services"]).transform_values { |v| service(v) },
    "container" => job.key?("container") ? service(job["container"]) : nil,
    "github_environment" => string_value(environment),
    "checkout_persist_credentials" => checkout_persist_credentials,
    "runner_environment_asserted" => runner_environment_asserted,
    "steps" => normalized_steps
  }
  rows << observed_job.merge("secret_refs" => secret_refs(observed_job))
end
name = value["name"] || File.basename(ARGV.fetch(1), File.extname(ARGV.fetch(1)))
raise "workflow_name" unless name.is_a?(String) && !name.strip.empty?
raise "workflow_name" if name.include?("${{") || (selected && !value.key?("name"))
puts JSON.generate({
  "schema" => "normalized_github_workflow_source_v1",
  "workflow_path" => ARGV.fetch(1),
  "workflow_display_name" => name,
  "workflow_name" => name,
  "jobs" => rows.sort_by { |row| row["job_key"] },
  "permissions" => workflow_permissions,
  "environment" => workflow_environment,
  "defaults_run" => {
    "shell" => string_value(workflow_defaults["shell"]),
    "working_directory" => string_value(workflow_defaults["working-directory"])
  },
  "events" => workflow_events
})
"""


def normalize_github_workflow(
    raw: bytes,
    path: str,
    *,
    selected_jobs: list[str] | None = None,
    timeout: float = PREPARE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if len(raw) > MAX_WORKFLOW_BYTES:
        raise CiContractError(f"workflow source exceeds its byte limit: {path}")
    suffix = Path(path).suffix.lower()
    kind = "json" if suffix == ".json" else "yaml"
    result = run_bounded_command(
        ["ruby", "-rjson", "-ryaml", "-e", _ruby_normalizer(), kind, path,
         json.dumps(selected_jobs)],
        stdin=raw,
        timeout=timeout,
        output_limit=MAX_RUBY_OUTPUT_BYTES,
    )
    if result.returncode != 0:
        raise CiContractError("invalid_workflow_source")
    try:
        value = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CiContractError(f"{path}: normalizer output is invalid") from error
    if not isinstance(value, dict):
        raise CiContractError(f"{path}: normalized workflow is not an object")
    return value


def qualify_ci_rerun_effect_closure(
    workflow: Mapping[str, Any],
    selected_job: str,
    *,
    effects_by_job: Mapping[str, Sequence[str]],
) -> dict[str, list[str]]:
    """Return a rerun's full dependent-job effect closure, failing closed.

    ``effects_by_job`` must come from first-party effect qualification of this
    exact normalized workflow source. Job names are never used as effect proof.
    Only jobs explicitly classified as tests or infrastructure are eligible;
    an absent classification or any other effect blocks the entire rerun.
    """
    if (
        not isinstance(workflow, Mapping)
        or workflow.get("schema") != "normalized_github_workflow_source_v1"
        or not isinstance(selected_job, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", selected_job) is None
        or not isinstance(effects_by_job, Mapping)
    ):
        raise CiContractError("rerun_source_invalid")
    rows = workflow.get("jobs")
    if (
        not isinstance(rows, list)
        or not rows
        or len(rows) > MAX_RERUN_SOURCE_JOBS
    ):
        raise CiContractError("rerun_source_invalid")

    dependencies: dict[str, tuple[str, ...]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise CiContractError("rerun_source_invalid")
        key = row.get("job_key")
        needs = row.get("needs")
        if (
            not isinstance(key, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", key) is None
            or key in dependencies
            or not isinstance(needs, list)
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", item) is None
                for item in needs
            )
            or len(needs) != len(set(needs))
            or key in needs
        ):
            raise CiContractError("rerun_dependency_invalid")
        dependencies[key] = tuple(needs)
    if selected_job not in dependencies or any(
        dependency not in dependencies
        for values in dependencies.values()
        for dependency in values
    ):
        raise CiContractError("rerun_dependency_invalid")

    dependents = {key: [] for key in dependencies}
    remaining = {}
    for key, needs in dependencies.items():
        remaining[key] = len(needs)
        for dependency in needs:
            dependents[dependency].append(key)
    ready = [key for key, count in remaining.items() if count == 0]
    visited_count = 0
    while ready:
        key = ready.pop()
        visited_count += 1
        for dependent in dependents[key]:
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                ready.append(dependent)
    if visited_count != len(dependencies):
        raise CiContractError("rerun_dependency_invalid")

    closure = {selected_job}
    pending = [selected_job]
    while pending:
        key = pending.pop()
        for dependent in dependents[key]:
            if dependent not in closure:
                closure.add(dependent)
                pending.append(dependent)

    if set(effects_by_job) != closure:
        raise CiContractError("rerun_effect_unknown")
    effects: set[str] = set()
    for key in closure:
        declared = effects_by_job[key]
        if (
            not isinstance(declared, (list, tuple, set, frozenset))
            or not declared
            or any(not isinstance(item, str) for item in declared)
            or len(declared) != len(set(declared))
        ):
            raise CiContractError("rerun_effect_unknown")
        if not set(declared) <= CI_RERUN_ALLOWED_EFFECTS:
            raise CiContractError("rerun_effect_forbidden")
        effects.update(declared)
    return {
        "jobs": sorted(closure),
        "effects": sorted(effects),
    }


def prepare_ci_payload(
    payload: Mapping[str, Any],
    *,
    gh_bin: str | None = None,
    report_archiver=None,
    provider_guard: Callable[[], None] | None = None,
) -> dict[str, Any]:
    payload = unwrap_delivery_context_payload(payload)
    validate_preparation_identity(payload)
    for key in (
        "ci_contract", "ci_report", "pr_feedback_cursor",
    ):
        if key in payload and not isinstance(payload[key], str):
            raise CiContractError("input_invalid")
    return _prepare_ci_payload(
        payload,
        gh_bin=gh_bin,
        report_archiver=report_archiver,
        provider_guard=provider_guard,
    )


def _dynamic_pull_number(pr_url: str) -> int:
    parts = [part for part in urlparse(pr_url).path.split("/") if part]
    if len(parts) != 4:
        raise CiContractError("pr_url must identify one GitHub pull request")
    try:
        number = int(parts[3])
    except ValueError as error:
        raise CiContractError("pr_url must identify one GitHub pull request") from error
    if number < 1 or number > 2147483647:
        raise CiContractError("pr_url must identify one GitHub pull request")
    return number


def _dynamic_contract(
    *,
    repository: str,
    pull_number: int,
    head_oid: str,
    base_oid: str,
) -> dict[str, Any]:
    return validate_ci_contract({
        "schema": "github-ci-contract-v1",
        "repository": repository,
        "pull_number": pull_number,
        "head_oid": head_oid,
        "base_oid": base_oid,
        "require_ci": True,
    })


def _validate_dynamic_prepare_packets(
    payload: Mapping[str, Any],
    *,
    repository: str,
    pull_number: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    contract: dict[str, Any] | None = None
    raw_contract = payload.get("ci_contract", "")
    if raw_contract:
        try:
            contract = validate_ci_contract(
                runtime_contracts.parse_canonical_json(
                    raw_contract,
                    label="previous CI contract",
                    max_bytes=MAX_CI_REPORT_BYTES,
                )
            )
        except (CiContractError, RuntimeContractError, TypeError, ValueError) as error:
            raise CiContractError("previous CI contract is invalid") from error
        if (
            contract["repository"] != repository
            or contract["pull_number"] != pull_number
        ):
            raise CiContractError("previous CI contract identity is invalid")
    report: dict[str, Any] | None = None
    raw_report = payload.get("ci_report", "")
    if raw_report:
        try:
            report = validate_dynamic_ci_report(
                runtime_contracts.parse_canonical_json(
                    raw_report,
                    label="previous CI report",
                    max_bytes=runtime_contracts.MAX_DYNAMIC_CI_REPORT_BYTES,
                )
            )
        except (CiContractError, RuntimeContractError, TypeError, ValueError) as error:
            raise CiContractError("previous CI report is invalid") from error
        report_contract = report["contract"]
        if (
            report_contract["repository"] != repository
            or report_contract["pull_number"] != pull_number
        ):
            raise CiContractError("previous CI report identity is invalid")
    cursor = payload.get("pr_feedback_cursor", "uninitialized")
    if cursor != "uninitialized":
        try:
            validate_dynamic_pr_feedback_cursor(
                runtime_contracts.parse_canonical_json(
                    cursor,
                    label="previous PR feedback cursor",
                    max_bytes=runtime_contracts.MAX_FEEDBACK_BYTES,
                )
            )
        except (CiContractError, RuntimeContractError, TypeError, ValueError) as error:
            raise CiContractError("previous PR feedback cursor is invalid") from error
    return contract, report, cursor


def _dynamic_prepare_result(
    *,
    transition: str,
    workspace: Path,
    payload: Mapping[str, Any],
    contract: Mapping[str, Any],
    cursor: str,
    pr_base_branch: str | None = None,
    merge_report: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "transition": transition,
        "workspace_path": str(workspace),
        "pr_url": str(payload["pr_url"]),
        "branch_name": str(payload["branch_name"]),
    }
    if pr_base_branch is not None:
        result["pr_base_branch"] = pr_base_branch
    if merge_report is not None:
        result["merge_report"] = merge_report
        return result
    result.update(
        {
            "merge_strategy": str(payload["merge_strategy"]),
            "task_short_id": str(payload["task_short_id"]),
            "ci_contract": canonical_bytes(contract).decode("utf-8"),
            "pr_feedback_cursor": cursor,
        }
    )
    return result


def _prepare_ci_payload(
    payload: Mapping[str, Any],
    *,
    gh_bin: str | None = None,
    report_archiver=None,
    provider_guard: Callable[[], None] | None = None,
) -> dict[str, Any]:
    archive_report = report_archiver or archive_ci_report
    workspace = Path(payload["workspace_path"]).resolve()
    pr_url = payload["pr_url"]
    repository = _repository_from_url(pr_url)
    pull_number = _dynamic_pull_number(pr_url)
    expected_base_branch = payload.get("pr_base_branch")
    if expected_base_branch is not None and (
        not isinstance(expected_base_branch, str)
        or not expected_base_branch
        or expected_base_branch != expected_base_branch.strip()
        or expected_base_branch.startswith("-")
    ):
        raise CiContractError("target_branch_invalid")
    first = read_pull_request(
        workspace,
        pr_url,
        gh_bin=gh_bin,
        provider_guard=provider_guard,
    )
    _validate_pr_metadata(first, payload)
    if (
        expected_base_branch is not None
        and first["baseRefName"] != expected_base_branch
    ):
        raise CiContractError("target_branch_changed")
    head = first["headRefOid"]
    target = first["baseRefOid"]
    contract = _dynamic_contract(
        repository=repository,
        pull_number=pull_number,
        head_oid=head,
        base_oid=target,
    )
    prior_contract, previous_report, cursor = _validate_dynamic_prepare_packets(
        payload,
        repository=repository,
        pull_number=pull_number,
    )
    if str(first.get("state") or "").upper() == "MERGED":
        if previous_report is not None:
            archive_report(workspace, payload["task_short_id"], previous_report)
        return _dynamic_prepare_result(
            transition="ci_prepare_pr_merged",
            workspace=workspace,
            payload=payload,
            contract=contract,
            cursor=cursor,
            pr_base_branch=expected_base_branch,
            merge_report=json.dumps(first, sort_keys=True),
        )
    second = read_pull_request(
        workspace,
        pr_url,
        gh_bin=gh_bin,
        provider_guard=provider_guard,
    )
    _validate_pr_metadata(second, payload)
    if (
        expected_base_branch is not None
        and second["baseRefName"] != expected_base_branch
    ):
        raise CiContractError("target_branch_changed")
    if second["state"] == "MERGED":
        if previous_report is not None:
            archive_report(workspace, payload["task_short_id"], previous_report)
        contract = _dynamic_contract(
            repository=repository,
            pull_number=pull_number,
            head_oid=second["headRefOid"],
            base_oid=second["baseRefOid"],
        )
        return _dynamic_prepare_result(
            transition="ci_prepare_pr_merged",
            workspace=workspace,
            payload=payload,
            contract=contract,
            cursor=cursor,
            pr_base_branch=expected_base_branch,
            merge_report=json.dumps(second, sort_keys=True),
        )
    if (
        second["headRefOid"] != head
        or str(second.get("baseRefOid") or "") != target
        or second.get("baseRefName") != first.get("baseRefName")
    ):
        raise CiContractError("pull request identity changed during CI preparation")
    if prior_contract is not None and (
        prior_contract["head_oid"] != head
        or prior_contract["base_oid"] != target
    ):
        same_cycle = False
    else:
        same_cycle = (
            previous_report is not None
            and previous_report["contract"] == contract
        )
    if previous_report is not None:
        archive_report(workspace, payload["task_short_id"], previous_report)
    result = _dynamic_prepare_result(
        transition=(
            "ci_prepare_ready_retry" if same_cycle else "ci_prepare_ready_initial"
        ),
        workspace=workspace,
        payload=payload,
        contract=contract,
        cursor=cursor,
        pr_base_branch=expected_base_branch,
    )
    if same_cycle:
        result["ci_report"] = str(payload["ci_report"])
    return result


__all__ = [
    "BoundedCommandError",
    "CI_RERUN_ALLOWED_EFFECTS",
    "CiContractError",
    "normalize_github_workflow",
    "prepare_ci_payload",
    "qualify_ci_rerun_effect_closure",
    "read_pull_request",
    "run_bounded_command",
]
