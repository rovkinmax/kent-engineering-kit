"""Pure release-contract parsing, validation, canonicalization, and previews.

This module deliberately has no project, Git, filesystem, Kent, network, clock,
randomness, locking, journal, or write side effects.  Revision-bound artifact
bytes and their Git digests are supplied by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence
import tomllib

from .model import SpecError


class ReleaseSpecError(SpecError):
    """Raised when a release contract is malformed or inconsistent."""


ReleaseValidationError = ReleaseSpecError
ReleaseError = ReleaseSpecError


SPEC_ROOTS = {
    "schema_version",
    "spec_kind",
    "topology_kind",
    "adoption_mode",
    "project_name",
    "repository",
    "runtime_attested",
    "workflow_source_intent",
    "source_manifest",
    "required_jobs_v1",
    "qualification_jobs_v1",
    "effect_jobs_v1",
    "operation_variants",
    "approval_materializations",
}
REQUIRED_SPEC_ROOTS = SPEC_ROOTS - {"approval_materializations"}
SOURCE_INTENT_KEYS = {
    "name",
    "id",
    "update_kind",
    "expected_project_link",
    "expected_project_default",
    "allow_create",
    "allow_default_change",
    "allow_uuid_change",
}
MANIFEST_KEYS = {
    "schema",
    "closure_algorithm",
    "project_name",
    "repository",
    "topology_kind",
    "additional_paths",
    "additional_trees",
    "declared_prompt_references",
    "external_roots",
    "runtime_attested",
}
SOURCE_MANIFEST_REF_KEYS = {
    "schema",
    "path",
    "revision_binding",
    "runtime_attested",
}
EXTERNAL_ROOT_KEYS = {"kind", "key", "runtime_digest_required"}
JOB_TABLE_KEYS = {"schema", "jobs"}
JOB_ROW_KEYS = {
    "contract_key",
    "workflow_path",
    "event_selector",
    "job_key",
    "job_display_name",
    "matrix",
    "condition",
    "needs",
    "continue_on_error",
    "runs_on",
    "runner_trust",
    "credential_profile",
    "allowed_effects",
    "skip_policy",
    "branch_protection_required",
    "control_plane_fixtures_forbidden",
    "credential_scope_is_job_local",
    "runner_environment_asserted",
    "effective_permissions",
    "effective_defaults_run",
    "github_environment",
    "services",
    "container",
    "checkout_persist_credentials",
    "secret_refs",
    "effective_environment",
    "steps",
}
OPERATION_VARIANT_KEYS = {
    "key",
    "operation_kind",
    "authority_kind",
    "authority_transitions",
    "required_job_contract_keys",
    "qualification_job_contract_keys",
    "effect_job_contract_keys",
    "approval_required",
    "project_fields",
}
PROJECT_FIELD_KEYS = {"name", "type", "nullable", "approval_renderable"}
KENT_AUTHORITY_KEYS = {
    "kind",
    "task_short_id",
    "workflow_id",
    "workflow_revision",
    "project_id",
    "approval_authority",
    "authority_transition",
}
GITHUB_AUTHORITY_KEYS = {
    "kind",
    "workflow_path",
    "workflow_name",
    "event",
    "run_id",
    "attempt",
    "head_sha",
    "ref",
}
APPROVAL_KEYS = {
    "variant_key",
    "source_path",
    "source_node_key",
    "source_node_kind",
    "authority_transition_parameter",
    "summary_language",
    "summary_sections",
    "materialized_before_pending_approval",
    "commentary_equals_summary",
    "decision_may_select_approval",
    "required_fields",
    "templates",
}
WORKFLOW_KEYS = {
    "schema",
    "workflow_path",
    "workflow_display_name",
    "events",
    "permissions",
    "environment",
    "defaults_run",
    "jobs",
}
JOB_KEYS = {
    "job_key",
    "job_display_name",
    "needs",
    "matrix",
    "condition",
    "continue_on_error",
    "runs_on",
    "runner_environment_asserted",
    "effective_permissions",
    "effective_defaults_run",
    "github_environment",
    "services",
    "container",
    "checkout_persist_credentials",
    "secret_refs",
    "effective_environment",
    "steps",
}
STEP_KEYS = {
    "kind",
    "name",
    "condition",
    "continue_on_error",
    "uses",
    "with",
    "run",
    "effective_shell",
    "effective_working_directory",
    "effective_environment",
    "secret_refs",
}
EVENT_KEYS = {
    "name",
    "branches",
    "branches_ignore",
    "tags",
    "tags_ignore",
    "paths",
    "paths_ignore",
    "types",
    "dispatch_inputs",
}
INPUT_KEYS = {
    "name",
    "type",
    "required",
    "default_present",
    "default",
}
POLICY_FIELDS = {
    "runner_trust",
    "credential_profile",
    "allowed_effects",
    "skip_policy",
    "branch_protection_required",
    "control_plane_fixtures_forbidden",
    "credential_scope_is_job_local",
}
NON_PRODUCTION_EFFECTS = {
    "dependency-downloads",
    "github-actions-cache-read-write",
    "github-actions-logs",
    "github-package-read",
    "inspect",
    "read",
    "validate",
    "verify",
    "evidence",
    "test",
}
REQUIRED_CREDENTIAL_PROFILES = {
    "credential-free",
    "github-platform-contents-packages-read",
    "github-platform-contents-read",
    "none",
}
QUALIFICATION_CREDENTIAL_PROFILES = {"credential-free", "none"}
SKIP_POLICIES = {"condition-gated", "event-gated", "never"}
SUMMARY_SECTIONS = (
    "Нужно от вас",
    "Почему",
    "После подтверждения",
)
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NORMALIZED_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REF_RE = re.compile(r"^refs/[^\s]+$")
REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
TASK_SHORT_ID_RE = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")
AUTHORITY_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
PLACEHOLDER_RE = re.compile(r"\{\{([^{}]*)\}\}")
SECRET_REFERENCE_RE = re.compile(
    r"(?:\$\{\{\s*)?secrets\.([A-Za-z_][A-Za-z0-9_-]*)(?:\s*\}\})?"
)
ACTION_REF_RE = re.compile(
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
    r"(?:/[A-Za-z0-9_.-]+)*@[0-9a-f]{40}"
)
GITHUB_HOSTED_TRUST_RE = re.compile(
    r"^github-hosted-[a-z0-9]+(?:-[a-z0-9]+)*-ephemeral(?:-effect)?$"
)
ORGANIZATION_ARC_EFFECT_TRUST = "organization-arc-ephemeral-effect"
APPROVAL_SOURCE_KEYS = {
    "authority_transition",
    "commentary",
    "operation_digest",
    "source_node_key",
    "source_node_kind",
    "source_path",
    "summary",
    "variant_key",
}
SUPPORTED_EVENT_NAMES = {
    "branch_protection_rule",
    "check_run",
    "check_suite",
    "create",
    "delete",
    "deployment",
    "deployment_status",
    "discussion",
    "discussion_comment",
    "fork",
    "gollum",
    "issue_comment",
    "issues",
    "label",
    "merge_group",
    "milestone",
    "page_build",
    "project",
    "project_card",
    "project_column",
    "public",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "pull_request_target",
    "push",
    "release",
    "repository_dispatch",
    "schedule",
    "status",
    "watch",
    "workflow_call",
    "workflow_dispatch",
    "workflow_run",
}
WORKFLOW_INPUT_TYPES = {"boolean", "choice", "environment", "number", "string"}
_VALIDATION_TOKEN = object()


def _error(message: str) -> None:
    raise ReleaseSpecError(message)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _error(f"{label} must be a table")
    if not all(isinstance(key, str) for key in value):
        _error(f"{label} keys must be strings")
    return dict(value)


def _closed(value: Any, allowed: set[str], label: str) -> dict[str, Any]:
    result = _mapping(value, label)
    unknown = sorted(set(result) - allowed)
    if unknown:
        _error(f"{label} has unknown keys: {unknown}")
    return result


def _require_keys(value: Mapping[str, Any], required: set[str], label: str) -> None:
    missing = sorted(required - set(value))
    if missing:
        _error(f"{label} is missing keys: {missing}")


def _required(value: Mapping[str, Any], key: str, label: str) -> Any:
    if key not in value:
        _error(f"{label} is missing {key!r}")
    return value[key]


def _string(value: Any, label: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        _error(f"{label} must be a string")
    if nonempty and not value.strip():
        _error(f"{label} must not be empty")
    return value


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        _error(f"{label} must be a boolean")
    return value


def _integer(value: Any, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _error(f"{label} must be an integer")
    if positive and value <= 0:
        _error(f"{label} must be positive")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        _error(f"{label} must be an array")
    return list(value)


def _sorted_strings(value: Any, label: str, *, unique: bool = True) -> tuple[str, ...]:
    values = tuple(_string(item, f"{label}[]") for item in _list(value, label))
    if unique and len(set(values)) != len(values):
        _error(f"{label} must contain unique values")
    if tuple(sorted(values)) != values:
        _error(f"{label} must be sorted")
    return values


def _sorted_paths(value: Any, label: str) -> tuple[str, ...]:
    values = tuple(_path(item, f"{label}[]") for item in _list(value, label))
    if len(set(values)) != len(values):
        _error(f"{label} must contain unique values")
    if tuple(sorted(values)) != values:
        _error(f"{label} must be sorted")
    return values


def _sorted_keys(value: Any, label: str) -> tuple[str, ...]:
    values = _sorted_strings(value, label)
    for item in values:
        if not NORMALIZED_KEY_RE.fullmatch(item):
            _error(f"{label} contains a non-normalized key")
    return values


def _canonical_scalar(value: Any, label: str) -> str | int | bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    _error(f"{label} must be a string, integer, or boolean")


def _scalar_map(value: Any, label: str) -> dict[str, str | int | bool]:
    result = _mapping(value, label)
    return {
        key: _canonical_scalar(item, f"{label}.{key}")
        for key, item in sorted(result.items())
    }


def _path(value: Any, label: str) -> str:
    text = _string(value, label)
    if text.startswith("/") or "\\" in text:
        _error(f"{label} must be a project-relative POSIX path")
    parts = text.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        _error(f"{label} must be normalized: {text!r}")
    return "/".join(parts)


def _repository(value: Any, label: str = "repository") -> str:
    result = _string(value, label)
    if not REPOSITORY_RE.fullmatch(result):
        _error(f"{label} must be an exact owner/name identity")
    return result


def _normalized_key(value: Any, label: str) -> str:
    result = _string(value, label)
    if not NORMALIZED_KEY_RE.fullmatch(result):
        _error(f"{label} must be a normalized key")
    return result


def canonical_json_bytes(value: Any) -> bytes:
    """Return compact, sorted-key UTF-8 JSON bytes without a trailing newline."""
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_json(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def sha256_digest(value: bytes | bytearray | str | Any) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    elif not isinstance(value, (bytes, bytearray)):
        value = canonical_json_bytes(value)
    return hashlib.sha256(bytes(value)).hexdigest()


canonical_bytes = canonical_json_bytes
canonical_sha256 = sha256_digest
sha256_hex = sha256_digest


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (str, int, bool)) and not isinstance(value, float):
        return value
    if value is None:
        return None
    _error(f"unsupported canonical value type: {type(value).__name__}")


@dataclass(frozen=True)
class WorkflowSourceIntent:
    name: str
    id: str
    update_kind: str
    expected_project_link: str
    expected_project_default: bool
    allow_create: bool
    allow_default_change: bool
    allow_uuid_change: bool

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkflowSourceIntent":
        data = _closed(value, SOURCE_INTENT_KEYS, "workflow_source_intent")
        expected_link = _required(data, "expected_project_link", "workflow_source_intent")
        expected_default = _required(
            data,
            "expected_project_default",
            "workflow_source_intent",
        )
        if not isinstance(expected_link, str):
            _error("workflow_source_intent.expected_project_link must be default or non-default")
        if expected_link not in {"default", "non-default"}:
            _error("workflow_source_intent.expected_project_link is unsupported")
        if not isinstance(expected_default, bool):
            _error("workflow_source_intent.expected_project_default must be a boolean")
        result = cls(
            name=_string(_required(data, "name", "workflow_source_intent"), "workflow_source_intent.name"),
            id=_string(_required(data, "id", "workflow_source_intent"), "workflow_source_intent.id"),
            update_kind=_string(
                _required(data, "update_kind", "workflow_source_intent"),
                "workflow_source_intent.update_kind",
            ),
            expected_project_link=expected_link,
            expected_project_default=expected_default,
            allow_create=_bool(
                _required(data, "allow_create", "workflow_source_intent"),
                "workflow_source_intent.allow_create",
            ),
            allow_default_change=_bool(
                _required(data, "allow_default_change", "workflow_source_intent"),
                "workflow_source_intent.allow_default_change",
            ),
            allow_uuid_change=_bool(
                _required(data, "allow_uuid_change", "workflow_source_intent"),
                "workflow_source_intent.allow_uuid_change",
            ),
        )
        result.validate()
        return result

    @classmethod
    def from_toml(cls, contents: str) -> "WorkflowSourceIntent":
        return cls.from_dict(tomllib.loads(contents))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WorkflowSourceIntent":
        return cls.from_dict(value)

    def validate(self, *, adoption_mode: str | None = None) -> None:
        if not UUID_RE.fullmatch(self.id):
            _error("workflow_source_intent.id must be a UUID")
        if self.expected_project_default != (
            self.expected_project_link == "default"
        ):
            _error(
                "workflow_source_intent.expected_project_default must agree with "
                "expected_project_link"
            )
        if self.update_kind not in {"graph-only", "graph-and-metadata", "metadata-only"}:
            _error(f"unsupported workflow update_kind {self.update_kind!r}")
        if adoption_mode == "managed-in-place" and self.update_kind not in {
            "graph-only",
            "graph-and-metadata",
        }:
            _error("managed-in-place requires graph-only or graph-and-metadata")
        if adoption_mode == "metadata-only" and self.update_kind != "metadata-only":
            _error("metadata-only requires metadata-only workflow intent")
        if self.allow_create or self.allow_default_change or self.allow_uuid_change:
            _error("workflow source allow-flags must all be false")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "id": self.id,
            "update_kind": self.update_kind,
            "expected_project_link": self.expected_project_link,
            "expected_project_default": self.expected_project_default,
            "allow_create": self.allow_create,
            "allow_default_change": self.allow_default_change,
            "allow_uuid_change": self.allow_uuid_change,
        }


@dataclass(frozen=True)
class ExternalRoot:
    kind: str
    key: str
    runtime_digest_required: bool = True

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExternalRoot":
        data = _closed(value, EXTERNAL_ROOT_KEYS, "external_root")
        result = cls(
            kind=_string(_required(data, "kind", "external_root"), "external_root.kind"),
            key=_string(_required(data, "key", "external_root"), "external_root.key"),
            runtime_digest_required=_bool(
                _required(data, "runtime_digest_required", "external_root"),
                "external_root.runtime_digest_required",
            ),
        )
        if not result.runtime_digest_required:
            _error("external_root.runtime_digest_required must be true")
        return result

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "runtime_digest_required": self.runtime_digest_required,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ExternalRoot":
        return cls.from_dict(value)


@dataclass(frozen=True)
class ReleaseSourceManifest:
    schema: str
    closure_algorithm: str
    project_name: str
    repository: str
    topology_kind: str
    additional_paths: tuple[str, ...]
    additional_trees: tuple[str, ...]
    declared_prompt_references: tuple[str, ...]
    external_roots: tuple[ExternalRoot, ...]
    runtime_attested: bool

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ReleaseSourceManifest":
        data = _closed(value, MANIFEST_KEYS, "source_manifest")
        _require_keys(data, MANIFEST_KEYS, "source_manifest")
        paths = _sorted_paths(data["additional_paths"], "additional_paths")
        trees = _sorted_paths(data["additional_trees"], "additional_trees")
        references = _sorted_paths(
            data["declared_prompt_references"],
            "declared_prompt_references",
        )
        for index, tree in enumerate(trees):
            for other in trees[index + 1 :]:
                if tree == other or tree.startswith(other + "/") or other.startswith(tree + "/"):
                    _error("additional_trees may not overlap")
        roots = tuple(
            ExternalRoot.from_dict(item)
            for item in _list(data["external_roots"], "external_roots")
        )
        root_keys = [(item.kind, item.key) for item in roots]
        if root_keys != sorted(root_keys) or len(set(root_keys)) != len(root_keys):
            _error("external_roots must be sorted and unique by kind and key")
        result = cls(
            schema=_string(_required(data, "schema", "source_manifest"), "source_manifest.schema"),
            closure_algorithm=_string(
                _required(data, "closure_algorithm", "source_manifest"),
                "source_manifest.closure_algorithm",
            ),
            project_name=_string(
                _required(data, "project_name", "source_manifest"),
                "source_manifest.project_name",
            ),
            repository=_repository(
                _required(data, "repository", "source_manifest"),
                "source_manifest.repository",
            ),
            topology_kind=_string(
                _required(data, "topology_kind", "source_manifest"),
                "source_manifest.topology_kind",
            ),
            additional_paths=paths,
            additional_trees=trees,
            declared_prompt_references=references,
            external_roots=roots,
            runtime_attested=_bool(
                _required(data, "runtime_attested", "source_manifest"),
                "source_manifest.runtime_attested",
            ),
        )
        result.validate(check_source_coverage=False)
        return result

    @classmethod
    def from_json(cls, contents: str) -> "ReleaseSourceManifest":
        return cls.from_dict(json.loads(contents))

    @classmethod
    def parse(cls, contents: str) -> "ReleaseSourceManifest":
        return cls.from_json(contents)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ReleaseSourceManifest":
        return cls.from_dict(value)

    def validate(
        self,
        *,
        project_name: str | None = None,
        repository: str | None = None,
        topology_kind: str | None = None,
        derived_paths: Iterable[str] = (),
        manifest_path: str | None = None,
        check_source_coverage: bool = True,
    ) -> None:
        if self.schema != "release_source_manifest_v1":
            _error("source_manifest.schema must be release_source_manifest_v1")
        if self.closure_algorithm != "project-instruction-closure-v1":
            _error("source_manifest.closure_algorithm is unsupported")
        if self.runtime_attested:
            _error("tracked source_manifest.runtime_attested must be false")
        if project_name is not None and self.project_name != project_name:
            _error("source_manifest.project_name does not match release spec")
        if repository is not None and self.repository != repository:
            _error("source_manifest.repository does not match release spec")
        if topology_kind is not None and self.topology_kind != topology_kind:
            _error("source_manifest.topology_kind does not match release spec")

        derived = {_path(item, "derived_path") for item in derived_paths}
        additions = set(self.additional_paths)
        if derived & additions:
            _error("additional_paths may not repeat a derived path")
        final_paths = derived | additions
        tree_values = set(self.additional_trees)
        if manifest_path is not None:
            normalized_manifest = _path(manifest_path, "manifest_path")
            if normalized_manifest in additions:
                _error("additional_paths may not contain the manifest")
            for tree in tree_values:
                if normalized_manifest == tree or normalized_manifest.startswith(tree + "/"):
                    _error("additional_trees may not contain the manifest")
        for tree in tree_values:
            if any(
                path == tree
                or path.startswith(tree + "/")
                or tree.startswith(path + "/")
                for path in final_paths
            ):
                _error("derived or additional paths may not be beneath an additional tree")
        if check_source_coverage:
            uncovered = [
                reference
                for reference in self.declared_prompt_references
                if reference not in final_paths
                and not any(
                    reference == tree or reference.startswith(tree + "/")
                    for tree in tree_values
                )
            ]
            if uncovered:
                _error("declared_prompt_references must be covered by the final path set")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "closure_algorithm": self.closure_algorithm,
            "project_name": self.project_name,
            "repository": self.repository,
            "topology_kind": self.topology_kind,
            "additional_paths": list(self.additional_paths),
            "additional_trees": list(self.additional_trees),
            "declared_prompt_references": list(self.declared_prompt_references),
            "external_roots": [item.as_dict() for item in self.external_roots],
            "runtime_attested": self.runtime_attested,
        }


@dataclass(frozen=True)
class SourceManifestReference:
    schema: str
    path: str
    revision_binding: str
    runtime_attested: bool

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SourceManifestReference":
        data = _closed(value, SOURCE_MANIFEST_REF_KEYS, "source_manifest")
        result = cls(
            schema=_string(
                _required(data, "schema", "source_manifest"),
                "source_manifest.schema",
            ),
            path=_path(
                _required(data, "path", "source_manifest"),
                "source_manifest.path",
            ),
            revision_binding=_string(
                _required(data, "revision_binding", "source_manifest"),
                "source_manifest.revision_binding",
            ),
            runtime_attested=_bool(
                _required(data, "runtime_attested", "source_manifest"),
                "source_manifest.runtime_attested",
            ),
        )
        result.validate()
        return result

    @classmethod
    def from_toml(cls, contents: str) -> "SourceManifestReference":
        return cls.from_dict(tomllib.loads(contents))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SourceManifestReference":
        return cls.from_dict(value)

    def validate(self) -> None:
        if self.schema != "release_source_manifest_v1":
            _error("source_manifest.schema must be release_source_manifest_v1")
        if self.revision_binding != "runtime-source-envelope":
            _error("source_manifest.revision_binding is unsupported")
        if self.runtime_attested:
            _error("tracked source_manifest.runtime_attested must be false")
        if not self.path.endswith(".json"):
            _error("source_manifest.path must reference a JSON file")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "path": self.path,
            "revision_binding": self.revision_binding,
            "runtime_attested": self.runtime_attested,
        }


SourceManifestSpec = SourceManifestReference


def _normalize_permissions(value: Any, label: str) -> dict[str, str]:
    data = _mapping(value, label)
    result = {}
    for key, item in sorted(data.items()):
        permission = _string(item, f"{label}.{key}")
        if permission not in {"read", "write", "none"}:
            _error(f"{label}.{key} has unsupported permission {permission!r}")
        result[key] = permission
    return result


def _normalize_string_list(value: Any, label: str) -> tuple[str, ...]:
    values = tuple(_string(item, f"{label}[]", nonempty=False) for item in _list(value, label))
    if len(set(values)) != len(values):
        _error(f"{label} must be unique")
    if tuple(sorted(values)) != values:
        _error(f"{label} must be sorted")
    return values


def _secret_reference_names(value: Any) -> set[str]:
    if isinstance(value, str):
        return {match.group(1) for match in SECRET_REFERENCE_RE.finditer(value)}
    if isinstance(value, Mapping):
        names: set[str] = set()
        for key, item in value.items():
            names.update(_secret_reference_names(key))
            names.update(_secret_reference_names(item))
        return names
    if isinstance(value, (list, tuple)):
        names = set()
        for item in value:
            names.update(_secret_reference_names(item))
        return names
    return set()


def _validate_action_reference(value: str, label: str) -> None:
    if not value:
        return
    reference_path = value.split("@", 1)[0]
    if (
        value.startswith(("./", "../"))
        or ".github/workflows/" in value
        or any(part in {".", ".."} for part in reference_path.split("/"))
    ):
        _error(f"{label} may not use a local action")
    if not ACTION_REF_RE.fullmatch(value):
        _error(f"{label} must use an immutable action reference")


def _runner_trust_class(value: Any, label: str) -> str:
    trust = _string(value, label)
    if GITHUB_HOSTED_TRUST_RE.fullmatch(trust):
        return "github-hosted"
    if trust == ORGANIZATION_ARC_EFFECT_TRUST:
        return "organization-arc"
    _error(f"{label} is not a supported normalized ephemeral trust")


def _normalize_effective_defaults(value: Any, label: str) -> dict[str, str]:
    data = _closed(value, {"shell", "working_directory"}, label)
    _require_keys(data, {"shell", "working_directory"}, label)
    return {
        "shell": _string(data.get("shell", ""), f"{label}.shell", nonempty=False),
        "working_directory": _string(
            data.get("working_directory", ""),
            f"{label}.working_directory",
            nonempty=False,
        ),
    }


def _normalize_service(value: Any, label: str) -> dict[str, Any]:
    data = _closed(value, {"image", "environment", "ports", "options"}, label)
    _require_keys(data, {"image", "environment", "ports", "options"}, label)
    ports = _normalize_string_list(data.get("ports", []), f"{label}.ports")
    image = _string(data.get("image", ""), f"{label}.image", nonempty=False)
    if image and not re.fullmatch(r".+@sha256:[0-9a-f]{64}", image):
        _error(f"{label}.image must use an immutable lowercase SHA-256 reference")
    environment = _scalar_map(data.get("environment", {}), f"{label}.environment")
    return {
        "image": image,
        "environment": environment,
        "ports": list(ports),
        "options": _string(data.get("options", ""), f"{label}.options", nonempty=False),
    }


def _normalize_container(value: Any, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    return _normalize_service(value, label)


@dataclass(frozen=True)
class NormalizedGitHubStepV1:
    kind: str
    name: str
    condition: str
    continue_on_error: bool
    uses: str
    with_values: dict[str, str | int | bool]
    run: str
    effective_shell: str
    effective_working_directory: str
    effective_environment: dict[str, str | int | bool]
    secret_refs: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NormalizedGitHubStepV1":
        data = _closed(value, STEP_KEYS, "workflow step")
        _require_keys(data, STEP_KEYS, "workflow step")
        result = cls(
            kind=_string(_required(data, "kind", "workflow step"), "step.kind"),
            name=_string(_required(data, "name", "workflow step"), "step.name", nonempty=False),
            condition=_string(data.get("condition", ""), "step.condition", nonempty=False),
            continue_on_error=_bool(
                _required(data, "continue_on_error", "workflow step"),
                "step.continue_on_error",
            ),
            uses=_string(data.get("uses", ""), "step.uses", nonempty=False),
            with_values=_scalar_map(data.get("with", {}), "step.with"),
            run=_string(data.get("run", ""), "step.run", nonempty=False),
            effective_shell=_string(
                data.get("effective_shell", ""),
                "step.effective_shell",
                nonempty=False,
            ),
            effective_working_directory=_string(
                data.get("effective_working_directory", ""),
                "step.effective_working_directory",
                nonempty=False,
            ),
            effective_environment=_scalar_map(
                data.get("effective_environment", {}),
                "step.effective_environment",
            ),
            secret_refs=_normalize_string_list(
                data.get("secret_refs", []),
                "step.secret_refs",
            ),
        )
        if result.kind not in {"run", "uses"}:
            _error("step.kind must be run or uses")
        if result.kind == "run" and not result.run:
            _error("run steps must provide run")
        if result.kind == "uses" and not result.uses:
            _error("uses steps must provide uses")
        if result.kind == "run" and result.uses:
            _error("run steps may not provide uses")
        if result.kind == "uses" and result.run:
            _error("uses steps may not provide run")
        _validate_action_reference(result.uses, "step.uses")
        extracted_secrets = _secret_reference_names(
            {
                "kind": result.kind,
                "name": result.name,
                "condition": result.condition,
                "continue_on_error": result.continue_on_error,
                "uses": result.uses,
                "with": result.with_values,
                "run": result.run,
                "effective_shell": result.effective_shell,
                "effective_working_directory": result.effective_working_directory,
                "environment": result.effective_environment,
            }
        )
        if set(result.secret_refs) != extracted_secrets:
            _error("step.secret_refs must exactly match nested secret references")
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "NormalizedGitHubStepV1":
        return cls.from_dict(value)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "condition": self.condition,
            "continue_on_error": self.continue_on_error,
            "uses": self.uses,
            "with": dict(self.with_values),
            "run": self.run,
            "effective_shell": self.effective_shell,
            "effective_working_directory": self.effective_working_directory,
            "effective_environment": dict(self.effective_environment),
            "secret_refs": list(self.secret_refs),
        }


def _normalize_matrix(value: Any, label: str) -> Any:
    if not isinstance(value, dict):
        _error(f"{label} must be one expanded matrix row")
    return _scalar_map(value, label)


@dataclass(frozen=True)
class NormalizedGitHubJobV1:
    job_key: str
    job_display_name: str
    needs: tuple[str, ...]
    matrix: Any
    condition: str
    continue_on_error: bool
    runs_on: str
    runner_environment_asserted: bool
    effective_permissions: dict[str, str]
    effective_defaults_run: dict[str, str]
    github_environment: str
    services: dict[str, dict[str, Any]]
    container: dict[str, Any] | None
    checkout_persist_credentials: bool
    secret_refs: tuple[str, ...]
    effective_environment: dict[str, str | int | bool]
    steps: tuple[NormalizedGitHubStepV1, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NormalizedGitHubJobV1":
        data = _closed(value, JOB_KEYS, "workflow job")
        _require_keys(data, JOB_KEYS, "workflow job")
        services_raw = _mapping(data["services"], "job.services")
        services = {
            key: _normalize_service(item, f"job.services.{key}")
            for key, item in sorted(services_raw.items())
        }
        steps = tuple(
            NormalizedGitHubStepV1.from_dict(item)
            for item in _list(data["steps"], "job.steps")
        )
        if not steps:
            _error("workflow job must have at least one step")
        result = cls(
            job_key=_string(_required(data, "job_key", "workflow job"), "job.job_key"),
            job_display_name=_string(
                _required(data, "job_display_name", "workflow job"),
                "job.job_display_name",
                nonempty=False,
            ),
            needs=_normalize_string_list(data.get("needs", []), "job.needs"),
            matrix=_normalize_matrix(data.get("matrix", {}), "job.matrix"),
            condition=_string(data.get("condition", ""), "job.condition", nonempty=False),
            continue_on_error=_bool(
                _required(data, "continue_on_error", "workflow job"),
                "job.continue_on_error",
            ),
            runs_on=_string(_required(data, "runs_on", "workflow job"), "job.runs_on"),
            runner_environment_asserted=_bool(
                _required(data, "runner_environment_asserted", "workflow job"),
                "job.runner_environment_asserted",
            ),
            effective_permissions=_normalize_permissions(
                data.get("effective_permissions", {}),
                "job.effective_permissions",
            ),
            effective_defaults_run=_normalize_effective_defaults(
                data.get("effective_defaults_run", {}),
                "job.effective_defaults_run",
            ),
            github_environment=_string(
                data.get("github_environment", ""),
                "job.github_environment",
                nonempty=False,
            ),
            services=services,
            container=_normalize_container(data.get("container"), "job.container"),
            checkout_persist_credentials=_bool(
                _required(data, "checkout_persist_credentials", "workflow job"),
                "job.checkout_persist_credentials",
            ),
            secret_refs=_normalize_string_list(
                data.get("secret_refs", []),
                "job.secret_refs",
            ),
            effective_environment=_scalar_map(
                data.get("effective_environment", {}),
                "job.effective_environment",
            ),
            steps=steps,
        )
        extracted_secrets = _secret_reference_names(
            {
                "job_key": result.job_key,
                "job_display_name": result.job_display_name,
                "needs": result.needs,
                "matrix": result.matrix,
                "condition": result.condition,
                "continue_on_error": result.continue_on_error,
                "runs_on": result.runs_on,
                "runner_environment_asserted": result.runner_environment_asserted,
                "effective_permissions": result.effective_permissions,
                "effective_defaults_run": result.effective_defaults_run,
                "github_environment": result.github_environment,
                "checkout_persist_credentials": result.checkout_persist_credentials,
                "environment": result.effective_environment,
                "services": result.services,
                "container": result.container,
                "steps": [step.as_dict() for step in result.steps],
            }
        )
        if set(result.secret_refs) != extracted_secrets:
            _error("job.secret_refs must exactly match nested secret references")
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "NormalizedGitHubJobV1":
        return cls.from_dict(value)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_key": self.job_key,
            "job_display_name": self.job_display_name,
            "needs": list(self.needs),
            "matrix": self.matrix,
            "condition": self.condition,
            "continue_on_error": self.continue_on_error,
            "runs_on": self.runs_on,
            "runner_environment_asserted": self.runner_environment_asserted,
            "effective_permissions": dict(self.effective_permissions),
            "effective_defaults_run": dict(self.effective_defaults_run),
            "github_environment": self.github_environment,
            "services": self.services,
            "container": self.container,
            "checkout_persist_credentials": self.checkout_persist_credentials,
            "secret_refs": list(self.secret_refs),
            "effective_environment": dict(self.effective_environment),
            "steps": [step.as_dict() for step in self.steps],
        }


def _normalize_input(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    data = _closed(value, INPUT_KEYS, label)
    present = _bool(_required(data, "default_present", label), f"{label}.default_present")
    input_type = _string(_required(data, "type", label), f"{label}.type")
    if input_type not in WORKFLOW_INPUT_TYPES:
        _error(f"{label}.type is unsupported")
    if present:
        default = _canonical_scalar(_required(data, "default", label), f"{label}.default")
        if input_type == "boolean" and not isinstance(default, bool):
            _error(f"{label}.default must be boolean for boolean inputs")
        if input_type == "number" and (
            isinstance(default, bool) or not isinstance(default, int)
        ):
            _error(f"{label}.default must be an integer for number inputs")
        if input_type in {"choice", "environment", "string"} and not isinstance(
            default,
            str,
        ):
            _error(f"{label}.default must be a string for {input_type} inputs")
    elif "default" in data:
        _error(f"{label}.default is forbidden when default_present is false")
    else:
        default = None
    return {
        "name": _string(_required(data, "name", label), f"{label}.name"),
        "type": input_type,
        "required": _bool(_required(data, "required", label), f"{label}.required"),
        "default_present": present,
        **({"default": default} if present else {}),
    }


def _normalize_event(value: Mapping[str, Any]) -> dict[str, Any]:
    data = _closed(value, EVENT_KEYS, "workflow event")
    _require_keys(data, EVENT_KEYS, "workflow event")
    event_name = _string(_required(data, "name", "workflow event"), "event.name")
    if event_name not in SUPPORTED_EVENT_NAMES:
        _error(f"workflow event name is unsupported: {event_name!r}")
    inputs = data["dispatch_inputs"]
    normalized_inputs = [
        _normalize_input(item, f"workflow event {event_name}.input")
        for item in _list(inputs, "workflow event.dispatch_inputs")
    ]
    input_names = [item["name"] for item in normalized_inputs]
    if len(set(input_names)) != len(input_names):
        _error("workflow event dispatch inputs must be unique")
    if input_names != sorted(input_names):
        _error("workflow event dispatch inputs must be sorted")
    if normalized_inputs and event_name not in {"workflow_dispatch", "workflow_call"}:
        _error("workflow event dispatch inputs require workflow_dispatch or workflow_call")
    branches = _normalize_string_list(data["branches"], "event.branches")
    branches_ignore = _normalize_string_list(
        data["branches_ignore"],
        "event.branches_ignore",
    )
    tags = _normalize_string_list(data["tags"], "event.tags")
    tags_ignore = _normalize_string_list(data["tags_ignore"], "event.tags_ignore")
    paths = _normalize_string_list(data["paths"], "event.paths")
    paths_ignore = _normalize_string_list(data["paths_ignore"], "event.paths_ignore")
    if branches and branches_ignore:
        _error("workflow event may not define both branches and branches_ignore")
    if tags and tags_ignore:
        _error("workflow event may not define both tags and tags_ignore")
    if paths and paths_ignore:
        _error("workflow event may not define both paths and paths_ignore")
    return {
        "name": event_name,
        "branches": list(branches),
        "branches_ignore": list(branches_ignore),
        "tags": list(tags),
        "tags_ignore": list(tags_ignore),
        "paths": list(paths),
        "paths_ignore": list(paths_ignore),
        "types": list(_normalize_string_list(data["types"], "event.types")),
        "dispatch_inputs": normalized_inputs,
    }


@dataclass(frozen=True)
class NormalizedGitHubWorkflowSourceV1:
    schema: str
    workflow_path: str
    workflow_display_name: str
    events: tuple[dict[str, Any], ...]
    permissions: dict[str, str]
    environment: dict[str, str | int | bool]
    defaults_run: dict[str, str]
    jobs: tuple[NormalizedGitHubJobV1, ...]

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "NormalizedGitHubWorkflowSourceV1":
        data = _closed(value, WORKFLOW_KEYS, "normalized workflow")
        _require_keys(data, WORKFLOW_KEYS, "normalized workflow")
        schema = _string(
            _required(data, "schema", "normalized workflow"),
            "workflow.schema",
        )
        if schema != "normalized_github_workflow_source_v1":
            _error("workflow.schema must be normalized_github_workflow_source_v1")
        events = tuple(
            sorted(
                (
                    _normalize_event(item)
                    for item in _list(data["events"], "workflow.events")
                ),
                key=lambda event: event["name"],
            )
        )
        jobs = tuple(
            NormalizedGitHubJobV1.from_dict(item)
            for item in _list(data["jobs"], "workflow.jobs")
        )
        jobs = tuple(
            sorted(
                jobs,
                key=lambda job: (job.job_key, canonical_json(job.matrix)),
            )
        )
        job_identities = [
            (job.job_key, canonical_json(job.matrix))
            for job in jobs
        ]
        if len(set(job_identities)) != len(job_identities):
            _error("workflow jobs must have unique job_key and matrix values")
        if not events:
            _error("workflow.events must be non-empty")
        event_names = [event["name"] for event in events]
        if len(set(event_names)) != len(event_names):
            _error("workflow.events names must be unique")
        result = cls(
            schema=schema,
            workflow_path=_path(
                _required(data, "workflow_path", "normalized workflow"),
                "workflow.workflow_path",
            ),
            workflow_display_name=_string(
                _required(data, "workflow_display_name", "normalized workflow"),
                "workflow.workflow_display_name",
                nonempty=False,
            ),
            events=events,
            permissions=_normalize_permissions(
                data.get("permissions", {}),
                "workflow.permissions",
            ),
            environment=_scalar_map(data.get("environment", {}), "workflow.environment"),
            defaults_run=_normalize_effective_defaults(
                data.get("defaults_run", {}),
                "workflow.defaults_run",
            ),
            jobs=jobs,
        )
        if _secret_reference_names(
            {
                "schema": result.schema,
                "workflow_path": result.workflow_path,
                "workflow_display_name": result.workflow_display_name,
                "permissions": result.permissions,
                "environment": result.environment,
                "defaults_run": result.defaults_run,
                "events": result.events,
            }
        ):
            _error("workflow.environment may not contain secrets")
        return result

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "NormalizedGitHubWorkflowSourceV1":
        return cls.from_dict(value)

    @classmethod
    def from_json(cls, contents: str) -> "NormalizedGitHubWorkflowSourceV1":
        return cls.from_dict(json.loads(contents))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "workflow_path": self.workflow_path,
            "workflow_display_name": self.workflow_display_name,
            "events": list(self.events),
            "permissions": dict(self.permissions),
            "environment": dict(self.environment),
            "defaults_run": dict(self.defaults_run),
            "jobs": [job.as_dict() for job in self.jobs],
        }


@dataclass(frozen=True)
class JobContractTable:
    schema: str
    jobs: tuple[dict[str, Any], ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], set_kind: str) -> "JobContractTable":
        data = _closed(value, JOB_TABLE_KEYS, f"{set_kind}_jobs_v1")
        _require_keys(data, JOB_TABLE_KEYS, f"{set_kind}_jobs_v1")
        schema = _string(
            _required(data, "schema", f"{set_kind}_jobs_v1"),
            f"{set_kind}_jobs_v1.schema",
        )
        expected_schema = f"{set_kind}_jobs_v1"
        if schema != expected_schema:
            _error(f"{set_kind}_jobs_v1.schema must be {expected_schema}")
        rows = []
        for index, raw in enumerate(_list(data["jobs"], f"{set_kind}_jobs_v1.jobs")):
            row = _closed(raw, JOB_ROW_KEYS, f"{set_kind}_jobs_v1.jobs[{index}]")
            rows.append(
                _validate_contract_row(
                    row,
                    f"{set_kind}_jobs_v1.jobs[{index}]",
                )
            )
        keys = [row["contract_key"] for row in rows]
        if len(set(keys)) != len(keys):
            _error(f"{set_kind}_jobs_v1 contract_key values must be unique")
        if set_kind in {"required", "effect"} and not rows:
            _error(f"{set_kind}_jobs_v1.jobs must be non-empty")
        return cls(
            schema=schema,
            jobs=tuple(sorted(rows, key=lambda row: row["contract_key"])),
        )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        set_kind: str,
    ) -> "JobContractTable":
        return cls.from_dict(value, set_kind)

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "jobs": list(self.jobs)}


def _validate_contract_row(row: Mapping[str, Any], label: str) -> dict[str, Any]:
    _require_keys(row, JOB_ROW_KEYS, label)
    contract_key = _string(row["contract_key"], f"{label}.contract_key")
    if not NORMALIZED_KEY_RE.fullmatch(contract_key):
        _error(f"{label}.contract_key must be a normalized key")
    workflow_path = _path(row["workflow_path"], f"{label}.workflow_path")
    job_key = _string(row["job_key"], f"{label}.job_key")
    selector = row["event_selector"]
    if not isinstance(selector, Mapping):
        _error(f"{label}.event_selector must be a complete event record")
    selector_value: Any = _normalize_event(selector)
    declared_steps = _list(row["steps"], f"{label}.steps")
    source_steps = []
    overlays = []
    for index, step in enumerate(declared_steps):
        step_label = f"{label}.steps[{index}]"
        step_data = _closed(
            step,
            STEP_KEYS | {"validation_required"},
            step_label,
        )
        _require_keys(step_data, STEP_KEYS | {"validation_required"}, step_label)
        overlay = _bool(
            step_data["validation_required"],
            f"{step_label}.validation_required",
        )
        overlays.append(overlay)
        source_steps.append({key: step_data[key] for key in STEP_KEYS})
    source = {key: row[key] for key in JOB_KEYS}
    source["steps"] = source_steps
    normalized_job = NormalizedGitHubJobV1.from_dict(source)
    if normalized_job.job_key != job_key:
        _error(f"{label}.job_key does not match normalized job source")
    if not NORMALIZED_KEY_RE.fullmatch(job_key):
        _error(f"{label}.job_key must be a normalized key")
    for key in POLICY_FIELDS:
        value = row[key]
        if key in {
            "branch_protection_required",
            "control_plane_fixtures_forbidden",
            "credential_scope_is_job_local",
        }:
            _bool(value, f"{label}.{key}")
        elif key == "allowed_effects":
            _sorted_strings(value, f"{label}.{key}")
        elif key == "runner_trust":
            _runner_trust_class(value, f"{label}.{key}")
        elif key == "credential_profile":
            _normalized_key(value, f"{label}.{key}")
        elif key == "skip_policy":
            skip_policy = _string(value, f"{label}.{key}")
            if skip_policy not in SKIP_POLICIES:
                _error(f"{label}.skip_policy is unsupported")
        else:
            _string(value, f"{label}.{key}", nonempty=False)
    normalized = dict(row)
    normalized.update(normalized_job.as_dict())
    normalized["workflow_path"] = workflow_path
    normalized["event_selector"] = selector_value
    normalized["steps"] = [
        {
            **step,
            "validation_required": overlays[index],
        }
        for index, step in enumerate(normalized_job.as_dict()["steps"])
    ]
    return _canonical_value(normalized)


@dataclass(frozen=True)
class ProjectField:
    name: str
    type: str
    nullable: bool
    approval_renderable: bool

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], label: str = "project_field") -> "ProjectField":
        data = _closed(value, PROJECT_FIELD_KEYS, label)
        result = cls(
            name=_string(_required(data, "name", label), f"{label}.name"),
            type=_string(_required(data, "type", label), f"{label}.type"),
            nullable=_bool(_required(data, "nullable", label), f"{label}.nullable"),
            approval_renderable=_bool(
                _required(data, "approval_renderable", label),
                f"{label}.approval_renderable",
            ),
        )
        if result.type not in {"string", "integer", "boolean"}:
            _error(f"{label}.type is unsupported")
        if not IDENTIFIER_RE.fullmatch(result.name):
            _error(f"{label}.name must be an identifier")
        if result.nullable and result.approval_renderable:
            _error(f"{label} nullable fields may not be approval-renderable")
        return result

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "nullable": self.nullable,
            "approval_renderable": self.approval_renderable,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        label: str = "project_field",
    ) -> "ProjectField":
        return cls.from_dict(value, label)


@dataclass(frozen=True)
class AuthoritySpec:
    kind: str
    values: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AuthoritySpec":
        data = _mapping(value, "authority_kind")
        kind = _string(_required(data, "kind", "authority_kind"), "authority_kind.kind")
        allowed = KENT_AUTHORITY_KEYS if kind == "kent_transition" else GITHUB_AUTHORITY_KEYS
        if kind not in {"kent_transition", "github_run"}:
            _error(f"authority_kind.kind is unsupported: {kind!r}")
        data = _closed(data, allowed, "authority_kind")
        if kind == "kent_transition":
            values = {
                "kind": kind,
                "task_short_id": _string(
                    _required(data, "task_short_id", "authority_kind"),
                    "authority_kind.task_short_id",
                ),
                "workflow_id": _string(
                    _required(data, "workflow_id", "authority_kind"),
                    "authority_kind.workflow_id",
                ),
                "workflow_revision": _integer(
                    _required(data, "workflow_revision", "authority_kind"),
                    "authority_kind.workflow_revision",
                    positive=True,
                ),
                "project_id": _string(
                    _required(data, "project_id", "authority_kind"),
                    "authority_kind.project_id",
                ),
                "approval_authority": _string(
                    _required(data, "approval_authority", "authority_kind"),
                    "authority_kind.approval_authority",
                ),
                "authority_transition": _string(
                    _required(data, "authority_transition", "authority_kind"),
                    "authority_kind.authority_transition",
                ),
            }
            if not UUID_RE.fullmatch(values["workflow_id"]):
                _error("authority_kind.workflow_id must be a UUID")
            if not TASK_SHORT_ID_RE.fullmatch(values["task_short_id"]):
                _error("authority_kind.task_short_id must be a normalized Task ID")
            project_id = values["project_id"]
            if not project_id.startswith("project-") or not UUID_RE.fullmatch(
                project_id.removeprefix("project-")
            ):
                _error("authority_kind.project_id must be project-<UUID>")
            _normalized_key(
                values["authority_transition"],
                "authority_kind.authority_transition",
            )
            if not AUTHORITY_SLUG_RE.fullmatch(values["approval_authority"]):
                _error(
                    "authority_kind.approval_authority must be a normalized slug"
                )
        else:
            values = {
                "kind": kind,
                "workflow_path": _path(
                    _required(data, "workflow_path", "authority_kind"),
                    "authority_kind.workflow_path",
                ),
                "workflow_name": _string(
                    _required(data, "workflow_name", "authority_kind"),
                    "authority_kind.workflow_name",
                ),
                "event": _string(
                    _required(data, "event", "authority_kind"),
                    "authority_kind.event",
                ),
                "run_id": _integer(
                    _required(data, "run_id", "authority_kind"),
                    "authority_kind.run_id",
                    positive=True,
                ),
                "attempt": _integer(
                    _required(data, "attempt", "authority_kind"),
                    "authority_kind.attempt",
                    positive=True,
                ),
                "head_sha": _string(
                    _required(data, "head_sha", "authority_kind"),
                    "authority_kind.head_sha",
                ),
                "ref": _string(
                    _required(data, "ref", "authority_kind"),
                    "authority_kind.ref",
                ),
            }
            if not SHA1_RE.fullmatch(values["head_sha"]):
                _error("authority_kind.head_sha must be lowercase 40-hex")
            if not REF_RE.fullmatch(values["ref"]):
                _error("authority_kind.ref must be a normalized refs/... value")
        return cls(kind=kind, values=values)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.values)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AuthoritySpec":
        return cls.from_dict(value)


@dataclass(frozen=True)
class OperationVariant:
    key: str
    operation_kind: str
    authority_kind: AuthoritySpec
    authority_transitions: tuple[str, ...]
    required_job_contract_keys: tuple[str, ...]
    qualification_job_contract_keys: tuple[str, ...]
    effect_job_contract_keys: tuple[str, ...]
    approval_required: bool
    project_fields: tuple[ProjectField, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], index: int = 0) -> "OperationVariant":
        label = f"operation_variants[{index}]"
        data = _closed(value, OPERATION_VARIANT_KEYS, label)
        _require_keys(data, OPERATION_VARIANT_KEYS, label)
        fields = tuple(
            ProjectField.from_dict(item, f"{label}.project_fields[{field_index}]")
            for field_index, item in enumerate(
                _list(data["project_fields"], f"{label}.project_fields")
            )
        )
        names = [item.name for item in fields]
        if len(set(names)) != len(names):
            _error(f"{label}.project_fields names must be unique")
        reserved_names = {
            "schema_version",
            "variant_key",
            "operation_kind",
            "repository",
            "runtime_source_envelope_digest",
            "operation_jobs_manifest_digest",
        } | KENT_AUTHORITY_KEYS | GITHUB_AUTHORITY_KEYS
        if set(names) & reserved_names:
            _error(f"{label}.project_fields collide with operation fields")
        result = cls(
            key=_normalized_key(_required(data, "key", label), f"{label}.key"),
            operation_kind=_normalized_key(
                _required(data, "operation_kind", label),
                f"{label}.operation_kind",
            ),
            authority_kind=AuthoritySpec.from_dict(
                _required(data, "authority_kind", label)
            ),
            authority_transitions=_sorted_keys(
                _required(data, "authority_transitions", label),
                f"{label}.authority_transitions",
            ),
            required_job_contract_keys=_sorted_keys(
                _required(data, "required_job_contract_keys", label),
                f"{label}.required_job_contract_keys",
            ),
            qualification_job_contract_keys=_sorted_keys(
                _required(data, "qualification_job_contract_keys", label),
                f"{label}.qualification_job_contract_keys",
            ),
            effect_job_contract_keys=_sorted_keys(
                _required(data, "effect_job_contract_keys", label),
                f"{label}.effect_job_contract_keys",
            ),
            approval_required=_bool(
                _required(data, "approval_required", label),
                f"{label}.approval_required",
            ),
            project_fields=fields,
        )
        result.validate()
        return result

    def validate(self) -> None:
        all_keys = (
            self.required_job_contract_keys
            + self.qualification_job_contract_keys
            + self.effect_job_contract_keys
        )
        if len(set(all_keys)) != len(all_keys):
            _error(f"operation variant {self.key!r} repeats a job contract key")
        if self.authority_kind.kind == "kent_transition":
            if not self.authority_transitions:
                _error(f"operation variant {self.key!r} needs authority transitions")
            if (
                self.authority_kind.values["authority_transition"]
                not in self.authority_transitions
            ):
                _error("Kent authority_transition is not in authority_transitions")
        elif self.authority_transitions:
            _error("github_run authority transitions must be empty")
        if self.authority_kind.kind == "github_run" and self.approval_required:
            _error("github_run operation variants cannot require approval")

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "operation_kind": self.operation_kind,
            "authority_kind": self.authority_kind.as_dict(),
            "authority_transitions": list(self.authority_transitions),
            "required_job_contract_keys": list(self.required_job_contract_keys),
            "qualification_job_contract_keys": list(self.qualification_job_contract_keys),
            "effect_job_contract_keys": list(self.effect_job_contract_keys),
            "approval_required": self.approval_required,
            "project_fields": [item.as_dict() for item in self.project_fields],
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        index: int = 0,
    ) -> "OperationVariant":
        return cls.from_dict(value, index)


@dataclass(frozen=True)
class ApprovalMaterialization:
    variant_key: str
    source_path: str
    source_node_key: str
    source_node_kind: str
    authority_transition_parameter: str
    summary_language: str
    summary_sections: tuple[str, ...]
    materialized_before_pending_approval: bool
    commentary_equals_summary: bool
    decision_may_select_approval: bool
    required_fields: tuple[str, ...]
    templates: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], index: int = 0) -> "ApprovalMaterialization":
        label = f"approval_materializations[{index}]"
        data = _closed(value, APPROVAL_KEYS, label)
        _require_keys(data, APPROVAL_KEYS, label)
        sections = tuple(
            _string(item, f"{label}.summary_sections[]")
            for item in _list(data["summary_sections"], f"{label}.summary_sections")
        )
        required_fields = _sorted_strings(
            data["required_fields"],
            f"{label}.required_fields",
        )
        templates = _mapping(_required(data, "templates", label), f"{label}.templates")
        result = cls(
            variant_key=_string(_required(data, "variant_key", label), f"{label}.variant_key"),
            source_path=_path(_required(data, "source_path", label), f"{label}.source_path"),
            source_node_key=_string(
                _required(data, "source_node_key", label),
                f"{label}.source_node_key",
            ),
            source_node_kind=_string(
                _required(data, "source_node_kind", label),
                f"{label}.source_node_kind",
            ),
            authority_transition_parameter=_string(
                _required(data, "authority_transition_parameter", label),
                f"{label}.authority_transition_parameter",
            ),
            summary_language=_string(
                _required(data, "summary_language", label),
                f"{label}.summary_language",
            ),
            summary_sections=sections,
            materialized_before_pending_approval=_bool(
                _required(data, "materialized_before_pending_approval", label),
                f"{label}.materialized_before_pending_approval",
            ),
            commentary_equals_summary=_bool(
                _required(data, "commentary_equals_summary", label),
                f"{label}.commentary_equals_summary",
            ),
            decision_may_select_approval=_bool(
                _required(data, "decision_may_select_approval", label),
                f"{label}.decision_may_select_approval",
            ),
            required_fields=required_fields,
            templates=templates,
        )
        return result

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        index: int = 0,
    ) -> "ApprovalMaterialization":
        return cls.from_dict(value, index)

    def validate(self, variant: OperationVariant) -> None:
        if self.variant_key != variant.key:
            _error("approval materialization references an unknown variant")
        if self.source_node_kind != "script":
            _error("approval materialization source_node_kind must be script")
        if self.authority_transition_parameter != "authority_transition":
            _error("approval authority_transition_parameter is unsupported")
        if self.summary_language != "ru":
            _error("approval summary_language must be ru")
        if self.summary_sections != SUMMARY_SECTIONS:
            _error("approval summary_sections must use the exact Russian sections")
        if (
            not self.materialized_before_pending_approval
            or not self.commentary_equals_summary
            or self.decision_may_select_approval
        ):
            _error("approval materialization safety booleans are invalid")
        if not variant.approval_required or variant.authority_kind.kind != "kent_transition":
            _error("only approval-required kent_transition variants may materialize approval")
        allowed = set(variant.authority_transitions)
        if set(self.templates) != allowed:
            _error("approval templates must cover exactly authority transitions")
        fields = {item.name for item in variant.project_fields if item.approval_renderable}
        if not set(self.required_fields) <= fields:
            _error("approval required_fields must be approval-renderable project fields")
        for transition, templates in self.templates.items():
            references = _validate_templates(
                templates,
                fields | {"operation_digest"},
            )
            if references & fields != set(self.required_fields):
                _error(
                    "approval required_fields must exactly match project placeholders "
                    f"for transition {transition!r}"
                )

    def as_dict(self) -> dict[str, Any]:
        return {
            "variant_key": self.variant_key,
            "source_path": self.source_path,
            "source_node_key": self.source_node_key,
            "source_node_kind": self.source_node_kind,
            "authority_transition_parameter": self.authority_transition_parameter,
            "summary_language": self.summary_language,
            "summary_sections": list(self.summary_sections),
            "materialized_before_pending_approval": self.materialized_before_pending_approval,
            "commentary_equals_summary": self.commentary_equals_summary,
            "decision_may_select_approval": self.decision_may_select_approval,
            "required_fields": list(self.required_fields),
            "templates": self.templates,
        }


def _template_references(
    text: str,
    allowed_keys: set[str],
    label: str = "approval template",
) -> set[str]:
    references: set[str] = set()
    index = 0
    while index < len(text):
        character = text[index]
        if character == "{":
            if not text.startswith("{{", index):
                _error(f"{label} contains a malformed placeholder")
            end = text.find("}}", index + 2)
            if end < 0:
                _error(f"{label} contains an unmatched placeholder")
            expression = text[index + 2 : end].strip()
            if not IDENTIFIER_RE.fullmatch(expression):
                _error("approval templates allow only bare field identifiers")
            if expression not in allowed_keys:
                _error(f"approval template uses unknown field {expression!r}")
            references.add(expression)
            index = end + 2
            continue
        if character == "}":
            _error(f"{label} contains a malformed placeholder")
        index += 1
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        _error("approval templates must be single-line and control-free")
    return references


def _validate_templates(value: Any, allowed_keys: set[str]) -> set[str]:
    if not isinstance(value, Mapping) or set(value) != set(SUMMARY_SECTIONS):
        _error("approval template sections must be exact")
    references: set[str] = set()
    for section in SUMMARY_SECTIONS:
        text = _string(value[section], "approval template", nonempty=False)
        references.update(_template_references(text, allowed_keys))
    return references


@dataclass(frozen=True)
class ReleaseSpec:
    schema_version: int
    spec_kind: str
    topology_kind: str
    adoption_mode: str
    project_name: str
    repository: str
    runtime_attested: bool
    workflow_source_intent: WorkflowSourceIntent
    source_manifest: SourceManifestReference
    required_jobs_v1: JobContractTable
    qualification_jobs_v1: JobContractTable
    effect_jobs_v1: JobContractTable
    operation_variants: tuple[OperationVariant, ...]
    approval_materializations: tuple[ApprovalMaterialization, ...] = field(
        default_factory=tuple
    )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        profile: Any | None = None,
    ) -> "ReleaseSpec":
        data = _closed(value, SPEC_ROOTS, "release spec")
        missing = sorted(REQUIRED_SPEC_ROOTS - set(data))
        if missing:
            _error(f"release spec is missing roots: {missing}")
        schema_version = _integer(
            _required(data, "schema_version", "release spec"),
            "release spec.schema_version",
        )
        if schema_version != 1:
            _error("release spec schema_version must be 1")
        variants = tuple(
            OperationVariant.from_dict(item, index)
            for index, item in enumerate(
                _list(data["operation_variants"], "operation_variants")
            )
        )
        if not variants:
            _error("operation_variants must be non-empty")
        variant_keys = [item.key for item in variants]
        if len(set(variant_keys)) != len(variant_keys):
            _error("operation_variants keys must be unique")
        materializations = tuple(
            ApprovalMaterialization.from_dict(item, index)
            for index, item in enumerate(
                _list(data.get("approval_materializations", []), "approval_materializations")
            )
        )
        result = cls(
            schema_version=schema_version,
            spec_kind=_string(
                _required(data, "spec_kind", "release spec"),
                "release spec.spec_kind",
            ),
            topology_kind=_string(
                _required(data, "topology_kind", "release spec"),
                "release spec.topology_kind",
            ),
            adoption_mode=_string(
                _required(data, "adoption_mode", "release spec"),
                "release spec.adoption_mode",
            ),
            project_name=_string(
                _required(data, "project_name", "release spec"),
                "release spec.project_name",
            ),
            repository=_repository(
                _required(data, "repository", "release spec"),
                "release spec.repository",
            ),
            runtime_attested=_bool(
                _required(data, "runtime_attested", "release spec"),
                "release spec.runtime_attested",
            ),
            workflow_source_intent=WorkflowSourceIntent.from_dict(
                _required(data, "workflow_source_intent", "release spec")
            ),
            source_manifest=SourceManifestReference.from_dict(
                _required(data, "source_manifest", "release spec")
            ),
            required_jobs_v1=JobContractTable.from_dict(
                _required(data, "required_jobs_v1", "release spec"),
                "required",
            ),
            qualification_jobs_v1=JobContractTable.from_dict(
                _required(data, "qualification_jobs_v1", "release spec"),
                "qualification",
            ),
            effect_jobs_v1=JobContractTable.from_dict(
                _required(data, "effect_jobs_v1", "release spec"),
                "effect",
            ),
            operation_variants=variants,
            approval_materializations=materializations,
        )
        result.validate(profile=profile)
        return result

    @classmethod
    def from_toml(
        cls,
        contents: str,
        *,
        profile: Any | None = None,
    ) -> "ReleaseSpec":
        try:
            value = tomllib.loads(contents)
        except tomllib.TOMLDecodeError as error:
            raise ReleaseSpecError(f"cannot parse release spec: {error}") from error
        return cls.from_dict(value, profile=profile)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        profile: Any | None = None,
    ) -> "ReleaseSpec":
        return cls.from_dict(value, profile=profile)

    @classmethod
    def load(
        cls,
        contents: str,
        *,
        profile: Any | None = None,
    ) -> "ReleaseSpec":
        return cls.from_toml(contents, profile=profile)

    @classmethod
    def parse(cls, contents: str, *, profile: Any | None = None) -> "ReleaseSpec":
        return cls.from_toml(contents, profile=profile)

    def validate(self, *, profile: Any | None = None) -> None:
        if self.spec_kind != "release":
            _error("release spec.spec_kind must be release")
        if self.runtime_attested:
            _error("tracked release spec.runtime_attested must be false")
        if self.adoption_mode not in {"managed-in-place", "metadata-only"}:
            _error("release spec.adoption_mode is unsupported")
        self.workflow_source_intent.validate(adoption_mode=self.adoption_mode)
        self.source_manifest.validate()
        tables = {
            "required": self.required_jobs_v1,
            "qualification": self.qualification_jobs_v1,
            "effect": self.effect_jobs_v1,
        }
        global_keys: dict[str, str] = {}
        global_identities: dict[str, str] = {}
        for set_kind, table in tables.items():
            for row in table.jobs:
                key = row["contract_key"]
                previous = global_keys.get(key)
                if previous is not None:
                    _error(
                        f"contract_key {key!r} is duplicated in "
                        f"{previous} and {set_kind}"
                    )
                global_keys[key] = set_kind
                identity = canonical_json(
                    (
                        row["workflow_path"],
                        row["event_selector"],
                        row["job_key"],
                        row["matrix"],
                    )
                )
                previous_identity = global_identities.get(identity)
                if previous_identity is not None:
                    _error(
                        "normalized job identity is present in "
                        f"{previous_identity} and {set_kind}"
                    )
                global_identities[identity] = set_kind
        referenced_qualification = False
        for variant in self.operation_variants:
            for set_kind, keys in (
                ("required", variant.required_job_contract_keys),
                ("qualification", variant.qualification_job_contract_keys),
                ("effect", variant.effect_job_contract_keys),
            ):
                for key in keys:
                    actual_set = global_keys.get(key)
                    if actual_set != set_kind:
                        _error(
                            f"variant {variant.key!r} references {key!r} "
                            f"outside the {set_kind} table"
                        )
                if set_kind == "qualification" and keys:
                    referenced_qualification = True
        if bool(self.qualification_jobs_v1.jobs) != referenced_qualification:
            _error(
                "qualification_jobs_v1 cardinality does not match operation references"
            )
        if profile is not None:
            profile_schema = getattr(profile, "schema_version", None)
            if profile_schema != 4:
                _error("release spec requires a schema-4 ProjectProfile")
            if getattr(profile, "project_name", None) != self.project_name:
                _error("release spec.project_name does not match ProjectProfile")
            release = getattr(profile, "release", None)
            if release is None:
                _error("schema-4 ProjectProfile has no release profile")
            if release.topology_kind != self.topology_kind:
                _error("release spec.topology_kind does not match ProjectProfile.release")
            if release.adoption_mode != self.adoption_mode:
                _error("release spec.adoption_mode does not match ProjectProfile.release")
        materialization_by_variant = {}
        for item in self.approval_materializations:
            if item.variant_key in materialization_by_variant:
                _error("approval_materializations must have unique variant keys")
            materialization_by_variant[item.variant_key] = item
            variant = next(
                (candidate for candidate in self.operation_variants if candidate.key == item.variant_key),
                None,
            )
            if variant is None:
                _error("approval materialization references an unknown variant")
            item.validate(variant)
        for variant in self.operation_variants:
            has_materialization = variant.key in materialization_by_variant
            if variant.approval_required != has_materialization:
                _error(
                    f"variant {variant.key!r} has incorrect approval materialization cardinality"
                )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "spec_kind": self.spec_kind,
            "topology_kind": self.topology_kind,
            "adoption_mode": self.adoption_mode,
            "project_name": self.project_name,
            "repository": self.repository,
            "runtime_attested": self.runtime_attested,
            "workflow_source_intent": self.workflow_source_intent.as_dict(),
            "source_manifest": self.source_manifest.as_dict(),
            "required_jobs_v1": self.required_jobs_v1.as_dict(),
            "qualification_jobs_v1": self.qualification_jobs_v1.as_dict(),
            "effect_jobs_v1": self.effect_jobs_v1.as_dict(),
            "operation_variants": [item.as_dict() for item in self.operation_variants],
            "approval_materializations": [
                item.as_dict() for item in self.approval_materializations
            ],
        }


def _coerce_workflows(
    value: Any,
) -> tuple[NormalizedGitHubWorkflowSourceV1, ...]:
    if isinstance(value, NormalizedGitHubWorkflowSourceV1):
        workflows = (value,)
    elif isinstance(value, (list, tuple)):
        if not value or not all(
            isinstance(item, NormalizedGitHubWorkflowSourceV1)
            for item in value
        ):
            _error("workflow sources must contain normalized workflow DTOs")
        workflows = tuple(value)
    else:
        _error("workflow source must be normalized workflow DTOs")
    paths = [workflow.workflow_path for workflow in workflows]
    if len(set(paths)) != len(paths):
        _error("workflow source paths must be unique")
    return workflows


def _event_matches(selector: Any, event: Mapping[str, Any]) -> bool:
    if not isinstance(selector, Mapping):
        return False
    normalized = _normalize_event(selector)
    return normalized == dict(event)


def _matrix_matches(expected: Any, actual: Any) -> bool:
    return _canonical_value(expected) == _canonical_value(actual)


def _find_contract_job(
    workflows: Sequence[NormalizedGitHubWorkflowSourceV1],
    row: Mapping[str, Any],
) -> tuple[
    NormalizedGitHubWorkflowSourceV1,
    Any,
    dict[str, Any],
    NormalizedGitHubJobV1,
]:
    path = _path(row["workflow_path"], "contract.workflow_path")
    candidates = [workflow for workflow in workflows if workflow.workflow_path == path]
    if len(candidates) != 1:
        _error(f"contract workflow path {path!r} does not select one workflow")
    workflow = candidates[0]
    selector = row["event_selector"]
    canonical_selector = _normalize_event(selector)
    matching_events = [event for event in workflow.events if _event_matches(selector, event)]
    if len(matching_events) != 1:
        _error(f"contract {row['contract_key']!r} does not select exactly one event")
    matches = [
        job
        for job in workflow.jobs
        if job.job_key == row["job_key"] and _matrix_matches(row["matrix"], job.matrix)
    ]
    if len(matches) != 1:
        _error(f"contract {row['contract_key']!r} does not select exactly one job")
    return workflow, canonical_selector, matching_events[0], matches[0]


def _contract_steps(row: Mapping[str, Any], job: NormalizedGitHubJobV1) -> tuple[dict[str, Any], ...]:
    if "steps" not in row:
        return tuple(
            {"step_index": index, "validation_required": False}
            for index in range(len(job.steps))
        )
    declared = _list(row["steps"], f"contract {row['contract_key']}.steps")
    if len(declared) != len(job.steps):
        _error(f"contract {row['contract_key']!r} has step cardinality drift")
    overlays = []
    for index, step in enumerate(declared):
        data = _mapping(step, f"contract {row['contract_key']}.steps[{index}]")
        data = _closed(
            data,
            STEP_KEYS | {"validation_required"},
            f"contract {row['contract_key']}.steps[{index}]",
        )
        if "validation_required" not in data:
            _error(f"contract {row['contract_key']!r} is missing validation_required")
        overlays.append(
            {"step_index": index, "validation_required": _bool(
                data["validation_required"],
                f"contract {row['contract_key']}.steps[{index}].validation_required",
            )}
        )
    return tuple(overlays)


def _compare_contract_job(row: Mapping[str, Any], job: NormalizedGitHubJobV1) -> None:
    source = job.as_dict()
    declared = {key: row[key] for key in JOB_KEYS}
    declared["steps"] = [
        {key: step[key] for key in STEP_KEYS}
        for step in row["steps"]
    ]
    if _canonical_value(declared) != _canonical_value(source):
        _error(f"contract {row['contract_key']!r} has normalized source drift")


@dataclass(frozen=True)
class ValidatedJobBinding:
    set_kind: str
    contract_key: str
    workflow_path: str
    event_selector: Any
    workflow: dict[str, Any]
    event: dict[str, Any]
    job_key: str
    matrix: Any
    job: dict[str, Any]
    policy: dict[str, Any]
    step_overlays: tuple[dict[str, Any], ...]
    _proof: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._proof is not _VALIDATION_TOKEN:
            _error("validated job bindings require validator provenance")

    @property
    def stable_identity(self) -> tuple[Any, ...]:
        return (
            self.set_kind,
            self.workflow_path,
            canonical_json(self.event_selector),
            self.job_key,
            canonical_json(self.matrix),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "set_kind": self.set_kind,
            "contract_key": self.contract_key,
            "workflow_path": self.workflow_path,
            "event_selector": self.event_selector,
            "workflow": self.workflow,
            "event": self.event,
            "job_key": self.job_key,
            "matrix": self.matrix,
            "job": self.job,
            "policy": self.policy,
            "step_contract": list(self.step_overlays),
        }


def _validate_job_policy(
    set_kind: str,
    row: Mapping[str, Any],
    job: NormalizedGitHubJobV1,
    overlays: Sequence[Mapping[str, Any]],
    event: Mapping[str, Any],
) -> None:
    if job.continue_on_error:
        _error(f"{set_kind} job {job.job_key!r} may not continue on error")
    if job.secret_refs:
        if set_kind != "effect":
            _error(f"{set_kind} job {job.job_key!r} may not use secrets")
    if job.container or job.services or job.github_environment:
        _error(f"{set_kind} job {job.job_key!r} has forbidden runtime fixtures")
    if set_kind in {"required", "qualification"} and job.checkout_persist_credentials:
        _error(f"{set_kind} job {job.job_key!r} persists checkout credentials")
    if set_kind in {"required", "qualification"} and any(
        value not in {"read", "none"}
        for value in job.effective_permissions.values()
    ):
        _error(f"{set_kind} job {job.job_key!r} has non-read permission")
    if not job.runner_environment_asserted:
        _error(f"{set_kind} job {job.job_key!r} does not assert its runner environment")
    if set_kind in {"required", "qualification"} and "self-hosted" in job.runs_on.lower():
        _error(f"{set_kind} job {job.job_key!r} must use a GitHub-hosted runner")
    if any(step.continue_on_error for step in job.steps):
        _error(f"{set_kind} job {job.job_key!r} contains a failure-masking step")
    runner_trust = row["runner_trust"]
    trust_class = _runner_trust_class(
        runner_trust,
        f"{set_kind} job {job.job_key!r}.runner_trust",
    )
    self_hosted = "self-hosted" in job.runs_on.lower()
    if trust_class == "github-hosted" and self_hosted:
        _error(f"{set_kind} job {job.job_key!r} has a false runner trust assertion")
    if set_kind in {"required", "qualification"} and trust_class != "github-hosted":
        _error(f"{set_kind} job {job.job_key!r} must use GitHub-hosted ephemeral trust")
    if (
        set_kind in {"required", "qualification"}
        and runner_trust.endswith("-effect")
    ):
        _error(f"{set_kind} job {job.job_key!r} may not use effect runner trust")
    skip_policy = row["skip_policy"]
    if set_kind == "required" and skip_policy != "never":
        _error("required jobs must use skip_policy=never")
    if set_kind in {"qualification", "effect"} and skip_policy == "never":
        _error(f"{set_kind} jobs must use explicit gating")
    if skip_policy == "condition-gated" and not job.condition:
        _error(f"{set_kind} jobs with condition-gated skip policy need a condition")
    if set_kind == "required":
        if not row["branch_protection_required"]:
            _error("required jobs must require branch protection")
        if event["name"] not in {"pull_request", "merge_group"}:
            _error("required branch protection jobs need pull_request or merge_group")
        if job.needs:
            _error("required jobs may not have needs")
        if job.condition:
            _error("required jobs may not be conditional")
        if not any(item["validation_required"] for item in overlays):
            _error("required jobs need a validation step")
        for index, overlay in enumerate(overlays):
            if overlay["validation_required"]:
                step = job.steps[index]
                if step.condition or step.continue_on_error:
                    _error("validation steps must be unconditional and non-failing")
        effects = set(row["allowed_effects"])
        if not effects <= NON_PRODUCTION_EFFECTS:
            _error("required job has a production effect")
        if row["credential_profile"] not in REQUIRED_CREDENTIAL_PROFILES:
            _error("required jobs must use a credential-safe profile")
        if not row["control_plane_fixtures_forbidden"]:
            _error("required jobs must forbid control-plane fixtures")
        if row["credential_scope_is_job_local"]:
            _error("required jobs may not use job-local credentials")
    elif set_kind == "qualification":
        if row["branch_protection_required"]:
            _error("qualification jobs may not require branch protection")
        if row["credential_scope_is_job_local"]:
            _error("qualification jobs must be credential-free")
        if row["credential_profile"] not in QUALIFICATION_CREDENTIAL_PROFILES:
            _error("qualification jobs must be credential-free")
        if not row["control_plane_fixtures_forbidden"]:
            _error("qualification jobs must forbid control-plane fixtures")
        if not set(row["allowed_effects"]) <= NON_PRODUCTION_EFFECTS:
            _error("qualification jobs may not have production effects")
    elif set_kind == "effect":
        if row["branch_protection_required"]:
            _error("effect jobs may not require branch protection")
        if not row["credential_scope_is_job_local"]:
            _error("effect jobs need job-local credentials")
        if not row["control_plane_fixtures_forbidden"]:
            _error("effect jobs must forbid control-plane fixtures")
        if not row["allowed_effects"]:
            _error("effect jobs need a non-empty effect allowlist")
        if row["credential_profile"] in {"", "none", "credential-free"}:
            _error("effect jobs need an explicit credential profile")


def _validate_job_sources(
    source: Any,
    contracts: JobContractTable | Mapping[str, Any],
    set_kind: str,
    *,
    contract_keys: Sequence[str] | None = None,
) -> tuple[ValidatedJobBinding, ...]:
    workflows = _coerce_workflows(source)
    table = (
        contracts
        if isinstance(contracts, JobContractTable)
        else JobContractTable.from_dict(contracts, set_kind)
    )
    selected_keys = set(contract_keys) if contract_keys is not None else None
    if selected_keys is not None:
        unknown = selected_keys - {row["contract_key"] for row in table.jobs}
        if unknown:
            _error(f"{set_kind} contract references are unknown: {sorted(unknown)}")
        rows = [row for row in table.jobs if row["contract_key"] in selected_keys]
    else:
        rows = list(table.jobs)
    bindings = []
    for row in rows:
        workflow, event_selector, event, job = _find_contract_job(workflows, row)
        _compare_contract_job(row, job)
        overlays = _contract_steps(row, job)
        _validate_job_policy(set_kind, row, job, overlays, event)
        bindings.append(
            ValidatedJobBinding(
                set_kind=set_kind,
                contract_key=row["contract_key"],
                workflow_path=workflow.workflow_path,
                event_selector=event_selector,
                workflow=workflow.as_dict(),
                event=event,
                job_key=job.job_key,
                matrix=job.matrix,
                job=job.as_dict(),
                policy={
                    key: _canonical_value(row[key])
                    for key in sorted(POLICY_FIELDS)
                },
                step_overlays=overlays,
                _proof=_VALIDATION_TOKEN,
            )
        )
    identities = [binding.stable_identity for binding in bindings]
    if len(set(identities)) != len(identities):
        _error(f"{set_kind} job contracts overlap the same normalized job")
    return tuple(bindings)


def validate_required_job_sources(
    source: Any,
    contracts: JobContractTable | Mapping[str, Any],
    *,
    contract_keys: Sequence[str] | None = None,
) -> tuple[ValidatedJobBinding, ...]:
    return _validate_job_sources(
        source,
        contracts,
        "required",
        contract_keys=contract_keys,
    )


def validate_qualification_job_sources(
    source: Any,
    contracts: JobContractTable | Mapping[str, Any],
    *,
    contract_keys: Sequence[str] | None = None,
) -> tuple[ValidatedJobBinding, ...]:
    return _validate_job_sources(
        source,
        contracts,
        "qualification",
        contract_keys=contract_keys,
    )


def validate_effect_job_sources(
    source: Any,
    contracts: JobContractTable | Mapping[str, Any],
    *,
    contract_keys: Sequence[str] | None = None,
) -> tuple[ValidatedJobBinding, ...]:
    return _validate_job_sources(
        source,
        contracts,
        "effect",
        contract_keys=contract_keys,
    )


def _validated_jobs_fingerprint(
    variant_key: str,
    required: Sequence[ValidatedJobBinding],
    qualification: Sequence[ValidatedJobBinding],
    effect: Sequence[ValidatedJobBinding],
) -> str:
    def canonical_bindings(
        bindings: Sequence[ValidatedJobBinding],
    ) -> list[dict[str, Any]]:
        return [
            item.as_dict()
            for item in sorted(bindings, key=lambda item: item.stable_identity)
        ]

    return sha256_digest(
        canonical_json_bytes(
            {
                "variant_key": variant_key,
                "required": canonical_bindings(required),
                "qualification": canonical_bindings(qualification),
                "effect": canonical_bindings(effect),
            }
        )
    )


def _ensure_variant_bindings(
    variant: OperationVariant,
    validated: ValidatedOperationJobs,
) -> None:
    actual = {
        "required": {item.contract_key for item in validated.required},
        "qualification": {item.contract_key for item in validated.qualification},
        "effect": {item.contract_key for item in validated.effect},
    }
    expected = {
        "required": set(variant.required_job_contract_keys),
        "qualification": set(variant.qualification_job_contract_keys),
        "effect": set(variant.effect_job_contract_keys),
    }
    if actual != expected:
        _error("validated operation jobs do not match variant contract references")


@dataclass(frozen=True)
class ValidatedOperationJobs:
    variant_key: str
    required: tuple[ValidatedJobBinding, ...]
    qualification: tuple[ValidatedJobBinding, ...]
    effect: tuple[ValidatedJobBinding, ...]
    _proof: object = field(repr=False, compare=False)
    _provenance_digest: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            self._proof is not _VALIDATION_TOKEN
            or self._provenance_digest
            != _validated_jobs_fingerprint(
                self.variant_key,
                self.required,
                self.qualification,
                self.effect,
            )
        ):
            _error("validated operation jobs require validator provenance")

    @property
    def bindings(self) -> tuple[ValidatedJobBinding, ...]:
        return self.required + self.qualification + self.effect

    @property
    def operation_jobs_manifest(self) -> dict[str, Any]:
        return {
            "schema": "operation_jobs_manifest_v1",
            "variant_key": self.variant_key,
            "bindings": [
                binding.as_dict()
                for binding in sorted(self.bindings, key=lambda item: item.stable_identity)
            ],
        }

    @property
    def operation_jobs_manifest_bytes(self) -> bytes:
        return canonical_json_bytes(self.operation_jobs_manifest)

    @property
    def operation_jobs_manifest_digest(self) -> str:
        return sha256_digest(self.operation_jobs_manifest_bytes)

    def as_dict(self) -> dict[str, Any]:
        return self.operation_jobs_manifest


def validate_operation_jobs(
    variant: OperationVariant,
    source: Any,
    *,
    required: JobContractTable | Mapping[str, Any],
    qualification: JobContractTable | Mapping[str, Any],
    effect: JobContractTable | Mapping[str, Any],
) -> ValidatedOperationJobs:
    variant.validate()
    required_bindings = validate_required_job_sources(
        source,
        required,
        contract_keys=variant.required_job_contract_keys,
    )
    qualification_bindings = validate_qualification_job_sources(
        source,
        qualification,
        contract_keys=variant.qualification_job_contract_keys,
    )
    effect_bindings = validate_effect_job_sources(
        source,
        effect,
        contract_keys=variant.effect_job_contract_keys,
    )
    validated = ValidatedOperationJobs(
        variant_key=variant.key,
        required=required_bindings,
        qualification=qualification_bindings,
        effect=effect_bindings,
        _proof=_VALIDATION_TOKEN,
        _provenance_digest=_validated_jobs_fingerprint(
            variant.key,
            required_bindings,
            qualification_bindings,
            effect_bindings,
        ),
    )
    identities = [binding.stable_identity[1:] for binding in validated.bindings]
    if len(set(identities)) != len(identities):
        _error("operation variant job bindings overlap normalized job identities")
    return validated


def _variant_from(value: Any, spec: ReleaseSpec | None = None) -> OperationVariant:
    if isinstance(value, OperationVariant):
        return value
    if spec is not None:
        for variant in spec.operation_variants:
            if variant.key == value:
                return variant
    if isinstance(value, Mapping):
        return OperationVariant.from_dict(value)
    _error("operation variant is required")


def _validate_field_value(value: Any, field_spec: ProjectField, label: str) -> Any:
    if value is None:
        if field_spec.nullable:
            return None
        _error(f"{label} may not be null")
    if field_spec.type == "string":
        return _string(value, label)
    if field_spec.type == "integer":
        return _integer(value, label)
    if field_spec.type == "boolean":
        return _bool(value, label)
    _error(f"{label} has unsupported type")


@dataclass(frozen=True)
class CanonicalizedPublicationOperation:
    operation: dict[str, Any]
    operation_bytes: bytes
    operation_digest: str
    operation_jobs_manifest: dict[str, Any]
    operation_jobs_manifest_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "operation_digest": self.operation_digest,
            "operation_jobs_manifest": self.operation_jobs_manifest,
            "operation_jobs_manifest_digest": self.operation_jobs_manifest_digest,
        }


def canonicalize_publication_operation(
    operation: Mapping[str, Any],
    variant: OperationVariant | Mapping[str, Any] | str,
    validated_jobs: ValidatedOperationJobs,
    *,
    spec: ReleaseSpec | None = None,
) -> CanonicalizedPublicationOperation:
    if spec is None:
        _error("canonicalization requires ReleaseSpec")
    selected = _variant_from(variant, spec)
    spec_variant = next(
        (item for item in spec.operation_variants if item.key == selected.key),
        None,
    )
    if spec_variant is None or spec_variant.as_dict() != selected.as_dict():
        _error("operation variant does not match ReleaseSpec")
    if not isinstance(validated_jobs, ValidatedOperationJobs):
        _error("canonicalization requires ValidatedOperationJobs")
    if validated_jobs._proof is not _VALIDATION_TOKEN:
        _error("canonicalization requires validator provenance")
    if validated_jobs._provenance_digest != _validated_jobs_fingerprint(
        validated_jobs.variant_key,
        validated_jobs.required,
        validated_jobs.qualification,
        validated_jobs.effect,
    ):
        _error("validated operation jobs provenance is stale")
    _ensure_variant_bindings(selected, validated_jobs)
    if validated_jobs.variant_key != selected.key:
        _error("validated operation jobs use a different variant")
    data = _mapping(operation, "publication operation")
    allowed = {
        "schema_version",
        "variant_key",
        "operation_kind",
        "repository",
        "runtime_source_envelope_digest",
        "operation_jobs_manifest_digest",
        "authority",
        "project_fields",
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        _error(f"publication operation has unknown fields: {unknown}")
    required = {
        "schema_version",
        "variant_key",
        "operation_kind",
        "repository",
        "runtime_source_envelope_digest",
        "operation_jobs_manifest_digest",
    }
    missing = sorted(required - set(data))
    if missing:
        _error(f"publication operation is missing fields: {missing}")
    if "authority" not in data:
        _error("publication operation is missing authority")
    if _integer(data["schema_version"], "operation.schema_version") != 1:
        _error("operation.schema_version must be 1")
    if data["variant_key"] != selected.key:
        _error("operation.variant_key does not match variant")
    if data["operation_kind"] != selected.operation_kind:
        _error("operation.operation_kind does not match variant")
    repository = _repository(data["repository"], "operation.repository")
    if repository != spec.repository:
        _error("operation.repository does not match ReleaseSpec.repository")
    runtime_digest = _string(
        data["runtime_source_envelope_digest"],
        "operation.runtime_source_envelope_digest",
    )
    if not SHA256_RE.fullmatch(runtime_digest):
        _error("operation.runtime_source_envelope_digest must be lowercase 64-hex")
    expected_jobs_digest = validated_jobs.operation_jobs_manifest_digest
    if data["operation_jobs_manifest_digest"] != expected_jobs_digest:
        _error("operation job manifest digest does not match validated jobs")
    authority = AuthoritySpec.from_dict(data["authority"])
    if authority.kind != selected.authority_kind.kind:
        _error("operation authority kind does not match variant")
    if authority.as_dict() != selected.authority_kind.as_dict():
        _error("operation authority does not match the variant authority")
    project_values = data["project_fields"]
    if not isinstance(project_values, Mapping):
        _error("operation.project_fields must be a table")
    expected_fields = {field_spec.name for field_spec in selected.project_fields}
    if set(project_values) != expected_fields:
        _error("operation project fields are missing or extra")
    canonical_project = {}
    for field_spec in selected.project_fields:
        value = project_values[field_spec.name]
        canonical_project[field_spec.name] = _validate_field_value(
            value,
            field_spec,
            f"operation.project_fields.{field_spec.name}",
        )
    canonical = {
        "schema_version": 1,
        "variant_key": selected.key,
        "operation_kind": selected.operation_kind,
        "repository": repository,
        "runtime_source_envelope_digest": runtime_digest,
        "operation_jobs_manifest_digest": expected_jobs_digest,
        "authority": authority.as_dict(),
        "project_fields": canonical_project,
    }
    operation_bytes = canonical_json_bytes(canonical)
    return CanonicalizedPublicationOperation(
        operation=canonical,
        operation_bytes=operation_bytes,
        operation_digest=sha256_digest(operation_bytes),
        operation_jobs_manifest=validated_jobs.operation_jobs_manifest,
        operation_jobs_manifest_digest=expected_jobs_digest,
    )


def _template_values(operation: Mapping[str, Any], operation_digest: str) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(operation_digest):
        _error("operation_digest must be lowercase 64-hex")
    values = dict(operation)
    authority = operation.get("authority", {})
    if isinstance(authority, Mapping):
        values.update(authority)
    fields = operation.get("project_fields", {})
    if isinstance(fields, Mapping):
        values.update(fields)
    values["operation_digest"] = operation_digest
    return values


def _render_template(template: str, values: Mapping[str, Any]) -> str:
    _template_references(template, set(values))

    def replace(match: re.Match[str]) -> str:
        expression = match.group(1).strip()
        if not IDENTIFIER_RE.fullmatch(expression):
            _error("approval template allows only bare field identifiers")
        if expression not in values:
            _error(f"approval template field {expression!r} is missing")
        value = values[expression]
        if value is None:
            _error(f"approval template field {expression!r} is null")
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        elif isinstance(value, str):
            rendered = value
        else:
            _error(f"approval template field {expression!r} is not renderable")
        if "\r" in rendered or "\n" in rendered or any(
            ord(character) < 32 or ord(character) == 127 for character in rendered
        ):
            _error(f"approval template field {expression!r} contains control characters")
        return rendered

    return PLACEHOLDER_RE.sub(replace, template)


def render_approval_summary(
    materialization: ApprovalMaterialization | Mapping[str, Any],
    operation: Mapping[str, Any],
    operation_digest: str,
    *,
    authority_transition: str | None = None,
) -> str:
    materialized = (
        materialization
        if isinstance(materialization, ApprovalMaterialization)
        else ApprovalMaterialization.from_dict(materialization)
    )
    if materialized.source_node_kind != "script":
        _error("approval source_node_kind must be script")
    if materialized.summary_language != "ru":
        _error("approval summary_language must be ru")
    if materialized.summary_sections != SUMMARY_SECTIONS:
        _error("approval summary_sections must be exact")
    if (
        not materialized.materialized_before_pending_approval
        or not materialized.commentary_equals_summary
        or materialized.decision_may_select_approval
    ):
        _error("approval materialization safety booleans are invalid")
    values = _template_values(operation, operation_digest)
    transition_value = (
        authority_transition
        if authority_transition is not None
        else values.get(materialized.authority_transition_parameter)
    )
    transition = _string(transition_value, "authority transition")
    if transition not in materialized.templates:
        _error("approval operation transition has no template")
    templates = materialized.templates[transition]
    references = _validate_templates(templates, set(values))
    if not set(materialized.required_fields) <= references:
        _error("approval required_fields must appear in every transition template")
    ordered = [templates[section] for section in SUMMARY_SECTIONS]
    lines = [_render_template(_string(template, "approval template"), values) for template in ordered]
    if len(lines) != 3:
        _error("approval summary must contain exactly three lines")
    if any("\r" in line or "\n" in line for line in lines):
        _error("approval summary must contain exactly three physical lines")
    return "\n".join(lines)


def validate_approval_materialization(
    materialization: ApprovalMaterialization | Mapping[str, Any],
    operation: Mapping[str, Any],
    operation_digest: str,
    *,
    authority_transition: str | None = None,
    source_text: Mapping[str, Any] | None = None,
    expected_summary: str | None = None,
    expected_commentary: str | None = None,
) -> str:
    materialized = (
        materialization
        if isinstance(materialization, ApprovalMaterialization)
        else ApprovalMaterialization.from_dict(materialization)
    )
    summary = render_approval_summary(
        materialized,
        operation,
        operation_digest,
        authority_transition=authority_transition,
    )
    values = _template_values(operation, operation_digest)
    transition_value = (
        authority_transition
        if authority_transition is not None
        else values.get(materialized.authority_transition_parameter)
    )
    transition = _string(transition_value, "authority transition")
    if operation.get("variant_key") != materialized.variant_key:
        _error("approval operation variant does not match materialization")
    for field_name in materialized.required_fields:
        if field_name not in values or values[field_name] is None:
            _error(f"approval required field {field_name!r} is missing or null")
    if expected_summary is None:
        _error("approval validation requires expected_summary")
    if summary != expected_summary:
        _error("approval summary does not match rendered summary")
    if materialized.commentary_equals_summary:
        if expected_commentary is None or expected_commentary != summary:
            _error("approval commentary must equal summary")
    if source_text is None:
        _error("approval validation requires a source materialization mapping")
    expected_source = {
        "source_path": materialized.source_path,
        "source_node_key": materialized.source_node_key,
        "source_node_kind": materialized.source_node_kind,
        "variant_key": materialized.variant_key,
        "authority_transition": transition,
        "operation_digest": operation_digest,
        "summary": summary,
        "commentary": summary,
    }
    actual_source = _closed(
        source_text,
        APPROVAL_SOURCE_KEYS,
        "approval source materialization",
    )
    _require_keys(
        actual_source,
        APPROVAL_SOURCE_KEYS,
        "approval source materialization",
    )
    if actual_source != expected_source:
        _error("approval source materialization does not match exactly")
    return summary


@dataclass(frozen=True)
class SelectedReleaseArtifacts:
    spec_raw_blob_sha256: str
    source_manifest_raw_blob_sha256: str
    snapshot_raw_blob_sha256: str
    builder_raw_blob_sha256: str | None = None
    derived_paths: tuple[str, ...] = ()
    additional_paths: tuple[str, ...] = ()
    additional_trees: tuple[str, ...] = ()
    declared_prompt_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "spec_raw_blob_sha256",
            "source_manifest_raw_blob_sha256",
            "snapshot_raw_blob_sha256",
        ):
            value = _string(getattr(self, name), f"artifact.{name}")
            if not SHA256_RE.fullmatch(value):
                _error(f"artifact.{name} must be lowercase 64-hex")
        if self.builder_raw_blob_sha256 is not None:
            builder_digest = _string(
                self.builder_raw_blob_sha256,
                "artifact.builder_raw_blob_sha256",
            )
            if not SHA256_RE.fullmatch(builder_digest):
                _error("artifact.builder_raw_blob_sha256 must be lowercase 64-hex")
        for name in (
            "derived_paths",
            "additional_paths",
            "additional_trees",
            "declared_prompt_references",
        ):
            values = tuple(_path(item, f"artifact.{name}[]") for item in getattr(self, name))
            if values != tuple(sorted(set(values))):
                _error(f"artifact.{name} must be sorted and unique")
            object.__setattr__(self, name, values)
        trees = set(self.additional_trees)
        files = set(self.derived_paths) | set(self.additional_paths)
        tree_list = sorted(trees)
        for index, tree in enumerate(tree_list):
            for other in tree_list[index + 1 :]:
                if other == tree or other.startswith(tree + "/"):
                    _error("artifact trees may not overlap")
        for tree in trees:
            if any(
                path == tree
                or path.startswith(tree + "/")
                or tree.startswith(path + "/")
                for path in files
            ):
                _error("artifact file/tree roots may not overlap")
        if not set(self.declared_prompt_references) <= (
            files
            | {
                reference
                for reference in self.declared_prompt_references
                for tree in trees
                if reference == tree or reference.startswith(tree + "/")
            }
        ):
            _error("artifact prompt references are not covered")


def render_release_preview(
    spec: ReleaseSpec,
    validated_operation_jobs: ValidatedOperationJobs | Mapping[str, ValidatedOperationJobs],
    artifacts: SelectedReleaseArtifacts,
    *,
    job_sources_validated: bool = False,
) -> dict[str, Any]:
    if not isinstance(spec, ReleaseSpec):
        _error("render_release_preview requires ReleaseSpec")
    spec.validate()
    if not isinstance(artifacts, SelectedReleaseArtifacts):
        _error("render_release_preview requires SelectedReleaseArtifacts")
    if not isinstance(job_sources_validated, bool):
        _error("job_sources_validated must be a boolean")
    if isinstance(validated_operation_jobs, Mapping):
        jobs_by_variant = dict(validated_operation_jobs)
    elif isinstance(validated_operation_jobs, ValidatedOperationJobs):
        jobs_by_variant = {validated_operation_jobs.variant_key: validated_operation_jobs}
    else:
        _error("render_release_preview requires validated operation jobs")
    if any(
        not isinstance(key, str)
        or not isinstance(value, ValidatedOperationJobs)
        or value._proof is not _VALIDATION_TOKEN
        or value.variant_key != key
        for key, value in jobs_by_variant.items()
    ):
        _error("render_release_preview received forged or mismatched job bindings")
    known_variants = {variant.key for variant in spec.operation_variants}
    if set(jobs_by_variant) - known_variants:
        _error("render_release_preview received an unknown operation variant")
    if job_sources_validated and set(jobs_by_variant) != known_variants:
        _error("job_sources_validated=true requires every operation variant binding")
    operations = []
    normalized_identities = []
    for variant in spec.operation_variants:
        jobs = jobs_by_variant.get(variant.key)
        if jobs is not None:
            if jobs._provenance_digest != _validated_jobs_fingerprint(
                jobs.variant_key,
                jobs.required,
                jobs.qualification,
                jobs.effect,
            ):
                _error("render_release_preview received stale job provenance")
            _ensure_variant_bindings(variant, jobs)
            normalized_identities.extend(
                [
                    list(binding.stable_identity)
                    for binding in jobs.bindings
                ]
            )
        operations.append(variant.as_dict())
    source_manifest = spec.source_manifest
    preview = {
        "source_contract_valid": True,
        "runtime_attested": False,
        "job_sources_validated": job_sources_validated,
        "activation_authorized": False,
        "snapshot_json_valid": True,
        "workflow_source_intent": spec.workflow_source_intent.as_dict(),
        "artifact_digests": {
            key: value
            for key, value in {
                "spec_raw_blob_sha256": artifacts.spec_raw_blob_sha256,
                "source_manifest_raw_blob_sha256": (
                    artifacts.source_manifest_raw_blob_sha256
                ),
                "snapshot_raw_blob_sha256": artifacts.snapshot_raw_blob_sha256,
                "builder_raw_blob_sha256": artifacts.builder_raw_blob_sha256,
            }.items()
            if value is not None
        },
        "source_manifest": {
            "path": source_manifest.path,
            "revision_binding": source_manifest.revision_binding,
            "derived_source_count": len(artifacts.derived_paths),
            "additional_source_count": len(
                artifacts.additional_paths
            )
            + len(artifacts.additional_trees),
            "declared_prompt_reference_count": len(artifacts.declared_prompt_references),
        },
        "normalized_job_identities": sorted(normalized_identities),
        "operation_variants": operations,
        "approval_sections": [
            {
                "variant_key": item.variant_key,
                "sections": list(item.summary_sections),
            }
            for item in spec.approval_materializations
        ],
    }
    return preview


def load_release_spec(
    contents: str,
    *,
    profile: Any | None = None,
) -> ReleaseSpec:
    return ReleaseSpec.from_toml(contents, profile=profile)


def parse_release_spec(
    contents: str,
    *,
    profile: Any | None = None,
) -> ReleaseSpec:
    return load_release_spec(contents, profile=profile)


def parse_source_manifest(
    value: Mapping[str, Any] | str,
) -> ReleaseSourceManifest:
    if isinstance(value, str):
        return ReleaseSourceManifest.from_json(value)
    return ReleaseSourceManifest.from_dict(value)


def operation_jobs_manifest_bytes(validated: ValidatedOperationJobs) -> bytes:
    if not isinstance(validated, ValidatedOperationJobs):
        _error("operation jobs manifest requires ValidatedOperationJobs")
    return validated.operation_jobs_manifest_bytes


def operation_jobs_manifest_digest(validated: ValidatedOperationJobs) -> str:
    if not isinstance(validated, ValidatedOperationJobs):
        _error("operation jobs manifest requires ValidatedOperationJobs")
    return validated.operation_jobs_manifest_digest


__all__ = [
    "ApprovalMaterialization",
    "AuthoritySpec",
    "CanonicalizedPublicationOperation",
    "ExternalRoot",
    "JobContractTable",
    "NormalizedGitHubJobV1",
    "NormalizedGitHubStepV1",
    "NormalizedGitHubWorkflowSourceV1",
    "OperationVariant",
    "ProjectField",
    "ReleaseError",
    "ReleaseSourceManifest",
    "ReleaseSpec",
    "ReleaseSpecError",
    "ReleaseValidationError",
    "SelectedReleaseArtifacts",
    "SourceManifestReference",
    "SourceManifestSpec",
    "ValidatedJobBinding",
    "ValidatedOperationJobs",
    "WorkflowSourceIntent",
    "canonical_json",
    "canonical_json_bytes",
    "canonical_bytes",
    "canonical_sha256",
    "canonicalize_publication_operation",
    "load_release_spec",
    "parse_release_spec",
    "parse_source_manifest",
    "operation_jobs_manifest_bytes",
    "operation_jobs_manifest_digest",
    "render_approval_summary",
    "render_release_preview",
    "sha256_digest",
    "sha256_hex",
    "validate_approval_materialization",
    "validate_effect_job_sources",
    "validate_operation_jobs",
    "validate_qualification_job_sources",
    "validate_required_job_sources",
]
