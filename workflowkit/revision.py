from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import signal
import subprocess
import time
from typing import Any, Iterable

from .model import SpecError
from .profile import ProjectProfile
from .release import (
    ReleaseSourceManifest,
    ReleaseSpec,
    ReleaseSpecError,
    SelectedReleaseArtifacts,
    render_release_preview,
    validate_native_agent_approvals,
)
from .runtime import (
    MAX_EXTERNAL_ROOT_BYTES,
    MAX_EXTERNAL_TOTAL_BYTES,
    RuntimeExternalRoot,
    SelectedRuntimeSourceInputs,
    _make_selected_runtime_source_inputs,
)


PROFILE_PATH = ".kent/workflow-profile.toml"
PROJECT_CONTRACT_PATH = ".kent/project-contract.md"
ALLOWED_FILE_MODES = {"100644", "100755"}
TREE_MODE = "040000"
GIT_COMMAND_TIMEOUT_SECONDS = 30.0
GIT_COMMAND_OUTPUT_BYTES = 8 * 1024 * 1024
SOURCE_READ_TIMEOUT_SECONDS = 60.0
SOURCE_READ_TOTAL_BYTES = 32 * 1024 * 1024


@dataclass
class _SourceReadBudget:
    deadline: float
    remaining_bytes: int


_SOURCE_READ_BUDGET: ContextVar[_SourceReadBudget | None] = ContextVar(
    "revision_source_read_budget", default=None,
)


def selected_git_environment() -> dict[str, str]:
    """Select the caller's explicit root, never inherited repository routing."""
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
    })
    return environment


def selected_git_command(root: Path, *args: str) -> list[str]:
    return [
        "git", "--no-replace-objects", "--no-lazy-fetch", "-C", str(root),
        "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
        "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args,
    ]


@contextmanager
def source_read_budget():
    """One cumulative budget for the selected-revision read, including preflight."""
    if _SOURCE_READ_BUDGET.get() is not None:
        yield
        return
    token = _SOURCE_READ_BUDGET.set(_SourceReadBudget(
        time.monotonic() + SOURCE_READ_TIMEOUT_SECONDS, SOURCE_READ_TOTAL_BYTES,
    ))
    try:
        yield
    finally:
        _SOURCE_READ_BUDGET.reset(token)


class RevisionPreflightError(RuntimeError):
    """Raised when a Git revision cannot satisfy a project workflow profile."""


@dataclass(frozen=True)
class CheckedRevisionPath:
    path: str
    labels: tuple[str, ...]
    mode: str
    executable_required: bool


@dataclass(frozen=True)
class RevisionPreflightResult:
    project: str
    requested_ref: str
    commit_oid: str
    project_name: str
    workflow_prefix: str
    checked_paths: tuple[CheckedRevisionPath, ...]
    release_preview: dict[str, Any] | None = None
    selected_runtime_source_inputs: SelectedRuntimeSourceInputs | None = None

    def as_json(self) -> dict[str, Any]:
        payload = {
            "project": self.project,
            "requested_ref": self.requested_ref,
            "commit_oid": self.commit_oid,
            "project_name": self.project_name,
            "workflow_prefix": self.workflow_prefix,
            "checked_paths": [asdict(item) for item in self.checked_paths],
            "ready": True,
        }
        if self.release_preview is not None:
            payload["release_preview"] = self.release_preview
        if self.selected_runtime_source_inputs is not None:
            payload["selected_runtime_source_inputs"] = (
                self.selected_runtime_source_inputs.as_dict()
            )
        return payload

    @property
    def runtime_source_inputs(self) -> SelectedRuntimeSourceInputs | None:
        return self.selected_runtime_source_inputs


@dataclass(frozen=True)
class _TreeEntry:
    mode: str
    object_kind: str
    object_id: str
    path: str


def preflight_project_revision(
    project_root: Path,
    revision: str,
) -> RevisionPreflightResult:
    with source_read_budget():
        return _preflight_project_revision(project_root, revision)


