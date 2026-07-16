from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any
import tomllib

from .model import SpecError, validate_execution_target


DELIVERY_PROFILES = {"lite", "standard", "team", "release"}
SEMVER_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True)
class ProjectProfile:
    project_root: Path
    schema_version: int
    minimum_kent_version: str
    project_name: str
    workflow_prefix: str
    delivery_profile: str
    platforms: tuple[str, ...]
    source_control: str
    issue_tracker: str
    release_topology: str
    execution_default: str
    execution_overrides: dict[str, str]
    capabilities: dict[str, bool]
    commands: dict[str, str]
    procedures: dict[str, str]
    roles: dict[str, str]

    @classmethod
    def load(cls, project_root: Path) -> "ProjectProfile":
        root = project_root.expanduser().resolve()
        profile_path = root / ".kent" / "workflow-profile.toml"
        if not profile_path.is_file():
            raise SpecError(f"project profile not found: {profile_path}")

        try:
            raw = tomllib.loads(profile_path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise SpecError(f"cannot load project profile {profile_path}: {error}") from error

        execution = require_table(raw, "execution")
        profile = cls(
            project_root=root,
            schema_version=require_int(raw, "schema_version"),
            minimum_kent_version=require_string(raw, "minimum_kent_version"),
            project_name=require_string(raw, "project_name"),
            workflow_prefix=require_string(raw, "workflow_prefix"),
            delivery_profile=require_string(raw, "delivery_profile"),
            platforms=tuple(require_string_list(raw, "platforms")),
            source_control=require_string(raw, "source_control"),
            issue_tracker=require_string(raw, "issue_tracker"),
            release_topology=require_string(raw, "release_topology"),
            execution_default=require_string(execution, "default_target"),
            execution_overrides=string_table(execution.get("overrides", {}), "execution.overrides"),
            capabilities=bool_table(require_table(raw, "capabilities"), "capabilities"),
            commands=string_table(require_table(raw, "commands"), "commands"),
            procedures=string_table(raw.get("procedures", {}), "procedures"),
            roles=string_table(require_table(raw, "roles"), "roles"),
        )
        profile.validate()
        return profile

    def validate(self) -> None:
        if self.schema_version != 2:
            raise SpecError(
                f"unsupported profile schema {self.schema_version}; expected 2"
            )
        minimum_version = self.minimum_version_tuple()
        if minimum_version < (2, 3, 0):
            raise SpecError(
                "profile minimum_kent_version must be 2.3.0 or newer"
            )
        if self.delivery_profile not in DELIVERY_PROFILES:
            raise SpecError(
                f"unsupported delivery_profile {self.delivery_profile!r}"
            )
        if not self.platforms:
            raise SpecError("profile must declare at least one platform")
        validate_execution_target(self.execution_default)
        for target in self.execution_overrides.values():
            validate_execution_target(target)

        for capability in (
            "managed_worktrees",
            "pull_requests",
            "ci_monitoring",
            "device_smoke",
            "compliance_review",
            "spec_review",
        ):
            if capability not in self.capabilities:
                raise SpecError(f"missing capability {capability!r}")

        if not self.command("dispatch"):
            raise SpecError("profile command 'dispatch' is required")
        if not self.command("verify"):
            raise SpecError("profile command 'verify' is required")

        required_roles = {
            "orchestrator",
        }
        if self.capability("compliance_review"):
            required_roles.add("standards_review")
        if self.capability("spec_review"):
            required_roles.add("spec_review")
        for role in sorted(required_roles):
            self.role(role)

        if self.capability("ci_monitoring") and not self.capability("pull_requests"):
            raise SpecError("ci_monitoring requires pull_requests")

    def execution_target(self, workflow_kind: str) -> str:
        return self.execution_overrides.get(workflow_kind, self.execution_default)

    def minimum_version_tuple(self) -> tuple[int, int, int]:
        match = SEMVER_PATTERN.fullmatch(self.minimum_kent_version)
        if match is None:
            raise SpecError(
                "minimum_kent_version must use numeric major.minor.patch format"
            )
        return tuple(int(part) for part in match.groups())

    def capability(self, key: str) -> bool:
        try:
            return self.capabilities[key]
        except KeyError as error:
            raise SpecError(f"unknown capability {key!r}") from error

    def command(self, key: str) -> str:
        return self.commands.get(key, "").strip()

    def procedure(self, key: str) -> str:
        return self.procedures.get(key, "").strip()

    def role(self, key: str) -> str:
        role = self.roles.get(key, "").strip()
        if not role:
            raise SpecError(f"profile role {key!r} is required")
        return role

    def optional_role(self, key: str) -> str:
        return self.roles.get(key, "").strip()

    def workflow_name(self, workflow_kind: str, version: int) -> str:
        if version <= 0:
            raise SpecError("workflow version must be positive")
        display = {
            "delivery": "Engineering Delivery",
            "canary": "Engineering Canary",
        }.get(workflow_kind)
        if display is None:
            raise SpecError(f"unsupported workflow kind {workflow_kind!r}")
        return f"{self.workflow_prefix} {display} v{version}"


def require_table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise SpecError(f"{key} must be a TOML table")
    return value


def require_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"{key} must be a non-empty string")
    return value.strip()


def require_int(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int):
        raise SpecError(f"{key} must be an integer")
    return value


def require_string_list(raw: dict[str, Any], key: str) -> list[str]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise SpecError(f"{key} must be a non-empty string array")
    return [item.strip() for item in value]


def string_table(raw: Any, label: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise SpecError(f"{label} must be a TOML table")
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            raise SpecError(f"{label}.{key} must be a string")
        result[key] = value.strip()
    return result


def bool_table(raw: Any, label: str) -> dict[str, bool]:
    if not isinstance(raw, dict):
        raise SpecError(f"{label} must be a TOML table")
    result: dict[str, bool] = {}
    for key, value in raw.items():
        if not isinstance(value, bool):
            raise SpecError(f"{label}.{key} must be a boolean")
        result[key] = value
    return result
