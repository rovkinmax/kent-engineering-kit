from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from pathlib import Path, PurePosixPath
import subprocess
from typing import Any

from .model import SpecError
from .profile import ProjectProfile


PROFILE_PATH = ".kent/workflow-profile.toml"
PROJECT_CONTRACT_PATH = ".kent/project-contract.md"


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
    baseline_ref: str
    baseline_commit_oid: str
    project_name: str
    workflow_prefix: str
    checked_paths: tuple[CheckedRevisionPath, ...]

    def as_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ready"] = True
        return payload


def preflight_project_revision(
    project_root: Path,
    revision: str,
    baseline_revision: str,
) -> RevisionPreflightResult:
    root = project_root.expanduser().resolve()
    requested_ref = normalize_revision(revision, "revision")
    baseline_ref = normalize_revision(baseline_revision, "baseline revision")

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
    baseline_commit_oid = run_git(
        root,
        "rev-parse",
        "--verify",
        f"{baseline_ref}^{{commit}}",
    ).stdout.strip()
    ancestry = run_git(
        root,
        "merge-base",
        "--is-ancestor",
        baseline_commit_oid,
        commit_oid,
        check=False,
    )
    if ancestry.returncode == 1:
        raise RevisionPreflightError(
            f"baseline {baseline_ref} ({baseline_commit_oid}) is not an "
            f"ancestor of {requested_ref} ({commit_oid})"
        )
    if ancestry.returncode != 0:
        detail = ancestry.stderr.strip() or ancestry.stdout.strip() or "no output"
        raise RevisionPreflightError(
            f"cannot compare baseline ancestry: {detail}"
        )

    baseline_profile_contents = read_blob(
        root,
        baseline_commit_oid,
        PROFILE_PATH,
        label="baseline project profile",
    )
    profile_contents = read_blob(
        root,
        commit_oid,
        PROFILE_PATH,
        label="project profile",
    )
    try:
        baseline_profile = ProjectProfile.from_toml(
            root,
            baseline_profile_contents,
            source=f"{baseline_ref}:{PROFILE_PATH}",
            check_files=False,
        )
        profile = ProjectProfile.from_toml(
            root,
            profile_contents,
            source=f"{requested_ref}:{PROFILE_PATH}",
            check_files=False,
        )
    except SpecError as error:
        raise RevisionPreflightError(str(error)) from error

    differing_fields = profile_differences(baseline_profile, profile)
    if differing_fields:
        raise RevisionPreflightError(
            f"project profile at {requested_ref} differs from audited baseline "
            f"{baseline_ref}: {', '.join(differing_fields)}"
        )

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
    for key, path in baseline_profile.commands.items():
        if path:
            require(path, f"commands.{key}", executable=True)
    for key, path in baseline_profile.procedures.items():
        if path:
            require(path, f"procedures.{key}")
    for key in baseline_profile.required_adapters:
        require(
            baseline_profile.adapter(key),
            f"adapters.{key}",
            executable=True,
        )

    checked_paths = []
    for path, requirement in sorted(requirements.items()):
        mode = tree_mode(root, commit_oid, path)
        if not mode.startswith("100"):
            labels = ", ".join(sorted(requirement["labels"]))
            raise RevisionPreflightError(
                f"{path} for {labels} is not a regular tracked file at "
                f"{requested_ref} ({mode})"
            )
        if requirement["executable"] and mode != "100755":
            labels = ", ".join(sorted(requirement["labels"]))
            raise RevisionPreflightError(
                f"{path} for {labels} is not executable at {requested_ref} "
                f"({mode})"
            )
        checked_paths.append(
            CheckedRevisionPath(
                path=path,
                labels=tuple(sorted(requirement["labels"])),
                mode=mode,
                executable_required=requirement["executable"],
            )
        )

    return RevisionPreflightResult(
        project=str(root),
        requested_ref=requested_ref,
        commit_oid=commit_oid,
        baseline_ref=baseline_ref,
        baseline_commit_oid=baseline_commit_oid,
        project_name=profile.project_name,
        workflow_prefix=profile.workflow_prefix,
        checked_paths=tuple(checked_paths),
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


def profile_differences(
    baseline: ProjectProfile,
    selected: ProjectProfile,
) -> tuple[str, ...]:
    return tuple(
        field.name
        for field in fields(ProjectProfile)
        if field.name != "project_root"
        and getattr(baseline, field.name) != getattr(selected, field.name)
    )


def normalize_project_path(path: str, label: str) -> str:
    relative = PurePosixPath(path)
    if relative.is_absolute():
        raise RevisionPreflightError(f"{label} must be project-relative")
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise RevisionPreflightError(
            f"{label} must be a normalized project-relative path: {path!r}"
        )
    return relative.as_posix()


def read_blob(root: Path, commit_oid: str, path: str, *, label: str) -> str:
    result = run_git(
        root,
        "show",
        f"{commit_oid}:{path}",
        check=False,
    )
    if result.returncode != 0:
        raise RevisionPreflightError(
            f"{label} not found at {commit_oid}: {path}"
        )
    return result.stdout


def tree_mode(root: Path, commit_oid: str, path: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-z", commit_oid, "--", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip() or "no output"
        raise RevisionPreflightError(
            f"git ls-tree failed for {path}: {detail}"
        )
    records = [record for record in result.stdout.split(b"\0") if record]
    if len(records) != 1:
        raise RevisionPreflightError(
            f"required path not found at {commit_oid}: {path}"
        )
    metadata, separator, raw_path = records[0].partition(b"\t")
    if not separator or raw_path.decode(errors="surrogateescape") != path:
        raise RevisionPreflightError(
            f"cannot resolve exact required path at {commit_oid}: {path}"
        )
    mode, object_kind, _object_id = metadata.decode().split()
    if object_kind != "blob":
        raise RevisionPreflightError(
            f"required path is not a blob at {commit_oid}: {path}"
        )
    return mode


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
