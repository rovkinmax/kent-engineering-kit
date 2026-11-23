"""Resolve an enrolled external R2 profile from native project identity."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tomllib
from typing import Callable


ENROLLMENT_SCHEMA = "ordinary-r2-enrollment-v1"
ENROLLMENT_FIELDS = {
    "schema",
    "project_id",
    "primary_workspace_id",
    "primary_root",
    "settings_root",
    "v2_binding_sha256",
}
PROFILE_FIELDS = {"root", "project_key", "project_uid", "mode"}
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_ENROLLMENT_BYTES = 64 * 1024
MAX_PROFILE_BYTES = 1024 * 1024


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate enrollment field")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("invalid enrollment JSON")


def _key(value):
    if not isinstance(value, str) or not KEY.fullmatch(value):
        raise ValueError("invalid native project identity")
    return value


def _canonical_root(value, *, label):
    if (not isinstance(value, str) or not Path(value).is_absolute()
            or ".." in Path(value).parts or str(Path(value)) != value):
        raise ValueError(f"invalid {label}")
    path = Path(value)
    if path.resolve(strict=True) != path:
        raise ValueError(f"invalid {label}")
    return path


def _profile(raw):
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("bound settings profile is invalid") from error
    config = value.get("project_memory")
    if not isinstance(config, dict) or set(config) != PROFILE_FIELDS:
        raise ValueError("bound R2 storage configuration is incomplete")
    if config.get("mode") != "r2":
        raise ValueError("bound settings profile is not R2")
    _canonical_root(config.get("root"), label="R2 storage root")
    _key(config.get("project_key"))
    _key(config.get("project_uid"))
    return value, config


def read_bound_profile(
    project_id: str,
    primary_root: Path,
    primary_workspace_id: str,
    v2_binding_sha256: str,
    expected_memory: dict[str, object],
    *,
    reader: Callable,
    core=None,
    home: Path | None = None,
) -> dict[str, object]:
    """Read only the profile selected by the private native enrollment record."""
    project_id = _key(project_id)
    primary_workspace_id = _key(primary_workspace_id)
    if not isinstance(v2_binding_sha256, str) or not SHA256.fullmatch(v2_binding_sha256):
        raise ValueError("invalid historical installation binding digest")
    if (not isinstance(expected_memory, dict)
            or set(expected_memory) != {
                "root", "project_key", "project_uid", "legacy_task_ids",
            }):
        raise ValueError("invalid historical storage declaration")

    primary = _canonical_root(str(primary_root), label="native primary root")
    home_root = Path.home() if home is None else Path(home)
    home_root = _canonical_root(str(home_root), label="private home")
    enrollment_path = (
        home_root / ".kent/ordinary-r2/enrollments" / f"{project_id}.json"
    )
    enrollment_raw = reader(
        core, enrollment_path, private=True, maximum=MAX_ENROLLMENT_BYTES,
    )
    try:
        enrollment = json.loads(
            enrollment_raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("ordinary R2 enrollment is invalid") from error
    if (not isinstance(enrollment, dict)
            or set(enrollment) != ENROLLMENT_FIELDS
            or enrollment.get("schema") != ENROLLMENT_SCHEMA):
        raise ValueError("ordinary R2 enrollment schema differs")
    if (enrollment["project_id"] != project_id
            or enrollment["primary_workspace_id"] != primary_workspace_id
            or enrollment["primary_root"] != str(primary)
            or enrollment["v2_binding_sha256"] != v2_binding_sha256):
        raise ValueError("ordinary R2 enrollment identity differs")
    settings_root = _canonical_root(
        enrollment.get("settings_root"), label="external settings root",
    )
    if (settings_root == primary
            or settings_root.is_relative_to(primary)
            or primary.is_relative_to(settings_root)):
        raise ValueError("external settings root overlaps the native workspace")

    profile_path = settings_root / ".kent/workflow-profile.toml"
    profile_raw = reader(
        core, profile_path, private=False, maximum=MAX_PROFILE_BYTES,
    )
    profile, config = _profile(profile_raw)
    expected_config = {
        key: expected_memory[key]
        for key in ("root", "project_key", "project_uid")
    } | {"mode": "r2"}
    if config != expected_config:
        raise ValueError("external R2 storage configuration differs from installation")
    return {
        "settings_root": str(settings_root),
        "profile_path": str(profile_path),
        "profile": profile,
        "config": config,
        "enrollment_sha256": hashlib.sha256(enrollment_raw).hexdigest(),
    }