def _preflight_project_revision(
    project_root: Path,
    revision: str,
) -> RevisionPreflightResult:
    root = project_root.expanduser().resolve()
    requested_ref = normalize_revision(revision, "revision")
    git_root = Path(
        run_git(root, "rev-parse", "--show-toplevel").stdout.strip()
    ).resolve()
    if git_root != root:
        raise RevisionPreflightError(
            f"project must be the Git repository root: {root}; found {git_root}"
        )

    commit_oid = run_git(
        root,
        "rev-parse",
        "--verify",
        f"{requested_ref}^{{commit}}",
    ).stdout.strip()
    profile_bytes = read_blob_bytes(
        root,
        commit_oid,
        PROFILE_PATH,
        label="project profile",
    )
    profile_contents = decode_text(profile_bytes, PROFILE_PATH)
    try:
        profile = ProjectProfile.from_toml(
            root,
            profile_contents,
            source=f"{requested_ref}:{PROFILE_PATH}",
            check_files=False,
        )
    except SpecError as error:
        raise RevisionPreflightError(str(error)) from error

    requirements = profile_requirements(profile)
    if profile.schema_version == 3:
        checked_paths, _ = check_requirements(
            root,
            commit_oid,
            requested_ref,
            requirements,
            strict_modes=False,
            decode_textual=False,
        )
        return RevisionPreflightResult(
            project=str(root),
            requested_ref=requested_ref,
            commit_oid=commit_oid,
            project_name=profile.project_name,
            workflow_prefix=profile.workflow_prefix,
            checked_paths=checked_paths,
        )

    try:
        spec_path = profile.release.spec_path
    except AttributeError as error:
        raise RevisionPreflightError(
            "schema-4 ProjectProfile has no release profile"
        ) from error
    spec_bytes = read_blob_bytes(
        root,
        commit_oid,
        spec_path,
        label="release spec",
    )
    require_path(
        requirements,
        spec_path,
        "release.spec_path",
    )
    try:
        spec = ReleaseSpec.from_toml(
            decode_text(spec_bytes, spec_path),
            profile=profile,
        )
    except (ReleaseSpecError, ValueError) as error:
        raise RevisionPreflightError(
            f"cannot parse release spec at {requested_ref}:{spec_path}: {error}"
        ) from error

    add_release_requirements(requirements, spec)
    manifest_path = spec.source_manifest.path
    require_path(
        requirements,
        manifest_path,
        "source_manifest.path",
    )
    checked_paths, blobs = check_requirements(
        root,
        commit_oid,
        requested_ref,
        requirements,
        strict_modes=True,
    )
    manifest_bytes = read_blob_bytes(
        root,
        commit_oid,
        manifest_path,
        label="source manifest",
    )
    manifest = parse_source_manifest(manifest_bytes, manifest_path)
    validate_manifest_identity(profile, spec, manifest)

    derived_paths = tuple(sorted(requirements))
    try:
        manifest.validate(
            project_name=spec.project_name,
            repository=spec.repository,
            topology_kind=spec.topology_kind,
            derived_paths=derived_paths,
            manifest_path=manifest_path,
            check_source_coverage=False,
        )
    except ValueError as error:
        raise RevisionPreflightError(str(error)) from error

    additional_files, additional_checked = expand_manifest_additions(
        root,
        commit_oid,
        requested_ref,
        manifest,
        derived_paths=derived_paths,
        manifest_path=manifest_path,
    )
    checked_by_path = {item.path: item for item in checked_paths}
    checked_by_path.update(additional_checked)
    all_paths = set(derived_paths) | additional_files
    uncovered = sorted(
        set(manifest.declared_prompt_references) - all_paths
    )
    if uncovered:
        raise RevisionPreflightError(
            "declared_prompt_references are not covered by the final path set: "
            f"{uncovered}"
        )
    for path in sorted(all_paths):
        if path not in blobs:
            blobs[path] = read_blob_bytes(
                root,
                commit_oid,
                path,
                label="selected source",
            )
        decode_text(blobs[path], path)

    snapshot_path = profile.release.snapshot_path
    snapshot_bytes = blobs[snapshot_path]
    snapshot = parse_snapshot(snapshot_bytes, snapshot_path)
    try:
        validate_native_agent_approvals(spec, snapshot)
    except (ReleaseSpecError, ValueError) as error:
        raise RevisionPreflightError(
            f"cannot validate release snapshot at "
            f"{requested_ref}:{snapshot_path}: {error}"
        ) from error
    del snapshot

    artifacts = SelectedReleaseArtifacts(
        spec_raw_blob_sha256=digest(spec_bytes),
        source_manifest_raw_blob_sha256=digest(manifest_bytes),
        snapshot_raw_blob_sha256=digest(snapshot_bytes),
        builder_raw_blob_sha256=(
            digest(blobs[profile.release.builder_path])
            if profile.release.builder_path
            else None
        ),
        derived_paths=derived_paths,
        additional_paths=manifest.additional_paths,
        additional_trees=manifest.additional_trees,
        declared_prompt_references=manifest.declared_prompt_references,
    )
    try:
        preview = render_release_preview(
            spec,
            {},
            artifacts,
            job_sources_validated=False,
        )
    except (ReleaseSpecError, ValueError) as error:
        raise RevisionPreflightError(
            f"cannot render release preview at {requested_ref}: {error}"
        ) from error
    try:
        runtime_inputs = _make_selected_runtime_source_inputs(
            project_name=spec.project_name,
            repository=spec.repository,
            topology_kind=spec.topology_kind,
            project_commit=commit_oid,
            source_preview=preview,
            artifact_digests=preview["artifact_digests"],
            external_roots=tuple(
                RuntimeExternalRoot(
                    root.kind,
                    root.key,
                    root.runtime_digest_required,
                )
                for root in manifest.external_roots
            ),
        )
    except (TypeError, ValueError) as error:
        raise RevisionPreflightError(
            f"cannot bind selected runtime source inputs at "
            f"{requested_ref}: {error}"
        ) from error
    checked_paths = tuple(
        sorted(
            set(checked_by_path.values()),
            key=lambda item: item.path,
        )
    )
    return RevisionPreflightResult(
        project=str(root),
        requested_ref=requested_ref,
        commit_oid=commit_oid,
        project_name=profile.project_name,
        workflow_prefix=profile.workflow_prefix,
        checked_paths=checked_paths,
        release_preview=preview,
        selected_runtime_source_inputs=runtime_inputs,
    )


