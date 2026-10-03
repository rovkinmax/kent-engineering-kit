#!/usr/bin/env python3
"""Validate bounded, project-owned evidence for changed UI stories.

This module checks packet structure, workspace containment, and consistency.
It does not authenticate build output or decide whether a story meaningfully
covers a source change; the consuming project's verifier and review own those
checks.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys
from typing import Any


EXPECTED_CONTEXT_SCHEMA = "story-change-expected-context-v1"
EVIDENCE_SCHEMA = "story-change-evidence-v1"
RESULT_SCHEMA = "story-change-evaluation-v1"

MAX_JSON_BYTES = 1_048_576
MAX_JSON_DEPTH = 24
MAX_JSON_NODES = 16_384
MAX_STATES = 128
MAX_STORIES = 128
MAX_CHECKS = 256
MAX_REFERENCES_PER_LIST = 256
MAX_STRING_LENGTH = 2_048
MAX_ID_LENGTH = 128
MAX_IDENTITY_PART_LENGTH = 256
MAX_HASHED_ARTIFACT_BYTES = 16 * 1_048_576

PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
OBJECT_ID_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class StoryEvidenceError(ValueError):
    """Raised for malformed, unsafe, or operationally invalid inputs."""


class Capability(str, Enum):
    CONNECTED = "connected"
    NOT_CONNECTED = "not_connected"


class Status(str, Enum):
    EVIDENCE_READY = "evidence_ready"
    NOT_APPLICABLE = "not_applicable"
    UNSUPPORTED = "unsupported"
    NOT_CONNECTED = "not_connected"
    FAILED = "failed"


@dataclass(frozen=True)
class ExpectedContext:
    """Project-verifier context, supplied separately from the evidence packet."""

    workspace_root: Path
    project_id: str
    capability: Capability
    permit_deferred_not_connected: bool
    baseline: str
    revision: str
    diff_sha256: str

    def __post_init__(self) -> None:
        try:
            root = Path(self.workspace_root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, TypeError) as error:
            raise StoryEvidenceError(
                "expected workspace root is unavailable"
            ) from error
        if not root.is_dir():
            raise StoryEvidenceError("expected workspace root is not a directory")
        object.__setattr__(self, "workspace_root", root)

        project_id = _require_string(self.project_id, "context.project_id")
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise StoryEvidenceError("context.project_id has an invalid format")
        object.__setattr__(self, "project_id", project_id)

        try:
            capability = Capability(self.capability)
        except (TypeError, ValueError) as error:
            raise StoryEvidenceError(
                "context.capability must be connected or not_connected"
            ) from error
        object.__setattr__(self, "capability", capability)

        if not isinstance(self.permit_deferred_not_connected, bool):
            raise StoryEvidenceError(
                "context.permit_deferred_not_connected must be a boolean"
            )
        if (
            capability is Capability.CONNECTED
            and self.permit_deferred_not_connected
        ):
            raise StoryEvidenceError(
                "a connected capability cannot permit not-connected deferral"
            )

        _require_object_id(self.baseline, "context.baseline")
        _require_object_id(self.revision, "context.revision")
        _require_sha256(self.diff_sha256, "context.diff_sha256")

    @classmethod
    def from_dict(
        cls,
        payload: Any,
        *,
        workspace_root: Path,
    ) -> "ExpectedContext":
        context = _closed_object(
            payload,
            {
                "schema",
                "project_id",
                "capability",
                "permit_deferred_not_connected",
                "baseline",
                "revision",
                "diff_sha256",
            },
            "expected context",
        )
        if context["schema"] != EXPECTED_CONTEXT_SCHEMA:
            raise StoryEvidenceError("expected context schema is unsupported")
        return cls(
            workspace_root=workspace_root,
            project_id=context["project_id"],
            capability=context["capability"],
            permit_deferred_not_connected=(
                context["permit_deferred_not_connected"]
            ),
            baseline=context["baseline"],
            revision=context["revision"],
            diff_sha256=context["diff_sha256"],
        )


@dataclass(frozen=True)
class Evaluation:
    status: Status
    reasons: tuple[str, ...]
    artifacts: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": RESULT_SCHEMA,
            "status": self.status.value,
            "reasons": list(self.reasons),
            "artifacts": list(self.artifacts),
        }


@dataclass(frozen=True)
class _State:
    identifier: str
    support: str
    reason: str | None
    story_ids: tuple[str, ...]
    alternative_evidence: tuple[str, ...]


@dataclass(frozen=True)
class _Story:
    identifier: str
    change: str
    source: str
    source_sha256: str
    change_evidence: str
    change_evidence_sha256: str
    identity: tuple[str, str, str]
    check_ids: tuple[str, ...]


@dataclass(frozen=True)
class _Registry:
    artifact: str
    check_id: str
    identities: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class _Check:
    identifier: str
    kind: str
    command: str
    outcome: str
    artifact: str
    baseline: str
    revision: str
    diff_sha256: str
    story_ids: tuple[str, ...]


def _require_string(value: Any, label: str, *, limit: int = MAX_STRING_LENGTH) -> str:
    if not isinstance(value, str):
        raise StoryEvidenceError("{} must be a string".format(label))
    if not value.strip() or len(value) > limit:
        raise StoryEvidenceError("{} is empty or exceeds its size limit".format(label))
    if any(ord(character) < 32 for character in value):
        raise StoryEvidenceError("{} contains control characters".format(label))
    return value


def _require_identifier(value: Any, label: str) -> str:
    text = _require_string(value, label, limit=MAX_ID_LENGTH)
    if not IDENTIFIER_PATTERN.fullmatch(text):
        raise StoryEvidenceError("{} has an invalid identifier format".format(label))
    return text


def _require_choice(value: Any, label: str, choices: set[str]) -> str:
    text = _require_string(value, label, limit=MAX_ID_LENGTH)
    if text not in choices:
        raise StoryEvidenceError("{} is not a recognized status".format(label))
    return text


def _require_object_id(value: Any, label: str) -> str:
    text = _require_string(value, label, limit=64)
    if not OBJECT_ID_PATTERN.fullmatch(text):
        raise StoryEvidenceError("{} must be a 40- or 64-digit object ID".format(label))
    return text


def _require_sha256(value: Any, label: str) -> str:
    text = _require_string(value, label, limit=64)
    if not SHA256_PATTERN.fullmatch(text):
        raise StoryEvidenceError("{} must be a lowercase SHA-256 digest".format(label))
    return text


def _optional_reason(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, label)


def _closed_object(
    value: Any,
    keys: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StoryEvidenceError("{} must be an object".format(label))
    if any(not isinstance(key, str) for key in value):
        raise StoryEvidenceError("{} contains a non-string key".format(label))
    actual = set(value)
    missing = keys - actual
    extra = actual - keys
    if missing or extra:
        details = []
        if missing:
            details.append("missing fields: {}".format(", ".join(sorted(missing))))
        if extra:
            details.append("unknown fields: {}".format(", ".join(sorted(extra))))
        raise StoryEvidenceError(
            "{} is not a closed object ({})".format(label, "; ".join(details))
        )
    return value


def _bounded_list(
    value: Any,
    label: str,
    *,
    maximum: int,
) -> list[Any]:
    if not isinstance(value, list):
        raise StoryEvidenceError("{} must be an array".format(label))
    if len(value) > maximum:
        raise StoryEvidenceError("{} exceeds its item limit".format(label))
    return value


def _string_list(
    value: Any,
    label: str,
    *,
    maximum: int = MAX_REFERENCES_PER_LIST,
    identifiers: bool = False,
) -> tuple[str, ...]:
    raw = _bounded_list(value, label, maximum=maximum)
    parser = _require_identifier if identifiers else _require_string
    return tuple(
        parser(item, "{} item".format(label))
        for item in raw
    )


def _safe_reference(
    root: Path,
    value: Any,
    label: str,
) -> tuple[str, Path]:
    text = _require_string(value, label)
    if "\\" in text:
        raise StoryEvidenceError("{} must use workspace-relative POSIX paths".format(label))
    posix_path = PurePosixPath(text)
    windows_path = PureWindowsPath(text)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or not posix_path.parts
        or any(part in {"", ".", ".."} for part in posix_path.parts)
    ):
        raise StoryEvidenceError("{} is not a safe relative path".format(label))

    try:
        resolved = root.joinpath(*posix_path.parts).resolve(strict=False)
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise StoryEvidenceError("{} escapes the expected workspace".format(label)) from error
    return relative.as_posix(), resolved


def _file_exists(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _file_sha256(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_HASHED_ARTIFACT_BYTES:
            return None
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_HASHED_ARTIFACT_BYTES:
                    return None
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _identity(value: Any, label: str) -> tuple[str, str, str]:
    identity = _closed_object(
        value,
        {"group", "component", "style"},
        label,
    )
    return (
        _require_string(
            identity["group"],
            "{}.group".format(label),
            limit=MAX_IDENTITY_PART_LENGTH,
        ),
        _require_string(
            identity["component"],
            "{}.component".format(label),
            limit=MAX_IDENTITY_PART_LENGTH,
        ),
        _require_string(
            identity["style"],
            "{}.style".format(label),
            limit=MAX_IDENTITY_PART_LENGTH,
        ),
    )


def _parse_state(value: Any, index: int) -> _State:
    label = "states[{}]".format(index)
    state = _closed_object(
        value,
        {"id", "support", "reason", "story_ids", "alternative_evidence"},
        label,
    )
    support = _require_choice(
        state["support"],
        "{}.support".format(label),
        {"supported", "unsupported", "unknown"},
    )
    reason = _optional_reason(state["reason"], "{}.reason".format(label))
    return _State(
        identifier=_require_identifier(state["id"], "{}.id".format(label)),
        support=support,
        reason=reason,
        story_ids=_string_list(
            state["story_ids"],
            "{}.story_ids".format(label),
            identifiers=True,
        ),
        alternative_evidence=_string_list(
            state["alternative_evidence"],
            "{}.alternative_evidence".format(label),
        ),
    )


def _parse_story(value: Any, index: int) -> _Story:
    label = "stories[{}]".format(index)
    story = _closed_object(
        value,
        {
            "id",
            "change",
            "source",
            "source_sha256",
            "change_evidence",
            "change_evidence_sha256",
            "identity",
            "check_ids",
        },
        label,
    )
    change = _require_choice(
        story["change"],
        "{}.change".format(label),
        {"added", "updated", "unchanged"},
    )
    return _Story(
        identifier=_require_identifier(story["id"], "{}.id".format(label)),
        change=change,
        source=_require_string(story["source"], "{}.source".format(label)),
        source_sha256=_require_sha256(
            story["source_sha256"],
            "{}.source_sha256".format(label),
        ),
        change_evidence=_require_string(
            story["change_evidence"],
            "{}.change_evidence".format(label),
        ),
        change_evidence_sha256=_require_sha256(
            story["change_evidence_sha256"],
            "{}.change_evidence_sha256".format(label),
        ),
        identity=_identity(story["identity"], "{}.identity".format(label)),
        check_ids=_string_list(
            story["check_ids"],
            "{}.check_ids".format(label),
            identifiers=True,
        ),
    )


def _parse_registry(value: Any) -> _Registry | None:
    if value is None:
        return None
    registry = _closed_object(
        value,
        {"artifact", "check_id", "identities"},
        "registry",
    )
    identities = _bounded_list(
        registry["identities"],
        "registry.identities",
        maximum=MAX_STORIES,
    )
    return _Registry(
        artifact=_require_string(registry["artifact"], "registry.artifact"),
        check_id=_require_identifier(registry["check_id"], "registry.check_id"),
        identities=tuple(
            _identity(identity, "registry.identities[{}]".format(index))
            for index, identity in enumerate(identities)
        ),
    )


def _parse_check(value: Any, index: int) -> _Check:
    label = "checks[{}]".format(index)
    check = _closed_object(
        value,
        {
            "id",
            "kind",
            "command",
            "outcome",
            "artifact",
            "baseline",
            "revision",
            "diff_sha256",
            "story_ids",
        },
        label,
    )
    kind = _require_choice(
        check["kind"],
        "{}.kind".format(label),
        {"registry", "story"},
    )
    outcome = _require_choice(
        check["outcome"],
        "{}.outcome".format(label),
        {"passed", "failed", "skipped"},
    )
    return _Check(
        identifier=_require_identifier(check["id"], "{}.id".format(label)),
        kind=kind,
        command=_require_string(check["command"], "{}.command".format(label)),
        outcome=outcome,
        artifact=_require_string(check["artifact"], "{}.artifact".format(label)),
        baseline=_require_object_id(check["baseline"], "{}.baseline".format(label)),
        revision=_require_object_id(check["revision"], "{}.revision".format(label)),
        diff_sha256=_require_sha256(
            check["diff_sha256"],
            "{}.diff_sha256".format(label),
        ),
        story_ids=_string_list(
            check["story_ids"],
            "{}.story_ids".format(label),
            identifiers=True,
        ),
    )


def _evaluation(
    status: Status,
    reasons: list[str],
    artifacts: set[str],
) -> Evaluation:
    return Evaluation(
        status=status,
        reasons=tuple(dict.fromkeys(reasons)),
        artifacts=tuple(sorted(artifacts)),
    )


def _json_utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise StoryEvidenceError("JSON input contains invalid Unicode scalars") from error


def _require_bounded_json(value: Any) -> None:
    pending = [(value, 0)]
    estimated_bytes = 0
    visited_nodes = 0
    while pending:
        item, depth = pending.pop()
        visited_nodes += 1
        if visited_nodes > MAX_JSON_NODES:
            raise StoryEvidenceError("JSON input exceeds its node limit")
        if depth > MAX_JSON_DEPTH:
            raise StoryEvidenceError("JSON input exceeds its nesting limit")

        if item is None:
            estimated_bytes += 4
        elif isinstance(item, bool):
            estimated_bytes += 5
        elif isinstance(item, str):
            if len(item) > MAX_STRING_LENGTH:
                raise StoryEvidenceError("JSON string exceeds its size limit")
            estimated_bytes += _json_utf8_size(item) + 2
        elif isinstance(item, int):
            if item.bit_length() > 1_024:
                raise StoryEvidenceError("JSON integer exceeds its size limit")
            estimated_bytes += len(str(item)) + 1
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise StoryEvidenceError("JSON input contains a non-finite number")
            estimated_bytes += 32
        elif isinstance(item, list):
            if len(item) > MAX_REFERENCES_PER_LIST:
                raise StoryEvidenceError("JSON array exceeds its item limit")
            estimated_bytes += len(item) + 2
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, dict):
            if len(item) > MAX_REFERENCES_PER_LIST:
                raise StoryEvidenceError("JSON object exceeds its field limit")
            estimated_bytes += len(item) * 2 + 2
            for key, child in item.items():
                if not isinstance(key, str):
                    raise StoryEvidenceError("JSON object keys must be strings")
                if len(key) > MAX_STRING_LENGTH:
                    raise StoryEvidenceError("JSON object key exceeds its size limit")
                estimated_bytes += _json_utf8_size(key)
                pending.append((child, depth + 1))
        else:
            raise StoryEvidenceError("evidence packet is not JSON data")

        if estimated_bytes > MAX_JSON_BYTES:
            raise StoryEvidenceError("evidence packet exceeds the input size limit")


def evaluate_evidence(
    expected: ExpectedContext,
    evidence: Any,
) -> Evaluation:
    """Evaluate structural evidence against separately supplied project context.

    `evidence_ready` means the packet is internally coherent. It is not story
    PASS: the consuming project must independently verify the delivered diff,
    actual registry metadata, and actual fresh check runs.
    """

    if not isinstance(expected, ExpectedContext):
        raise StoryEvidenceError("expected context must be an ExpectedContext")
    _require_bounded_json(evidence)
    packet = _closed_object(
        evidence,
        {
            "schema",
            "project_id",
            "capability",
            "applicability",
            "baseline",
            "revision",
            "diff_sha256",
            "reason",
            "states",
            "registry",
            "stories",
            "checks",
        },
        "evidence packet",
    )
    if packet["schema"] != EVIDENCE_SCHEMA:
        raise StoryEvidenceError("evidence packet schema is unsupported")

    project_id = _require_string(packet["project_id"], "packet.project_id")
    capability = _require_choice(
        packet["capability"],
        "packet.capability",
        {item.value for item in Capability},
    )
    applicability = _require_choice(
        packet["applicability"],
        "packet.applicability",
        {"ui", "non_ui"},
    )
    baseline = _require_object_id(packet["baseline"], "packet.baseline")
    revision = _require_object_id(packet["revision"], "packet.revision")
    diff_sha256 = _require_sha256(packet["diff_sha256"], "packet.diff_sha256")
    reason = _optional_reason(packet["reason"], "packet.reason")

    state_values = _bounded_list(packet["states"], "states", maximum=MAX_STATES)
    story_values = _bounded_list(packet["stories"], "stories", maximum=MAX_STORIES)
    check_values = _bounded_list(packet["checks"], "checks", maximum=MAX_CHECKS)
    states = tuple(_parse_state(value, index) for index, value in enumerate(state_values))
    stories = tuple(_parse_story(value, index) for index, value in enumerate(story_values))
    registry = _parse_registry(packet["registry"])
    checks = tuple(_parse_check(value, index) for index, value in enumerate(check_values))

    artifacts: set[str] = set()
    resolved_paths: dict[str, Path] = {}

    def reference(value: str, label: str) -> str:
        canonical, path = _safe_reference(expected.workspace_root, value, label)
        artifacts.add(canonical)
        resolved_paths[canonical] = path
        return canonical

    state_alternatives: dict[str, tuple[str, ...]] = {}
    for state in states:
        state_alternatives[state.identifier] = tuple(
            reference(path, "state alternative evidence")
            for path in state.alternative_evidence
        )
    story_paths: dict[str, tuple[str, str]] = {}
    for story in stories:
        story_paths[story.identifier] = (
            reference(story.source, "story source"),
            reference(story.change_evidence, "story change evidence"),
        )
    registry_path: str | None = None
    if registry is not None:
        registry_path = reference(registry.artifact, "registry artifact")
    check_paths: dict[str, str] = {}
    for check in checks:
        check_paths[check.identifier] = reference(check.artifact, "check artifact")

    issues: list[str] = []

    def fail(reason_text: str) -> None:
        issues.append(reason_text)

    if project_id != expected.project_id:
        fail("evidence project does not match project-verifier context")
    if capability != expected.capability.value:
        fail("evidence capability does not match project-verifier context")
    if baseline != expected.baseline:
        fail("evidence baseline does not match project-verifier context")
    if revision != expected.revision:
        fail("evidence revision does not match the current verification source")
    if diff_sha256 != expected.diff_sha256:
        fail("evidence diff digest does not match the current verification diff")
    if issues:
        return _evaluation(Status.FAILED, issues, artifacts)

    if applicability == "non_ui":
        if reason is None:
            fail("non-UI applicability requires a reviewable reason")
        if states or stories or registry is not None or checks:
            fail("non-UI evidence cannot contain UI states, stories, or checks")
        if issues:
            return _evaluation(Status.FAILED, issues, artifacts)
        return _evaluation(Status.NOT_APPLICABLE, [reason], artifacts)

    if not states:
        fail("applicable UI evidence must enumerate changed states or interactions")
    state_ids = [state.identifier for state in states]
    if len(state_ids) != len(set(state_ids)):
        fail("evidence contains duplicate changed-state definitions")

    if expected.capability is Capability.NOT_CONNECTED:
        if not expected.permit_deferred_not_connected:
            fail("project policy does not permit deferred not-connected evidence")
        if reason is None:
            fail("not-connected deferral requires an explicit reason")
        if any(
            state.support != "unknown"
            or state.reason is not None
            or state.story_ids
            or state.alternative_evidence
            for state in states
        ):
            fail(
                "not-connected states must remain unknown, without an unsupported exemption"
            )
        if stories or registry is not None or checks:
            fail("not-connected evidence cannot claim registered stories or story checks")
        if issues:
            return _evaluation(Status.FAILED, issues, artifacts)
        return _evaluation(
            Status.NOT_CONNECTED,
            [
                reason,
                "The story obligation is deferred; this result is not story PASS.",
            ],
            artifacts,
        )

    if reason is not None:
        fail("connected UI evidence cannot provide a task-wide exemption reason")
    if any(state.support == "unknown" for state in states):
        fail("connected UI evidence cannot classify changed states as unknown")

    story_ids = [story.identifier for story in stories]
    if len(story_ids) != len(set(story_ids)):
        fail("evidence contains duplicate story definitions")
    story_by_id = {story.identifier: story for story in stories}

    identity_definitions = [story.identity for story in stories]
    if len(identity_definitions) != len(set(identity_definitions)):
        fail("evidence contains ambiguous duplicate registered-story definitions")
    if registry is not None and len(registry.identities) != len(set(registry.identities)):
        fail("registry evidence contains duplicate identities")

    if len({check.identifier for check in checks}) != len(checks):
        fail("evidence contains duplicate check definitions")
    check_by_id = {check.identifier: check for check in checks}

    supported_states = [state for state in states if state.support == "supported"]
    unsupported_states = [state for state in states if state.support == "unsupported"]
    for state in supported_states:
        if state.reason is not None:
            fail("supported state {} cannot have an unsupported reason".format(state.identifier))
        if state.alternative_evidence:
            fail(
                "supported state {} cannot use an unsupported-state exemption".format(
                    state.identifier
                )
            )
        if not state.story_ids:
            fail("supported state {} has no story mapping".format(state.identifier))
        for story_id in state.story_ids:
            if story_id not in story_by_id:
                fail(
                    "supported state {} maps to a missing story".format(state.identifier)
                )
    for state in unsupported_states:
        if state.reason is None:
            fail("unsupported state {} requires a concrete reason".format(state.identifier))
        if state.story_ids:
            fail(
                "unsupported state {} cannot claim story coverage".format(
                    state.identifier
                )
            )
        if not state.alternative_evidence:
            fail(
                "unsupported state {} requires alternative evidence".format(
                    state.identifier
                )
            )
        for path in state_alternatives[state.identifier]:
            if not _file_exists(resolved_paths[path]):
                fail(
                    "unsupported-state alternative evidence is missing: {}".format(path)
                )

    if not supported_states:
        if stories or registry is not None or checks:
            fail("unsupported-only evidence cannot claim story registrations or checks")
        if issues:
            return _evaluation(Status.FAILED, issues, artifacts)
        return _evaluation(
            Status.UNSUPPORTED,
            [state.reason for state in unsupported_states if state.reason],
            artifacts,
        )

    if registry is None:
        fail("supported UI states require actual registry evidence")
    elif registry_path is not None and not _file_exists(resolved_paths[registry_path]):
        fail("registered-story metadata artifact is missing")

    if not stories:
        fail("supported UI states require added or updated story definitions")
    for story in stories:
        if story.change == "unchanged":
            fail(
                "story {} was only executed, not added or meaningfully updated".format(
                    story.identifier
                )
            )
        if story.change not in {"added", "updated"}:
            fail("story {} has no accepted source-change kind".format(story.identifier))

        source_path, change_path = story_paths[story.identifier]
        actual_source_hash = _file_sha256(resolved_paths[source_path])
        if actual_source_hash is None:
            fail("story source is missing, unreadable, or exceeds the hash limit")
        elif actual_source_hash != story.source_sha256:
            fail("story source digest does not match the current workspace")
        actual_change_hash = _file_sha256(resolved_paths[change_path])
        if actual_change_hash is None:
            fail("story change evidence is missing, unreadable, or exceeds the hash limit")
        elif actual_change_hash != story.change_evidence_sha256:
            fail("story change-evidence digest does not match its retained artifact")

        if not story.check_ids:
            fail("story {} has no actual check references".format(story.identifier))
        for check_id in story.check_ids:
            check = check_by_id.get(check_id)
            if check is None or check.kind != "story":
                fail("story {} references a missing or mismatched check".format(story.identifier))
                continue
            if story.identifier not in check.story_ids:
                fail("story check does not map back to the story definition")

        if registry is not None and story.identity not in registry.identities:
            fail(
                "story {} is absent from supplied registered identities".format(
                    story.identifier
                )
            )

    if registry is not None:
        registry_check = check_by_id.get(registry.check_id)
        if registry_check is None or registry_check.kind != "registry":
            fail("registry evidence references a missing or mismatched registry check")
        elif check_paths.get(registry_check.identifier) != registry_path:
            fail("registry check artifact does not match registry metadata artifact")

    mapped_story_ids = {
        story_id
        for state in supported_states
        for story_id in state.story_ids
    }
    for story in stories:
        if story.identifier not in mapped_story_ids:
            fail("story {} is not mapped to a supported changed state".format(story.identifier))

    expected_story_check_ids = {
        check_id
        for story in stories
        for check_id in story.check_ids
    }
    expected_registry_check_id = registry.check_id if registry is not None else None
    for check in checks:
        if check.baseline != expected.baseline:
            fail("check {} uses a stale baseline".format(check.identifier))
        if check.revision != expected.revision:
            fail("check {} is stale for the current source revision".format(check.identifier))
        if check.diff_sha256 != expected.diff_sha256:
            fail("check {} uses a mismatched diff digest".format(check.identifier))
        if not _file_exists(resolved_paths[check_paths[check.identifier]]):
            fail("check artifact is missing: {}".format(check_paths[check.identifier]))
        if check.outcome != "passed":
            fail("check {} did not pass".format(check.identifier))
        if check.kind == "registry":
            if check.identifier != expected_registry_check_id:
                fail("unreferenced or unexpected registry check")
            if check.story_ids:
                fail("registry check cannot claim story-specific coverage")
        else:
            if check.identifier not in expected_story_check_ids:
                fail("unreferenced story check")
            if not check.story_ids:
                fail("story check must identify the stories it actually checked")
            if any(story_id not in story_by_id for story_id in check.story_ids):
                fail("story check references an unknown story")
            for story_id in check.story_ids:
                if story_id not in story_by_id:
                    continue
                if check.identifier not in story_by_id[story_id].check_ids:
                    fail("story check mapping is not bidirectional")

    if expected_registry_check_id is None:
        fail("connected supported UI evidence requires a registry check")
    if not expected_story_check_ids:
        fail("connected supported UI evidence requires actual story checks")

    if issues:
        return _evaluation(Status.FAILED, issues, artifacts)

    result_reasons = [
        "Packet coherence only; project review must verify meaningful source changes, "
        "the actual registry, and fresh command results."
    ]
    result_reasons.extend(
        state.reason
        for state in unsupported_states
        if state.reason is not None
    )
    return _evaluation(Status.EVIDENCE_READY, result_reasons, artifacts)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StoryEvidenceError("JSON input contains a duplicate object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise StoryEvidenceError("JSON input contains a non-finite number")


def _safe_json_input(
    root: Path,
    relative_path: str,
    label: str,
) -> dict[str, Any]:
    _, path = _safe_reference(root, relative_path, label)
    try:
        if not path.is_file():
            raise StoryEvidenceError("{} file is missing".format(label))
        if path.stat().st_size > MAX_JSON_BYTES:
            raise StoryEvidenceError("{} file exceeds the input size limit".format(label))
        with path.open("rb") as stream:
            raw = stream.read(MAX_JSON_BYTES + 1)
    except OSError as error:
        raise StoryEvidenceError("{} file cannot be read".format(label)) from error
    if len(raw) > MAX_JSON_BYTES:
        raise StoryEvidenceError("{} file exceeds the input size limit".format(label))
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except StoryEvidenceError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise StoryEvidenceError("{} file is not valid UTF-8 JSON".format(label)) from error
    if not isinstance(payload, dict):
        raise StoryEvidenceError("{} file must contain one JSON object".format(label))
    _require_bounded_json(payload)
    return payload


def _error_result(reason: str) -> Evaluation:
    return Evaluation(
        status=Status.FAILED,
        reasons=(reason,),
        artifacts=(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate bounded project story-change evidence."
    )
    parser.add_argument("--context", required=True, help="workspace-relative verifier context JSON")
    parser.add_argument("--evidence", required=True, help="workspace-relative evidence packet JSON")
    arguments = parser.parse_args(argv)

    try:
        workspace_root = Path.cwd().resolve(strict=True)
        context_payload = _safe_json_input(
            workspace_root,
            arguments.context,
            "expected context",
        )
        evidence_payload = _safe_json_input(
            workspace_root,
            arguments.evidence,
            "evidence packet",
        )
        expected = ExpectedContext.from_dict(
            context_payload,
            workspace_root=workspace_root,
        )
        result = evaluate_evidence(expected, evidence_payload)
    except (StoryEvidenceError, OSError, RuntimeError) as error:
        result = _error_result(str(error))
        exit_code = 2
    else:
        exit_code = 1 if result.status is Status.FAILED else 0

    print(json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
