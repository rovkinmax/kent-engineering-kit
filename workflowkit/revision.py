from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
from typing import Any, Iterable

from .model import SpecError
from .profile import ProjectProfile
from .release import (
    ReleaseSourceManifest,
    ReleaseSpec,
    ReleaseSpecError,
    SelectedReleaseArtifacts,
    render_release_preview,
)


PROFILE_PATH = ".kent/workflow-profile.toml"
PROJECT_CONTRACT_PATH = ".kent/project-contract.md"
ALLOWED_FILE_MODES = {"100644", "100755"}
TREE_MODE = "040000"


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
        return payload


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
) -> bytes:
    result = run_git_bytes(
        root,
        "cat-file",
        "blob",
        f"{commit_oid}:{path}",
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip() or "no output"
        raise RevisionPreflightError(
            f"{label} not found at {commit_oid}: {path}: {detail}"
        )
    return result.stdout


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
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
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
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip() or "no output"
        raise RevisionPreflightError(
            f"git {' '.join(args)} failed with exit {result.returncode}: {detail}"
        )
    return result