def normalize_revision(revision: str, label: str) -> str:
    normalized = revision.strip()
    if not normalized:
        raise RevisionPreflightError(f"{label} must not be empty")
    if normalized.startswith("-") or any(
        character.isspace() for character in normalized
    ):
        raise RevisionPreflightError(
            f"{label} must not start with '-' or contain whitespace"
        )
    return normalized


def profile_requirements(
    profile: ProjectProfile,
) -> dict[str, dict[str, Any]]:
    requirements: dict[str, dict[str, Any]] = {}

    def require(path: str, label: str, *, executable: bool = False) -> None:
        normalized = normalize_project_path(path, label)
        entry = requirements.setdefault(
            normalized,
            {"labels": set(), "executable": False},
        )
        entry["labels"].add(label)
        entry["executable"] = entry["executable"] or executable

    require(PROFILE_PATH, "profile")
    require(PROJECT_CONTRACT_PATH, "project_contract")
    for key, path in profile.commands.items():
        if path:
            require(path, f"commands.{key}", executable=True)
    for key, path in profile.procedures.items():
        if path:
            require(path, f"procedures.{key}")
    for key, path in profile.context_manifests.items():
        if path:
            require(path, f"context_manifests.{key}")
    for key, work_kind in profile.work_kinds.items():
        require(work_kind.plan, f"work_kinds.{key}.plan")
        require(work_kind.implement, f"work_kinds.{key}.implement")
    for key in profile.required_adapters:
        require(
            profile.adapter(key),
            f"adapters.{key}",
            executable=True,
        )
    if profile.release is not None:
        require(profile.release.spec_path, "release.spec_path")
        if profile.release.builder_path:
            require(
                profile.release.builder_path,
                "release.builder_path",
                executable=profile.release.adoption_mode == "managed-in-place",
            )
        require(profile.release.snapshot_path, "release.snapshot_path")
    return requirements


