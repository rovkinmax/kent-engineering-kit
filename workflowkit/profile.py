from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
from typing import Any
import tomllib

from .model import SpecError, validate_execution_target


DELIVERY_PROFILES = {"lite", "standard", "team", "release"}
SMOKE_POLICIES = {"disabled", "conditional", "required"}
WRITER_SESSION_POLICIES = {"continuous", "fresh_per_slice"}
PR_MERGE_STRATEGIES = {"auto", "merge", "squash", "rebase"}
PACKAGE_PUBLISH_TOPOLOGY = "manual-package-publish-after-main"
WORK_KIND_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
SEMVER_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
ROLE_PROMPT_DIRECTORIES = (
    Path(".kent/subagents"),
    Path(".kent/agents"),
)
ROLE_EXECUTION_FIELD_PATTERN = re.compile(
    r"^\s*(model|tools)\s*:",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WorkKind:
    key: str
    description: str
    plan: str
    implement: str


@dataclass(frozen=True)
class ProjectProfile:
    project_root: Path
    schema_version: int
    minimum_kent_version: str
    project_name: str
    workflow_prefix: str
    delivery_profile: str
    platforms: tuple[str, ...]
    required_adapters: tuple[str, ...]
    source_control: str
    issue_tracker: str
    release_topology: str
    execution_default: str
    execution_overrides: dict[str, str]
    policies: dict[str, str]
    capabilities: dict[str, bool]
    legacy_review_contract: bool
    commands: dict[str, str]
    procedures: dict[str, str]
    work_kinds: dict[str, WorkKind]
    adapters: dict[str, str]
    roles: dict[str, str]

    @classmethod
    def load(cls, project_root: Path) -> "ProjectProfile":
        root = project_root.expanduser().resolve()
        profile_path = root / ".kent" / "workflow-profile.toml"
        if not profile_path.is_file():
            raise SpecError(f"project profile not found: {profile_path}")

        try:
            contents = profile_path.read_text()
        except OSError as error:
            raise SpecError(f"cannot load project profile {profile_path}: {error}") from error
        return cls.from_toml(root, contents, source=str(profile_path))

    @classmethod
    def from_toml(
        cls,
        project_root: Path,
        contents: str,
        *,
        source: str = "project profile",
        check_files: bool = True,
    ) -> "ProjectProfile":
        root = project_root.expanduser().resolve()
        try:
            raw = tomllib.loads(contents)
        except tomllib.TOMLDecodeError as error:
            raise SpecError(f"cannot load project profile {source}: {error}") from error
        schema_version = require_int(raw, "schema_version")
        if schema_version != 3:
            raise SpecError(
                f"unsupported profile schema {schema_version}; expected 3"
            )
        execution = require_table(raw, "execution")
        capabilities = bool_table(
            require_table(raw, "capabilities"),
            "capabilities",
        )
        legacy_review_contract = (
            "standards_review" not in capabilities
            and "compliance_review" in capabilities
        )
        if legacy_review_contract:
            # TODO(profile-schema-next): Remove this compatibility path after
            # every project explicitly migrates to separate standards_review
            # and compliance_review capabilities in the finalized next schema.
            # Early schema-3 profiles used compliance_review for the Standards
            # branch and emitted compliance_report. Preserve that full contract
            # until the project explicitly opts into the split capabilities.
            capabilities["standards_review"] = capabilities["compliance_review"]
            capabilities["compliance_review"] = False
        profile = cls(
            project_root=root,
            schema_version=schema_version,
            minimum_kent_version=require_string(raw, "minimum_kent_version"),
            project_name=require_string(raw, "project_name"),
            workflow_prefix=require_string(raw, "workflow_prefix"),
            delivery_profile=require_string(raw, "delivery_profile"),
            platforms=tuple(require_string_list(raw, "platforms")),
            required_adapters=tuple(
                require_string_list(raw, "required_adapters")
            ),
            source_control=require_string(raw, "source_control"),
            issue_tracker=require_string(raw, "issue_tracker"),
            release_topology=require_string(raw, "release_topology"),
            execution_default=require_string(execution, "default_target"),
            execution_overrides=string_table(
                execution.get("overrides", {}),
                "execution.overrides",
            ),
            policies=string_table(require_table(raw, "policies"), "policies"),
            capabilities=capabilities,
            legacy_review_contract=legacy_review_contract,
            commands=string_table(require_table(raw, "commands"), "commands"),
            procedures=string_table(raw.get("procedures", {}), "procedures"),
            work_kinds=work_kind_table(require_table(raw, "work_kinds")),
            adapters=string_table(raw.get("adapters", {}), "adapters"),
            roles=string_table(require_table(raw, "roles"), "roles"),
        )
        profile.validate(check_files=check_files)
        return profile

    def validate(self, *, check_files: bool = True) -> None:
        if self.schema_version != 3:
            raise SpecError(
                f"unsupported profile schema {self.schema_version}; expected 3"
            )
        minimum_version = self.minimum_version_tuple()
        if minimum_version < (2, 5, 0):
            raise SpecError(
                "profile minimum_kent_version must be 2.5.0 or newer"
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

        smoke_policy = self.smoke_policy()
        if smoke_policy not in SMOKE_POLICIES:
            raise SpecError(
                f"unsupported policies.smoke {smoke_policy!r}; expected one of "
                f"{sorted(SMOKE_POLICIES)}"
            )
        writer_session_policy = self.writer_session_policy()
        if writer_session_policy not in WRITER_SESSION_POLICIES:
            raise SpecError(
                "unsupported policies.writer_sessions "
                f"{writer_session_policy!r}; expected one of "
                f"{sorted(WRITER_SESSION_POLICIES)}"
            )
        pr_merge_strategy = self.pr_merge_strategy()
        if pr_merge_strategy not in PR_MERGE_STRATEGIES:
            raise SpecError(
                "unsupported policies.pr_merge_strategy "
                f"{pr_merge_strategy!r}; expected one of "
                f"{sorted(PR_MERGE_STRATEGIES)}"
            )
        if "device_smoke" in self.capabilities:
            raise SpecError(
                "capabilities.device_smoke was removed in profile schema 3; "
                "use policies.smoke"
            )
        if len(set(self.required_adapters)) != len(self.required_adapters):
            raise SpecError("required_adapters must not contain duplicates")
        for adapter_key in self.required_adapters:
            configured_path = self.adapter(adapter_key)
            if not configured_path:
                raise SpecError(
                    f"required adapter {adapter_key!r} is missing from adapters"
                )
            if not check_files:
                continue
            adapter_path = self.resolve_project_path(
                configured_path,
                f"adapters.{adapter_key}",
            )
            if not adapter_path.is_file():
                raise SpecError(
                    f"required adapter not found: {adapter_path}"
                )
            if not os.access(adapter_path, os.X_OK):
                raise SpecError(
                    f"required adapter is not executable by the current user: "
                    f"{adapter_path}"
                )

        for capability in (
            "managed_worktrees",
            "pull_requests",
            "ci_monitoring",
            "standards_review",
            "compliance_review",
            "spec_review",
        ):
            if capability not in self.capabilities:
                raise SpecError(f"missing capability {capability!r}")

        if not self.command("dispatch"):
            raise SpecError("profile command 'dispatch' is required")
        if not self.command("verify"):
            raise SpecError("profile command 'verify' is required")
        if not self.command("checkpoint"):
            raise SpecError("profile command 'checkpoint' is required")
        if self.capability("managed_worktrees") and not self.command("janitor"):
            raise SpecError(
                "profile command 'janitor' is required for managed worktrees"
            )
        if self.capability("pull_requests"):
            if self.source_control != "github":
                raise SpecError(
                    "generated pull-request waiting currently requires "
                    "source_control = 'github'"
                )
            if not self.command("wait_pr"):
                raise SpecError(
                    "profile command 'wait_pr' is required for pull requests"
                )
        if self.package_publish_after_main():
            if not self.capability("pull_requests"):
                raise SpecError(
                    f"release_topology {PACKAGE_PUBLISH_TOPOLOGY!r} "
                    "requires pull_requests"
                )
            if not self.procedure("publish"):
                raise SpecError(
                    f"release_topology {PACKAGE_PUBLISH_TOPOLOGY!r} "
                    "requires procedures.publish"
                )
            if check_files:
                publish_path = self.resolve_project_path(
                    self.procedure("publish"),
                    "procedures.publish",
                )
                if not publish_path.is_file():
                    raise SpecError(
                        f"publish procedure not found: {publish_path}"
                    )

        if not self.work_kinds:
            raise SpecError("profile must declare at least one work kind")
        for key, work_kind in self.work_kinds.items():
            if key != work_kind.key:
                raise SpecError(
                    f"work kind mapping key {key!r} does not match "
                    f"{work_kind.key!r}"
                )
            if not check_files:
                continue
            for stage, configured_path in (
                ("plan", work_kind.plan),
                ("implement", work_kind.implement),
            ):
                procedure_path = self.resolve_project_path(
                    configured_path,
                    f"work_kinds.{key}.{stage}",
                )
                if not procedure_path.is_file():
                    raise SpecError(
                        f"work kind procedure not found: {procedure_path}"
                    )

        required_roles = {
            "fix",
            "implementation",
            "orchestrator",
            "release",
        }
        if smoke_policy != "disabled":
            required_roles.add("qa")
        if self.capability("pull_requests"):
            required_roles.add("ci")
        if self.capability("standards_review"):
            required_roles.add("standards_review")
        if (
            self.capability("pull_requests")
            and self.capability("compliance_review")
        ):
            required_roles.add("compliance")
        if self.capability("spec_review"):
            required_roles.add("spec_review")
        if self.package_publish_after_main():
            required_roles.add("package_release")
        for role in sorted(required_roles):
            self.role(role)

        if self.capability("ci_monitoring") and not self.capability("pull_requests"):
            raise SpecError("ci_monitoring requires pull_requests")
        if check_files:
            validate_project_role_prompts(self.project_root)

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

    def smoke_policy(self) -> str:
        policy = self.policies.get("smoke", "").strip()
        if not policy:
            raise SpecError("profile policy 'smoke' is required")
        return policy

    def writer_session_policy(self) -> str:
        return self.policies.get("writer_sessions", "continuous").strip()

    def pr_merge_strategy(self) -> str:
        return self.policies.get("pr_merge_strategy", "auto").strip()

    def package_publish_after_main(self) -> bool:
        return self.release_topology == PACKAGE_PUBLISH_TOPOLOGY

    def command(self, key: str) -> str:
        return self.commands.get(key, "").strip()

    def procedure(self, key: str) -> str:
        return self.procedures.get(key, "").strip()

    def work_kind(self, key: str) -> WorkKind:
        try:
            return self.work_kinds[key]
        except KeyError as error:
            raise SpecError(f"unsupported work kind {key!r}") from error

    def adapter(self, key: str) -> str:
        return self.adapters.get(key, "").strip()

    def resolve_project_path(self, configured_path: str, label: str) -> Path:
        relative = Path(configured_path)
        if relative.is_absolute():
            raise SpecError(f"{label} must be project-relative")

        lexical = self.project_root
        for part in relative.parts:
            if part in ("", "."):
                continue
            lexical /= part
            if lexical.is_symlink():
                raise SpecError(f"{label} must not contain symlinks")

        resolved = lexical.resolve()
        if not resolved.is_relative_to(self.project_root):
            raise SpecError(f"{label} escapes the project root")
        return resolved

    def role(self, key: str) -> str:
        role = self.roles.get(key, "").strip()
        if not role:
            raise SpecError(f"profile role {key!r} is required")
        return role

    def optional_role(self, key: str) -> str:
        return self.roles.get(key, "").strip()

    def workflow_name(
        self,
        workflow_kind: str,
        version: int | None = None,
        label: str = "",
    ) -> str:
        if workflow_kind == "smoke-lab":
            if version is not None:
                raise SpecError("smoke-lab workflow names are not versioned")
            normalized_label = " ".join(label.split())
            suffix = f" {normalized_label}" if normalized_label else ""
            return f"{self.workflow_prefix} Engineering Smoke Lab{suffix}"
        if label.strip():
            raise SpecError("labels are supported only for smoke-lab workflows")
        if version is None or version <= 0:
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


def work_kind_table(raw: Any) -> dict[str, WorkKind]:
    if not isinstance(raw, dict):
        raise SpecError("work_kinds must be a TOML table")
    result: dict[str, WorkKind] = {}
    for key, value in raw.items():
        if not WORK_KIND_KEY_PATTERN.fullmatch(key):
            raise SpecError(
                f"work kind key {key!r} is not a stable model key"
            )
        if not isinstance(value, dict):
            raise SpecError(f"work_kinds.{key} must be a TOML table")
        unknown_fields = set(value) - {"description", "plan", "implement"}
        if unknown_fields:
            raise SpecError(
                f"work_kinds.{key} has unsupported fields "
                f"{sorted(unknown_fields)}"
            )
        result[key] = WorkKind(
            key=key,
            description=require_string(
                value,
                "description",
            ),
            plan=require_string(value, "plan"),
            implement=require_string(value, "implement"),
        )
    return result


def validate_project_role_prompts(project_root: Path) -> None:
    root = project_root.expanduser().resolve()
    for relative_directory in ROLE_PROMPT_DIRECTORIES:
        directory = root / relative_directory
        if not directory.is_dir():
            continue
        for prompt_path in sorted(directory.rglob("*.md")):
            try:
                lines = prompt_path.read_text().splitlines()
            except (OSError, UnicodeError) as error:
                raise SpecError(
                    f"cannot read role prompt {prompt_path}: {error}"
                ) from error
            if not lines or lines[0].strip() != "---":
                continue
            for line_number, line in enumerate(lines[1:], start=2):
                if line.strip() == "---":
                    break
                if ROLE_EXECUTION_FIELD_PATTERN.match(line):
                    relative_path = prompt_path.relative_to(root)
                    raise SpecError(
                        f"{relative_path}:{line_number}: role prompts must not "
                        "declare model or tools; configure execution policy in "
                        ".kent/config.toml or the global Kent config"
                    )