def add_release_requirements(
    requirements: dict[str, dict[str, Any]],
    spec: ReleaseSpec,
) -> None:
    def require(path: str, label: str, *, executable: bool = False) -> None:
        require_path(requirements, path, label, executable=executable)

    for set_kind, table in (
        ("required", spec.required_jobs_v1),
        ("qualification", spec.qualification_jobs_v1),
        ("effect", spec.effect_jobs_v1),
    ):
        for row in table.jobs:
            require(
                row["workflow_path"],
                f"{set_kind}_jobs_v1.{row['contract_key']}.workflow_path",
            )
    for materialization in spec.approval_materializations:
        require(
            materialization.source_path,
            f"approval_materializations.{materialization.variant_key}.source_path",
            executable=True,
        )


def require_path(
    requirements: dict[str, dict[str, Any]],
    path: str,
    label: str,
    *,
    executable: bool = False,
) -> None:
    normalized = normalize_project_path(path, label)
    entry = requirements.setdefault(
        normalized,
        {"labels": set(), "executable": False},
    )
    entry["labels"].add(label)
    entry["executable"] = entry["executable"] or executable


def normalize_project_path(path: str, label: str) -> str:
    relative = PurePosixPath(path)
    if (
        not path
        or relative.is_absolute()
        or "\\" in path
        or relative.as_posix() != path
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise RevisionPreflightError(
            f"{label} must be a normalized project-relative path: {path!r}"
        )
    return relative.as_posix()


def check_requirements(
    root: Path,
    commit_oid: str,
    requested_ref: str,
    requirements: dict[str, dict[str, Any]],
    *,
    strict_modes: bool,
    decode_textual: bool = True,
) -> tuple[tuple[CheckedRevisionPath, ...], dict[str, bytes]]:
    checked: list[CheckedRevisionPath] = []
    blobs: dict[str, bytes] = {}
    for path, requirement in sorted(requirements.items()):
        entry = tree_entry(root, commit_oid, path)
        if entry is None:
            raise RevisionPreflightError(
                f"required path not found at {requested_ref}: {path}"
            )
        if entry.object_kind != "blob":
            raise RevisionPreflightError(
                f"{path} is not a blob at {requested_ref} ({entry.mode})"
            )
        if strict_modes:
            if entry.mode not in ALLOWED_FILE_MODES:
                raise RevisionPreflightError(
                    f"{path} for {', '.join(sorted(requirement['labels']))} "
                    f"is not a regular tracked file at {requested_ref} "
                    f"({entry.mode})"
                )
        elif not entry.mode.startswith("100"):
            labels = ", ".join(sorted(requirement["labels"]))
            raise RevisionPreflightError(
                f"{path} for {labels} is not a regular tracked file at "
                f"{requested_ref} ({entry.mode})"
            )
        if requirement["executable"] and entry.mode != "100755":
            labels = ", ".join(sorted(requirement["labels"]))
            raise RevisionPreflightError(
                f"{path} for {labels} is not executable at {requested_ref} "
                f"({entry.mode})"
            )
        raw = read_blob_bytes(root, commit_oid, path, label=path)
        if decode_textual:
            decode_text(raw, path)
        blobs[path] = raw
        checked.append(
            CheckedRevisionPath(
                path=path,
                labels=tuple(sorted(requirement["labels"])),
                mode=entry.mode,
                executable_required=requirement["executable"],
            )
        )
    return tuple(checked), blobs


def parse_source_manifest(raw: bytes, path: str) -> ReleaseSourceManifest:
    try:
        contents = decode_text(raw, path)
        value = json.loads(contents, parse_constant=reject_json_constant)
        return ReleaseSourceManifest.from_dict(value)
    except (UnicodeError, json.JSONDecodeError, ReleaseSpecError, ValueError) as error:
        raise RevisionPreflightError(
            f"cannot parse source manifest at {path}: {error}"
        ) from error


def parse_snapshot(raw: bytes, path: str) -> dict[str, Any]:
    try:
        value = json.loads(
            decode_text(raw, path),
            parse_constant=reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise RevisionPreflightError(
            f"cannot parse release snapshot at {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise RevisionPreflightError(
            f"release snapshot at {path} must be a JSON object"
        )
    return value


def reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def validate_manifest_identity(
    profile: ProjectProfile,
    spec: ReleaseSpec,
    manifest: ReleaseSourceManifest,
) -> None:
    if manifest.project_name != profile.project_name:
        raise RevisionPreflightError(
            "source manifest.project_name does not match ProjectProfile"
        )
    if manifest.project_name != spec.project_name:
        raise RevisionPreflightError(
            "source manifest.project_name does not match release spec"
        )
    if manifest.repository != spec.repository:
        raise RevisionPreflightError(
            "source manifest.repository does not match release spec"
        )
    if manifest.topology_kind != spec.topology_kind:
        raise RevisionPreflightError(
            "source manifest.topology_kind does not match release spec"
        )
    if manifest.schema != spec.source_manifest.schema:
        raise RevisionPreflightError(
            "source manifest.schema does not match release spec"
        )


def expand_manifest_additions(
    root: Path,
    commit_oid: str,
    requested_ref: str,
    manifest: ReleaseSourceManifest,
    *,
    derived_paths: Iterable[str],
    manifest_path: str,
) -> tuple[set[str], dict[str, CheckedRevisionPath]]:
    derived = set(derived_paths)
    files: set[str] = set()
    checked: dict[str, CheckedRevisionPath] = {}
    for path in manifest.additional_paths:
        if path in derived:
            raise RevisionPreflightError(
                f"additional path duplicates derived path: {path}"
            )
        entry = tree_entry(root, commit_oid, path)
        if entry is None:
            raise RevisionPreflightError(
                f"additional path not found at {requested_ref}: {path}"
            )
        if entry.object_kind != "blob" or entry.mode not in ALLOWED_FILE_MODES:
            raise RevisionPreflightError(
                f"additional path must be a regular file at {requested_ref}: "
                f"{path} ({entry.mode})"
            )
        files.add(path)
        checked[path] = CheckedRevisionPath(
            path=path,
            labels=("source_manifest.additional_paths",),
            mode=entry.mode,
            executable_required=False,
        )
    for tree in manifest.additional_trees:
        if tree in derived or any(
            path == tree or path.startswith(tree + "/")
            for path in derived
        ):
            raise RevisionPreflightError(
                f"additional tree contains a derived path: {tree}"
            )
        leaves = expand_tree(
            root,
            commit_oid,
            requested_ref,
            tree,
        )
        for path, mode in leaves:
            if path == manifest_path:
                raise RevisionPreflightError(
                    f"additional tree contains source manifest: {tree}"
                )
            files.add(path)
            checked[path] = CheckedRevisionPath(
                path=path,
                labels=("source_manifest.additional_trees",),
                mode=mode,
                executable_required=False,
            )
    return files, checked


def expand_tree(
    root: Path,
    commit_oid: str,
    requested_ref: str,
    tree_path: str,
) -> tuple[tuple[str, str], ...]:
    root_entry = tree_entry(root, commit_oid, tree_path)
    if root_entry is None:
        raise RevisionPreflightError(
            f"additional tree not found at {requested_ref}: {tree_path}"
        )
    if root_entry.mode != TREE_MODE or root_entry.object_kind != "tree":
        raise RevisionPreflightError(
            f"additional tree must be a tree with mode 040000 at "
            f"{requested_ref}: {tree_path} ({root_entry.mode})"
        )
    leaves: list[tuple[str, str]] = []
    visit_tree(
        root,
        requested_ref,
        root_entry.object_id,
        tree_path,
        leaves,
    )
    if not leaves:
        raise RevisionPreflightError(
            f"additional tree is empty at {requested_ref}: {tree_path}"
        )
    return tuple(sorted(leaves))


def visit_tree(
    root: Path,
    requested_ref: str,
    tree_oid: str,
    tree_path: str,
    leaves: list[tuple[str, str]],
) -> None:
    entries = tree_entries(root, tree_oid)
    if not entries:
        raise RevisionPreflightError(
            f"additional tree is empty at {requested_ref}: {tree_path}"
        )
    for entry in entries:
        path = f"{tree_path}/{entry.path}"
        if entry.mode == TREE_MODE and entry.object_kind == "tree":
            visit_tree(root, requested_ref, entry.object_id, path, leaves)
            continue
        if entry.object_kind != "blob" or entry.mode not in ALLOWED_FILE_MODES:
            raise RevisionPreflightError(
                f"additional tree contains unsupported Git entry at "
                f"{requested_ref}: {path} ({entry.mode}, {entry.object_kind})"
            )
        leaves.append((path, entry.mode))


def tree_entry(
    root: Path,
    commit_oid: str,
    path: str,
) -> _TreeEntry | None:
    result = run_git_bytes(
        root,
        "ls-tree",
        "-z",
        commit_oid,
        "--",
        path,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip() or "no output"
        raise RevisionPreflightError(
            f"git ls-tree failed for {path}: {detail}"
        )
    records = [record for record in result.stdout.split(b"\0") if record]
    if not records:
        return None
    if len(records) != 1:
        raise RevisionPreflightError(
            f"cannot resolve exact selected path: {path}"
        )
    metadata, separator, raw_path = records[0].partition(b"\t")
    if not separator:
        raise RevisionPreflightError(
            f"cannot parse Git tree entry for {path}"
        )
    try:
        mode, object_kind, object_id = metadata.decode("ascii").split()
        decoded_path = raw_path.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as error:
        raise RevisionPreflightError(
            f"cannot parse Git tree entry for {path}: {error}"
        ) from error
    if decoded_path != path:
        return None
    return _TreeEntry(mode, object_kind, object_id, decoded_path)


def tree_entries(root: Path, tree_oid: str) -> tuple[_TreeEntry, ...]:
    result = run_git_bytes(
        root,
        "ls-tree",
        "-z",
        tree_oid,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip() or "no output"
        raise RevisionPreflightError(
            f"git ls-tree failed for tree {tree_oid}: {detail}"
        )
    entries = []
    for record in (item for item in result.stdout.split(b"\0") if item):
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise RevisionPreflightError(
                f"cannot parse Git tree entry for tree {tree_oid}"
            )
        try:
            mode, object_kind, object_id = metadata.decode("ascii").split()
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as error:
            raise RevisionPreflightError(
                f"cannot parse Git tree entry for tree {tree_oid}: {error}"
            ) from error
        entries.append(_TreeEntry(mode, object_kind, object_id, path))
    return tuple(sorted(entries, key=lambda item: item.path))


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def decode_text(raw: bytes, path: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RevisionPreflightError(
            f"{path} must be valid UTF-8: {error}"
        ) from error


def read_blob_bytes(
    root: Path,
    commit_oid: str,
    path: str,
    *,
    label: str,
    byte_limit: int = GIT_COMMAND_OUTPUT_BYTES,
) -> bytes:
    size_result = run_git_bytes(
        root, "cat-file", "-s", f"{commit_oid}:{path}", check=False,
    )
    if size_result.returncode != 0:
        raise RevisionPreflightError(f"{label} not found at {commit_oid}: {path}")
    try:
        size = int(size_result.stdout)
    except ValueError as error:
        raise RevisionPreflightError("invalid Git blob size") from error
    budget = _SOURCE_READ_BUDGET.get()
    if size < 0 or size > byte_limit or (
        budget is not None and size > budget.remaining_bytes
    ):
        raise RevisionPreflightError("source blob exceeds byte limit")
    result = _run_git_bounded(
        root,
        "cat-file",
        "blob",
        f"{commit_oid}:{path}",
        check=False,
        output_limit=byte_limit,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip() or "no output"
        raise RevisionPreflightError(
            f"{label} not found at {commit_oid}: {path}: {detail}"
        )
    return result.stdout


def collect_runtime_external_captures(
    root: Path,
    inputs: SelectedRuntimeSourceInputs,
) -> tuple[tuple[str, str, bytes], ...]:
    with source_read_budget():
        return _collect_runtime_external_captures(root, inputs)


def _collect_runtime_external_captures(
    root: Path,
    inputs: SelectedRuntimeSourceInputs,
) -> tuple[tuple[str, str, bytes], ...]:
    """Read the declared source captures from the selected Git revision.

    Builder keys are digests; their path is H's profile.release.builder_path.
    Source keys are path=digest. Digests are either lowercase hex64 (SDK, Slack,
    Puber) or exactly sha256:<eight colon-separated lowercase hex8 groups>
    (Appsome). Keys/order remain unchanged in captures and the runtime envelope.
    """
    if not isinstance(inputs, SelectedRuntimeSourceInputs):
        raise RevisionPreflightError("external captures require proven selected inputs")
    captures: list[tuple[str, str, bytes]] = []
    total_bytes = 0
    builder_path: str | None = None
    for descriptor in inputs.external_roots:
        if descriptor.kind not in {"builder-sha256", "source-sha256"}:
            raise RevisionPreflightError(
                f"unsupported runtime external capture kind: {descriptor.kind}"
            )
        if descriptor.kind == "builder-sha256":
            expected = _external_source_digest(descriptor.key)
            if builder_path is None:
                if tree_mode(root, inputs.project_commit, PROFILE_PATH) not in ALLOWED_FILE_MODES:
                    raise RevisionPreflightError("selected builder profile must be a regular Git blob")
                raw_profile = read_blob_bytes(
                    root, inputs.project_commit, PROFILE_PATH, label="selected builder profile",
                )
                try:
                    profile = ProjectProfile.from_toml(
                        root, decode_text(raw_profile, PROFILE_PATH),
                        source=f"{inputs.project_commit}:{PROFILE_PATH}", check_files=False,
                    )
                except SpecError as error:
                    raise RevisionPreflightError("selected builder profile is invalid") from error
                if profile.release is None or not profile.release.builder_path:
                    raise RevisionPreflightError("selected builder profile has no builder_path")
                builder_path = profile.release.builder_path
            path = builder_path
        else:
            path, separator, encoded = descriptor.key.partition("=")
            if not separator or not path:
                raise RevisionPreflightError("source capture descriptor must be path=digest")
            expected = _external_source_digest(encoded)
        normalized = normalize_project_path(
            path,
            f"external capture {descriptor.kind}",
        )
        if tree_mode(root, inputs.project_commit, normalized) not in ALLOWED_FILE_MODES:
            raise RevisionPreflightError("external capture must be a regular Git blob")
        contents = read_blob_bytes(
            root,
            inputs.project_commit,
            normalized,
            label="runtime external capture",
            byte_limit=min(MAX_EXTERNAL_ROOT_BYTES, MAX_EXTERNAL_TOTAL_BYTES - total_bytes),
        )
        total_bytes += len(contents)
        actual = digest(contents)
        if actual != expected:
            raise RevisionPreflightError(
                f"runtime external capture digest mismatch for {normalized}"
            )
        captures.append((descriptor.kind, descriptor.key, contents))
    return tuple(captures)


def _external_source_digest(encoded: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", encoded):
        return encoded
    if re.fullmatch(r"sha256:(?:[0-9a-f]{8}:){7}[0-9a-f]{8}", encoded):
        # Match the existing Appsome codec before decoding; no tolerant
        # colon-stripping, whitespace, case-folding or alternate delimiters.
        return "".join(encoded.split(":")[1:])
    raise RevisionPreflightError("external capture digest encoding is invalid")


def read_blob(root: Path, commit_oid: str, path: str, *, label: str) -> str:
    return decode_text(
        read_blob_bytes(root, commit_oid, path, label=label),
        path,
    )


def tree_mode(root: Path, commit_oid: str, path: str) -> str:
    entry = tree_entry(root, commit_oid, path)
    if entry is None:
        raise RevisionPreflightError(
            f"required path not found at {commit_oid}: {path}"
        )
    if entry.object_kind != "blob":
        raise RevisionPreflightError(
            f"required path is not a blob at {commit_oid}: {path}"
        )
    return entry.mode


def run_git(
    root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    raw = _run_git_bounded(root, *args, check=check)
    result = subprocess.CompletedProcess(
        raw.args,
        raw.returncode,
        raw.stdout.decode("utf-8", errors="replace"),
        raw.stderr.decode("utf-8", errors="replace"),
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise RevisionPreflightError(
            f"git {' '.join(args)} failed with exit {result.returncode}: {detail}"
        )
    return result


def run_git_bytes(
    root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    result = _run_git_bounded(root, *args, check=check)
    if check and result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip() or "no output"
        raise RevisionPreflightError(
            f"git {' '.join(args)} failed with exit {result.returncode}: {detail}"
        )
    return result


def _terminate_git_process(
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
    # A reaped leader does not prove its TERM-ignoring descendants have exited.
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def owned_process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _run_git_bounded(
    root: Path,
    *args: str,
    check: bool,
    output_limit: int | None = None,
) -> subprocess.CompletedProcess[bytes]:
    budget = _SOURCE_READ_BUDGET.get()
    now = time.monotonic()
    deadline = min(
        now + GIT_COMMAND_TIMEOUT_SECONDS,
        budget.deadline if budget is not None else float("inf"),
    )
    if deadline <= now:
        raise RevisionPreflightError("source read timed out")
    if budget is not None and budget.remaining_bytes <= 0:
        raise RevisionPreflightError("source read output exceeded limit")
    output_limit = min(
        GIT_COMMAND_OUTPUT_BYTES,
        output_limit if output_limit is not None else GIT_COMMAND_OUTPUT_BYTES,
    )
    command = selected_git_command(root, *args)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=selected_git_environment(),
    )
    assert process.stdout is not None
    assert process.stderr is not None
    process_group_id = process.pid
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    failure: str | None = None
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "timed out"
                break
            events = selector.select(remaining)
            if not events:
                failure = "timed out"
                break
            for key, _ in events:
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                buffer = outputs[key.data]
                if len(buffer) + len(data) > output_limit or (
                    budget is not None and len(data) > budget.remaining_bytes
                ):
                    failure = "output exceeded limit"
                    break
                if budget is not None:
                    budget.remaining_bytes -= len(data)
                buffer.extend(data)
            if failure:
                break
    except BaseException:
        _terminate_git_process(process, process_group_id)
        process.stdout.close()
        process.stderr.close()
        raise
    finally:
        selector.close()
    if failure:
        _terminate_git_process(process, process_group_id)
        process.stdout.close()
        process.stderr.close()
        raise RevisionPreflightError(
            f"git {' '.join(args)} {failure}"
        )
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _terminate_git_process(process, process_group_id)
        process.stdout.close()
        process.stderr.close()
        raise RevisionPreflightError(
            f"git {' '.join(args)} timed out"
        )
    stdout = bytes(outputs["stdout"])
    stderr = bytes(outputs["stderr"])
    process.stdout.close()
    process.stderr.close()
    if owned_process_group_exists(process_group_id):
        _terminate_git_process(process, process_group_id)
        raise RevisionPreflightError("git reader left owned descendants")
    result = subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout,
        stderr,
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip() or "no output"
        raise RevisionPreflightError(
            f"git {' '.join(args)} failed with exit {result.returncode}: {detail}"
        )
    return result
