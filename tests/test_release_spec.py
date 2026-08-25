from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

import workflowkit
from workflowkit.release import (
    ApprovalMaterialization,
    AuthoritySpec,
    AuthorityTemplateSpec,
    GitHubRefPolicy,
    NormalizedGitHubWorkflowSourceV1,
    ReleaseSourceManifest,
    ReleaseSpec,
    ReleaseSpecError,
    SelectedReleaseArtifacts,
    canonical_json_bytes,
    canonicalize_publication_operation,
    render_approval_summary,
    render_release_preview,
    sha256_digest,
    validate_effect_job_sources,
    validate_approval_materialization,
    validate_operation_jobs,
    validate_qualification_job_sources,
    validate_required_job_sources,
    WorkflowSourceIntent,
)
from workflowkit.runtime import (
    RuntimeAuthorityBinding,
    RuntimeContractError,
    RuntimeExecutionContext,
    RuntimeExternalRoot,
    _make_selected_runtime_source_inputs,
    _resolve_runtime_authority_binding,
    capture_runtime_authority_binding,
    capture_runtime_execution_context,
)

PACKAGE_READ_SECRET = "GITHUB_PACKAGES_TOKEN"
PACKAGE_READ_SECRET_EXPRESSION = "${{ secrets.GITHUB_PACKAGES_TOKEN }}"
CACHE_RESTORE_ACTION = "actions/cache/restore@" + "a" * 40


def source_manifest() -> dict:
    return {
        "schema": "release_source_manifest_v1",
        "closure_algorithm": "project-instruction-closure-v1",
        "project_name": "Example",
        "repository": "owner/repository",
        "topology_kind": "appsome-release-publication",
        "additional_paths": [],
        "additional_trees": [],
        "declared_prompt_references": [],
        "external_roots": [],
        "runtime_attested": False,
    }


def source_manifest_ref() -> dict:
    return {
        "schema": "release_source_manifest_v1",
        "path": ".kent/release/source-manifest.json",
        "revision_binding": "runtime-source-envelope",
        "runtime_attested": False,
    }


def workflow_intent() -> dict:
    return {
        "name": "Example Release",
        "id": "123e4567-e89b-12d3-a456-426614174000",
        "update_kind": "graph-and-metadata",
        "expected_project_link": "non-default",
        "expected_project_default": False,
        "allow_create": False,
        "allow_default_change": False,
        "allow_uuid_change": False,
    }


def step(
    *,
    run: str = "echo ok",
    uses: str = "",
    secret_refs: list[str] | None = None,
    validation_required: bool = False,
    name: str = "validate",
    condition: str = "",
    continue_on_error: bool = False,
    with_values: dict | None = None,
    effective_environment: dict | None = None,
) -> dict:
    if uses:
        run = ""
    return {
        "kind": "uses" if uses else "run",
        "name": name,
        "condition": condition,
        "continue_on_error": continue_on_error,
        "uses": uses,
        "with": with_values or {},
        "run": run,
        "effective_shell": "bash",
        "effective_working_directory": "",
        "effective_environment": effective_environment or {},
        "secret_refs": secret_refs or [],
        **({"validation_required": validation_required} if validation_required else {}),
    }


def job(
    key: str,
    *,
    condition: str = "",
    permissions: dict[str, str] | None = None,
    secret_refs: list[str] | None = None,
    run: str = "echo ok",
    matrix: dict | None = None,
    validation_required: bool = False,
    runs_on: str = "ubuntu-latest",
    continue_on_error: bool = False,
    checkout_persist_credentials: bool = False,
    effective_environment: dict | None = None,
    steps: list[dict] | None = None,
) -> dict:
    refs = secret_refs or []
    default_run = run
    if refs and not any(f"secrets.{name}" in run for name in refs):
        default_run = f"echo ${{{{ secrets.{refs[0]} }}}}"
    return {
        "job_key": key,
        "job_display_name": key.replace("_", " ").title(),
        "needs": [],
        "matrix": matrix or {},
        "condition": condition,
        "continue_on_error": continue_on_error,
        "runs_on": runs_on,
        "runner_environment_asserted": True,
        "effective_permissions": permissions or {"contents": "read"},
        "effective_defaults_run": {"shell": "", "working_directory": ""},
        "github_environment": "",
        "services": {},
        "container": None,
        "checkout_persist_credentials": checkout_persist_credentials,
        "secret_refs": refs,
        "effective_environment": effective_environment or {},
        "steps": steps or [step(run=default_run, secret_refs=refs)],
    }


def normalized_workflow(
    *,
    path: str = ".github/workflows/release.yml",
    environment: dict | None = None,
    jobs: list[dict] | None = None,
) -> NormalizedGitHubWorkflowSourceV1:
    raw = {
        "schema": "normalized_github_workflow_source_v1",
        "workflow_path": path,
        "workflow_display_name": "Release",
        "events": [
            {
                "name": "pull_request",
                "branches": [],
                "branches_ignore": [],
                "tags": [],
                "tags_ignore": [],
                "paths": [],
                "paths_ignore": [],
                "types": [],
                "dispatch_inputs": [],
            }
        ],
        "permissions": {"contents": "read"},
        "environment": environment or {},
        "defaults_run": {"shell": "", "working_directory": ""},
        "jobs": jobs or [job("required_release", validation_required=True)],
    }
    return NormalizedGitHubWorkflowSourceV1.from_dict(raw)


def event_record(name: str = "pull_request", *, tags: list[str] | None = None) -> dict:
    return {
        "name": name,
        "branches": [],
        "branches_ignore": [],
        "tags": tags or [],
        "tags_ignore": [],
        "paths": [],
        "paths_ignore": [],
        "types": [],
        "dispatch_inputs": [],
    }


def contract_row(
    kind: str,
    key: str,
    source_job: dict,
    *,
    event_selector: dict | None = None,
    workflow_path: str = ".github/workflows/release.yml",
    runner_trust: str | None = None,
    credential_profile: str | None = None,
    allowed_effects: list[str] | None = None,
    skip_policy: str | None = None,
) -> dict:
    row = {
        "contract_key": key,
        "workflow_path": workflow_path,
        "event_selector": event_selector or event_record(),
        **deepcopy(source_job),
        "runner_trust": runner_trust or (
            "github-hosted-standard-ephemeral"
            if kind != "effect"
            else "github-hosted-standard-ephemeral-effect"
        ),
        "credential_profile": credential_profile or (
            "release"
            if kind == "effect"
            else (
                "none"
                if kind == "qualification"
                else "github-platform-contents-read"
            )
        ),
        "allowed_effects": allowed_effects or (
            ["dependency-downloads", "github-actions-logs"]
            if kind != "effect"
            else ["publish"]
        ),
        "skip_policy": skip_policy or ("never" if kind == "required" else "event-gated"),
        "branch_protection_required": kind == "required",
        "control_plane_fixtures_forbidden": True,
        "credential_scope_is_job_local": kind == "effect",
    }
    row["steps"] = [
        {**item, "validation_required": kind == "required"}
        for item in row["steps"]
    ]
    return row


def package_read_job(
    key: str,
    *,
    extra_steps: list[dict] | None = None,
    permissions: dict[str, str] | None = None,
    runs_on: str = "ubuntu-latest",
    effective_environment: dict | None = None,
) -> dict:
    recipient = step(
        run="python -m pip install package",
        secret_refs=[PACKAGE_READ_SECRET],
        effective_environment={
            PACKAGE_READ_SECRET: PACKAGE_READ_SECRET_EXPRESSION,
        },
    )
    return job(
        key,
        permissions=permissions or {"contents": "read", "packages": "read"},
        runs_on=runs_on,
        secret_refs=[PACKAGE_READ_SECRET],
        effective_environment=effective_environment,
        steps=[recipient, *(extra_steps or [])],
    )


def tables() -> tuple[dict, dict]:
    required_job = job("required_release", validation_required=True)
    effect_job = job(
        "publish_release",
        condition="github.event_name == 'workflow_dispatch'",
        permissions={"contents": "write"},
    )
    required = {
        "schema": "required_jobs_v1",
        "jobs": [contract_row("required", "required_release_contract", required_job)],
    }
    effect = {
        "schema": "effect_jobs_v1",
        "jobs": [contract_row("effect", "publish_release_contract", effect_job)],
    }
    return required, effect


def valid_spec() -> dict:
    required, effect = tables()
    return {
        "schema_version": 1,
        "spec_kind": "release",
        "topology_kind": "appsome-release-publication",
        "adoption_mode": "managed-in-place",
        "project_name": "Example",
        "repository": "owner/repository",
        "runtime_attested": False,
        "workflow_source_intent": workflow_intent(),
        "source_manifest": source_manifest_ref(),
        "required_jobs_v1": required,
        "qualification_jobs_v1": {
            "schema": "qualification_jobs_v1",
            "jobs": [],
        },
        "effect_jobs_v1": effect,
        "operation_variants": [
            {
                "key": "publish",
                "operation_kind": "publish",
                "authority_kind": {
                    "kind": "github_run",
                    "workflow_path": ".github/workflows/release.yml",
                    "workflow_name": "Release",
                    "event": "workflow_dispatch",
                    "run_id": 7,
                    "attempt": 1,
                    "head_sha": "a" * 40,
                    "ref": "refs/heads/main",
                },
                "authority_transitions": [],
                "required_job_contract_keys": ["required_release_contract"],
                "qualification_job_contract_keys": [],
                "effect_job_contract_keys": ["publish_release_contract"],
                "approval_required": False,
                "project_fields": [
                    {
                        "name": "version",
                        "type": "string",
                        "nullable": False,
                        "approval_renderable": True,
                    }
                ],
            }
        ],
    }


class ReleaseSpecTest(unittest.TestCase):
    def _runtime_inputs(
        self,
        *,
        commit: str = "a" * 40,
        external_roots: tuple[RuntimeExternalRoot, ...] = (),
    ):
        return _make_selected_runtime_source_inputs(
            project_name="Example",
            repository="owner/repository",
            topology_kind="appsome-release-publication",
            project_commit=commit,
            source_preview={"selected": True},
            artifact_digests={
                "spec_raw_blob_sha256": "b" * 64,
                "source_manifest_raw_blob_sha256": "c" * 64,
                "snapshot_raw_blob_sha256": "d" * 64,
            },
            external_roots=external_roots,
        )

    def _github_chain(
        self,
        *,
        commit: str = "a" * 40,
        ref: str = "refs/heads/main",
        run_id: int = 7,
        attempt: int = 1,
    ):
        inputs = self._runtime_inputs(commit=commit)
        execution = {
            "kind": "github_run",
            "repository": "owner/repository",
            "workflow_path": ".github/workflows/release.yml",
            "workflow_name": "Release",
            "event": "workflow_dispatch",
            "run_id": run_id,
            "attempt": attempt,
            "head_sha": commit,
            "ref": ref,
        }
        context = capture_runtime_execution_context(inputs, execution)
        authority = {
            key: execution[key]
            for key in (
                "kind",
                "workflow_path",
                "workflow_name",
                "event",
                "run_id",
                "attempt",
                "head_sha",
                "ref",
            )
        }
        binding = capture_runtime_authority_binding(
            inputs,
            [],
            context,
            authority,
        )
        return inputs, context, binding, authority

    def _kent_chain(
        self,
        *,
        commit: str = "a" * 40,
        task_short_id: str = "KIT-42",
        transition: str = "approve",
        workflow_revision: int = 2,
    ):
        inputs = self._runtime_inputs(commit=commit)
        execution = {
            "kind": "kent_transition",
            "task_id": "task-123e4567-e89b-12d3-a456-426614174000",
            "task_short_id": task_short_id,
            "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
            "workflow_revision": workflow_revision,
            "project_id": "project-123e4567-e89b-12d3-a456-426614174000",
            "project_commit": commit,
            "authority_transition": transition,
        }
        context = capture_runtime_execution_context(inputs, execution)
        authority = {
            "kind": "kent_transition",
            "task_short_id": task_short_id,
            "workflow_id": execution["workflow_id"],
            "workflow_revision": execution["workflow_revision"],
            "project_id": execution["project_id"],
            "approval_authority": "release-manager",
            "authority_transition": transition,
        }
        binding = capture_runtime_authority_binding(inputs, [], context, authority)
        return inputs, context, binding, authority

    def _validated_jobs(self, spec: ReleaseSpec) -> object:
        workflow = normalized_workflow(
            jobs=[
                job("required_release", validation_required=True),
                job(
                    "publish_release",
                    condition="github.event_name == 'workflow_dispatch'",
                    permissions={"contents": "write"},
                ),
            ]
        )
        return validate_operation_jobs(
            spec.operation_variants[0],
            workflow,
            required=spec.required_jobs_v1,
            qualification=spec.qualification_jobs_v1,
            effect=spec.effect_jobs_v1,
        )

    def test_schema2_templates_and_ref_policy_are_closed(self) -> None:
        spec_data = valid_spec()
        spec_data["schema_version"] = 2
        variant = spec_data["operation_variants"][0]
        variant["authority_kind"] = {
            "kind": "github_run_template",
            "workflow_path": ".github/workflows/release.yml",
            "workflow_name": "Release",
            "event": "workflow_dispatch",
            "ref_policy": {
                "kind": "prefix_project_field",
                "prefix": "refs/tags/v",
                "project_field": "version",
            },
        }
        variant["authority_transitions"] = []
        variant["approval_required"] = False
        spec = ReleaseSpec.from_dict(spec_data)
        self.assertIsInstance(spec.operation_variants[0].authority_kind, AuthorityTemplateSpec)
        self.assertEqual(
            spec.operation_variants[0].authority_kind.values["ref_policy"],
            {
                "kind": "prefix_project_field",
                "prefix": "refs/tags/v",
                "project_field": "version",
            },
        )
        with self.assertRaises(ReleaseSpecError):
            GitHubRefPolicy.from_dict(
                {"kind": "exact", "ref": "refs/tags/v1", "extra": True}
            )
        with self.assertRaises(ReleaseSpecError):
            GitHubRefPolicy.from_dict(
                {"kind": "exact", "ref": "refs/tags/{version}"}
            )
        missing_field = deepcopy(spec_data)
        missing_field["operation_variants"][0]["project_fields"] = []
        with self.assertRaises(ReleaseSpecError):
            ReleaseSpec.from_dict(missing_field)
        nullable_field = deepcopy(spec_data)
        nullable_field["operation_variants"][0]["project_fields"][0]["nullable"] = True
        with self.assertRaises(ReleaseSpecError):
            ReleaseSpec.from_dict(nullable_field)

    def test_schema_bound_authority_kinds_do_not_cross_versions(self) -> None:
        concrete_kent = valid_spec()
        concrete_kent["operation_variants"][0]["authority_kind"] = {
            "kind": "kent_transition_template",
            "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
            "project_id": "project-123e4567-e89b-12d3-a456-426614174000",
            "approval_authority": "release-manager",
        }
        concrete_github = valid_spec()
        concrete_github["operation_variants"][0]["authority_kind"] = {
            "kind": "github_run_template",
            "workflow_path": ".github/workflows/release.yml",
            "workflow_name": "Release",
            "event": "workflow_dispatch",
            "ref_policy": {"kind": "exact", "ref": "refs/heads/main"},
        }
        for concrete in (concrete_kent, concrete_github):
            with self.assertRaises(ReleaseSpecError):
                ReleaseSpec.from_dict(concrete)
        templated_kent = deepcopy(concrete_kent)
        templated_kent["schema_version"] = 2
        templated_kent["operation_variants"][0]["authority_kind"] = {
            "kind": "kent_transition",
            "task_short_id": "KIT-42",
            "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
            "workflow_revision": 2,
            "project_id": "project-123e4567-e89b-12d3-a456-426614174000",
            "approval_authority": "release-manager",
            "authority_transition": "approve",
        }
        templated_kent["operation_variants"][0]["authority_transitions"] = ["approve"]
        templated_kent["operation_variants"][0]["approval_required"] = False
        templated_github = deepcopy(concrete_github)
        templated_github["schema_version"] = 2
        templated_github["operation_variants"][0]["authority_kind"] = (
            valid_spec()["operation_variants"][0]["authority_kind"]
        )
        for templated in (templated_kent, templated_github):
            with self.assertRaises(ReleaseSpecError):
                ReleaseSpec.from_dict(templated)

    def test_schema1_rejects_every_runtime_argument_shape(self) -> None:
        spec = ReleaseSpec.from_dict(valid_spec())
        workflow = normalized_workflow(
            jobs=[
                job("required_release", validation_required=True),
                job(
                    "publish_release",
                    condition="github.event_name == 'workflow_dispatch'",
                    permissions={"contents": "write"},
                ),
            ]
        )
        validated = validate_operation_jobs(
            spec.operation_variants[0],
            workflow,
            required=spec.required_jobs_v1,
            qualification=spec.qualification_jobs_v1,
            effect=spec.effect_jobs_v1,
        )
        _inputs, context, binding, authority = self._github_chain()
        operation = {
            "schema_version": 1,
            "variant_key": "publish",
            "operation_kind": "publish",
            "repository": "owner/repository",
            "runtime_source_envelope_digest": "b" * 64,
            "operation_jobs_manifest_digest": validated.operation_jobs_manifest_digest,
            "authority": spec.operation_variants[0].authority_kind.as_dict(),
            "project_fields": {"version": "1.2.3"},
        }
        concrete = AuthoritySpec.from_dict(authority)
        for argument in (context, binding, concrete, {}, object()):
            for keyword in ("runtime_execution_context", "runtime_authority_binding"):
                with self.subTest(keyword=keyword, argument=type(argument).__name__):
                    with self.assertRaises(ReleaseSpecError):
                        canonicalize_publication_operation(
                            operation,
                            spec.operation_variants[0],
                            validated,
                            spec=spec,
                            **{keyword: argument},
                        )

    def test_schema1_golden_operation_digests_remain_frozen(self) -> None:
        github = ReleaseSpec.from_dict(valid_spec())
        github_jobs = self._validated_jobs(github)
        github_operation = {
            "schema_version": 1,
            "variant_key": "publish",
            "operation_kind": "publish",
            "repository": "owner/repository",
            "runtime_source_envelope_digest": "b" * 64,
            "operation_jobs_manifest_digest": github_jobs.operation_jobs_manifest_digest,
            "authority": github.operation_variants[0].authority_kind.as_dict(),
            "project_fields": {"version": "1.2.3"},
        }
        github_result = canonicalize_publication_operation(
            github_operation,
            github.operation_variants[0],
            github_jobs,
            spec=github,
        )
        self.assertEqual(
            github_result.operation_digest,
            "ff8e1bd0cf69743407d16fd1226506dce5872e94352290b1ec8f169b9cf0c974",
        )
        kent_data = valid_spec()
        kent_data["operation_variants"][0]["authority_kind"] = {
            "kind": "kent_transition",
            "task_short_id": "KIT-42",
            "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
            "workflow_revision": 2,
            "project_id": "project-123e4567-e89b-12d3-a456-426614174000",
            "approval_authority": "release-manager",
            "authority_transition": "approve",
        }
        kent_data["operation_variants"][0]["authority_transitions"] = ["approve"]
        kent_data["operation_variants"][0]["approval_required"] = True
        kent_data["approval_materializations"] = [
            {
                "variant_key": "publish",
                "source_path": ".kent/scripts/approve",
                "source_node_key": "approval",
                "source_node_kind": "script",
                "authority_transition_parameter": "authority_transition",
                "summary_language": "ru",
                "summary_sections": ["Нужно от вас", "Почему", "После подтверждения"],
                "materialized_before_pending_approval": True,
                "commentary_equals_summary": True,
                "decision_may_select_approval": False,
                "required_fields": ["version"],
                "templates": {
                    "approve": {
                        "Нужно от вас": "Версия {{version}}",
                        "Почему": "Digest {{operation_digest}}",
                        "После подтверждения": "Продолжить",
                    }
                },
            }
        ]
        kent = ReleaseSpec.from_dict(kent_data)
        kent_jobs = self._validated_jobs(kent)
        kent_operation = {
            "schema_version": 1,
            "variant_key": "publish",
            "operation_kind": "publish",
            "repository": "owner/repository",
            "runtime_source_envelope_digest": "b" * 64,
            "operation_jobs_manifest_digest": kent_jobs.operation_jobs_manifest_digest,
            "authority": kent.operation_variants[0].authority_kind.as_dict(),
            "project_fields": {"version": "1.2.3"},
        }
        kent_result = canonicalize_publication_operation(
            kent_operation,
            kent.operation_variants[0],
            kent_jobs,
            spec=kent,
        )
        self.assertEqual(
            kent_result.operation_digest,
            "8a69bad667751421cf4fa20457edb22d67c9c17d7f5d963b80f7b5b4571ccee0",
        )

    def test_schema2_source_strings_reject_all_placeholder_forms(self) -> None:
        sentinels = (
            "runtime",
            "dynamic",
            "current",
            "auto",
            "any",
            "unknown",
            "unset",
            "null",
            "none",
            "*",
            "-",
            "0",
            "$runtime",
            "${runtime}",
            "<runtime>",
        )
        invalid = (
            *sentinels,
            " leading",
            "trailing ",
            "{value}",
            "$value",
            "<value>",
            "bad\x00value",
            "bad\x7fvalue",
        )
        kent = {
            "kind": "kent_transition_template",
            "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
            "project_id": "project-123e4567-e89b-12d3-a456-426614174000",
            "approval_authority": "release-manager",
        }
        for field in tuple(kent):
            for value in invalid:
                broken = deepcopy(kent)
                broken[field] = value
                with self.subTest(kind="kent", field=field, value=repr(value)):
                    with self.assertRaises(ReleaseSpecError):
                        AuthorityTemplateSpec.from_dict(broken)
        github = {
            "kind": "github_run_template",
            "workflow_path": ".github/workflows/release.yml",
            "workflow_name": "Release",
            "event": "workflow_dispatch",
            "ref_policy": {
                "kind": "exact",
                "ref": "refs/heads/main",
            },
        }
        for field in ("kind", "workflow_path", "workflow_name", "event"):
            for value in invalid:
                broken = deepcopy(github)
                broken[field] = value
                with self.subTest(kind="github", field=field, value=repr(value)):
                    with self.assertRaises(ReleaseSpecError):
                        AuthorityTemplateSpec.from_dict(broken)
        for field in ("kind", "ref"):
            for value in invalid:
                broken = deepcopy(github["ref_policy"])
                broken[field] = value
                with self.subTest(kind="ref_policy", field=field, value=repr(value)):
                    with self.assertRaises(ReleaseSpecError):
                        GitHubRefPolicy.from_dict(broken)
        for field in ("kind", "prefix", "project_field"):
            for value in invalid:
                broken = {
                    "kind": "prefix_project_field",
                    "prefix": "refs/tags/v",
                    "project_field": "version",
                }
                broken[field] = value
                with self.subTest(kind="ref_policy", field=field, value=repr(value)):
                    with self.assertRaises(ReleaseSpecError):
                        GitHubRefPolicy.from_dict(broken)

    def test_schema2_ref_policy_grammar_and_fixed_prefix_components(self) -> None:
        for prefix in (
            "refs/tags/.hidden",
            "refs/tags/.",
            "refs/tags/.hidden/",
            "refs/tags/foo./",
            "refs/tags/foo.lock/",
            "refs/tags/..",
        ):
            with self.subTest(prefix=prefix):
                with self.assertRaises(ReleaseSpecError):
                    GitHubRefPolicy.from_dict(
                        {
                            "kind": "prefix_project_field",
                            "prefix": prefix,
                            "project_field": "version",
                        }
                    )
        policy = GitHubRefPolicy.from_dict(
            {
                "kind": "prefix_project_field",
                "prefix": "refs/tags/foo.",
                "project_field": "version",
            }
        )
        self.assertEqual(policy.resolve({"version": "1"}), "refs/tags/foo.1")
        for ref in (
            "refs/heads/main branch",
            "refs/heads/.hidden",
            "refs/heads/release.lock",
            "refs/heads/foo/",
            "refs/heads/a//b",
            "refs/heads/a..b",
        ):
            with self.subTest(ref=ref):
                with self.assertRaises(ReleaseSpecError):
                    GitHubRefPolicy.from_dict({"kind": "exact", "ref": ref})
        for field_mutation in (
            lambda fields: fields.clear(),
            lambda fields: fields[0].update({
                "name": "version",
                "type": "integer",
                "nullable": False,
                "approval_renderable": True,
            }),
            lambda fields: fields[0].update({
                "name": "version",
                "type": "string",
                "nullable": True,
                "approval_renderable": True,
            }),
        ):
            spec_data = valid_spec()
            spec_data["schema_version"] = 2
            spec_data["operation_variants"][0]["authority_kind"] = {
                "kind": "github_run_template",
                "workflow_path": ".github/workflows/release.yml",
                "workflow_name": "Release",
                "event": "workflow_dispatch",
                "ref_policy": {
                    "kind": "prefix_project_field",
                    "prefix": "refs/tags/v",
                    "project_field": "version",
                },
            }
            field_mutation(spec_data["operation_variants"][0]["project_fields"])
            with self.assertRaises(ReleaseSpecError):
                ReleaseSpec.from_dict(spec_data)

    def test_public_exports_add_exact_runtime_template_symbols(self) -> None:
        expected = {
            "KentClient", "ApprovalMaterialization", "AuthoritySpec",
            "AuthorityTemplateSpec",
            "CanonicalizedPublicationOperation", "ExternalRoot",
            "GitHubRefPolicy",
            "JobContractTable", "NormalizedGitHubJobV1",
            "NormalizedGitHubStepV1", "NormalizedGitHubWorkflowSourceV1",
            "OperationVariant", "ProjectField", "ProjectProfile",
            "ReleaseProfile", "ReleaseSourceManifest", "ReleaseSpec",
            "ReleaseSpecError", "SelectedReleaseArtifacts",
            "SourceManifestReference", "SourceManifestSpec", "WorkKind",
            "ValidatedJobBinding", "ValidatedOperationJobs",
            "WorkflowSourceIntent", "build_canary_workflow",
            "build_delivery_workflow", "build_smoke_lab_workflow",
            "canonical_bytes", "canonical_sha256",
            "canonicalize_publication_operation",
            "operation_jobs_manifest_bytes", "operation_jobs_manifest_digest",
            "preflight_project_revision", "render_approval_summary",
            "render_release_preview", "RuntimeContractError",
            "RuntimeExecutionContext",
            "RuntimeAuthorityBinding",
            "RuntimeExternalRoot", "SelectedRuntimeSourceInputs",
            "capture_runtime_execution_context",
            "capture_runtime_authority_binding",
            "append_ci_report_attempt", "build_ci_report",
            "build_terminal_marker", "build_terminal_seal_record",
            "capture_runtime_source_envelope", "check_state_sha256",
            "classify_ci_report", "classify_expected_ci_checks",
            "classify_pr_feedback", "classify_terminal_state",
            "classify_verification_report", "expected_ci_checks_sha256",
            "make_pr_feedback_cursor", "make_report_invalid_attempt",
            "parse_runtime_external_captures", "parse_canonical_json",
            "revalidate_runtime_source_envelope", "runtime_canonical_bytes",
            "runtime_canonical_sha256", "validate_ci_report",
            "validate_ci_report_history", "validate_captured_runtime_source_envelope",
            "validate_cleanup_report", "validate_expected_ci_checks",
            "validate_pr_feedback_cursor", "validate_runtime_source_envelope",
            "validate_terminal_chain", "validate_terminal_marker",
            "validate_terminal_seal_record", "validate_terminal_seal_request",
            "validate_verification_report", "validate_approval_materialization",
            "validate_effect_job_sources", "validate_operation_jobs",
            "validate_qualification_job_sources", "validate_required_job_sources",
        }
        self.assertEqual(set(workflowkit.__all__), expected)
        self.assertNotIn("_resolve_runtime_authority_binding", workflowkit.__all__)
        self.assertNotIn("resolve_runtime_authority_binding", workflowkit.__all__)
        self.assertNotIn("resolve_runtime_authority_binding", __import__(
            "workflowkit.runtime", fromlist=["__all__"]
        ).__all__)

    def test_runtime_context_and_binding_are_sealed(self) -> None:
        inputs = _make_selected_runtime_source_inputs(
            project_name="Example",
            repository="owner/repository",
            topology_kind="appsome-release-publication",
            project_commit="a" * 40,
            source_preview={"selected": True},
            artifact_digests={
                "spec_raw_blob_sha256": "b" * 64,
                "source_manifest_raw_blob_sha256": "c" * 64,
                "snapshot_raw_blob_sha256": "d" * 64,
            },
            external_roots=(RuntimeExternalRoot("env", "release"),),
        )
        context = capture_runtime_execution_context(
            inputs,
            {
                "kind": "github_run",
                "repository": "owner/repository",
                "workflow_path": ".github/workflows/release.yml",
                "workflow_name": "Release",
                "event": "workflow_dispatch",
                "run_id": 7,
                "attempt": 1,
                "head_sha": "a" * 40,
                "ref": "refs/heads/main",
            },
        )
        authority = {
            "kind": "github_run",
            "workflow_path": ".github/workflows/release.yml",
            "workflow_name": "Release",
            "event": "workflow_dispatch",
            "run_id": 7,
            "attempt": 1,
            "head_sha": "a" * 40,
            "ref": "refs/heads/main",
        }
        binding = capture_runtime_authority_binding(
            inputs, [("env", "release", b"stable")], context, authority
        )
        self.assertIsInstance(context, RuntimeExecutionContext)
        self.assertIsInstance(binding, RuntimeAuthorityBinding)
        with self.assertRaises(TypeError):
            RuntimeExecutionContext()
        with self.assertRaises(RuntimeContractError):
            capture_runtime_authority_binding(
                inputs,
                [("env", "release", b"stable")],
                context,
                {**authority, "ref": "refs/heads/other"},
            )

    def test_sealed_objects_reject_mapping_json_foreign_and_mutation(self) -> None:
        inputs, context, binding, _authority = self._github_chain()
        with self.assertRaises(RuntimeContractError):
            capture_runtime_authority_binding(
                inputs,
                [],
                {"kind": "github_run"},
                {},
            )
        with self.assertRaises(TypeError):
            json.dumps(context)
        with self.assertRaises(TypeError):
            json.dumps(binding)
        with self.assertRaises(TypeError):
            RuntimeAuthorityBinding()
        with self.assertRaises(TypeError):
            binding.authority["run_id"] = 9
        with self.assertRaises(RuntimeContractError):
            _resolve_runtime_authority_binding(
                {},
                context,
                runtime_source_envelope_digest=binding.runtime_source_envelope_digest,
            )
        stale_context = self._github_chain()[1]
        object.__setattr__(stale_context, "execution_context_sha256", "0" * 64)
        with self.assertRaises(RuntimeContractError):
            capture_runtime_authority_binding(
                inputs,
                [],
                stale_context,
                _authority,
            )
        stale_source_context = self._github_chain()[1]
        object.__setattr__(
            stale_source_context,
            "selected_runtime_source_inputs_sha256",
            "0" * 64,
        )
        with self.assertRaises(RuntimeContractError):
            capture_runtime_authority_binding(
                inputs,
                [],
                stale_source_context,
                _authority,
            )
        stale_binding = self._github_chain()[2]
        object.__setattr__(stale_binding, "provenance_fingerprint", "0" * 64)
        with self.assertRaises(RuntimeContractError):
            _resolve_runtime_authority_binding(
                stale_binding,
                context,
                runtime_source_envelope_digest=binding.runtime_source_envelope_digest,
            )
        stale_source_binding = self._github_chain()[2]
        object.__setattr__(
            stale_source_binding,
            "selected_runtime_source_inputs_sha256",
            "0" * 64,
        )
        with self.assertRaises(RuntimeContractError):
            _resolve_runtime_authority_binding(
                stale_source_binding,
                context,
                runtime_source_envelope_digest=binding.runtime_source_envelope_digest,
            )
        runtime_path = Path(__file__).resolve().parents[1] / "workflowkit" / "runtime.py"
        spec = importlib.util.spec_from_file_location("foreign_runtime", runtime_path)
        foreign = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(foreign)
        foreign_inputs = foreign._make_selected_runtime_source_inputs(
            project_name="Example",
            repository="owner/repository",
            topology_kind="appsome-release-publication",
            project_commit="a" * 40,
            source_preview={"selected": True},
            artifact_digests={
                "spec_raw_blob_sha256": "b" * 64,
                "source_manifest_raw_blob_sha256": "c" * 64,
                "snapshot_raw_blob_sha256": "d" * 64,
            },
            external_roots=(),
        )
        foreign_context = foreign.capture_runtime_execution_context(
            foreign_inputs,
            {
                "kind": "github_run",
                "repository": "owner/repository",
                "workflow_path": ".github/workflows/release.yml",
                "workflow_name": "Release",
                "event": "workflow_dispatch",
                "run_id": 7,
                "attempt": 1,
                "head_sha": "a" * 40,
                "ref": "refs/heads/main",
            },
        )
        foreign_authority = {
            "kind": "github_run",
            "workflow_path": ".github/workflows/release.yml",
            "workflow_name": "Release",
            "event": "workflow_dispatch",
            "run_id": 7,
            "attempt": 1,
            "head_sha": "a" * 40,
            "ref": "refs/heads/main",
        }
        foreign_binding = foreign.capture_runtime_authority_binding(
            foreign_inputs,
            [],
            foreign_context,
            foreign_authority,
        )
        with self.assertRaises(RuntimeContractError):
            capture_runtime_authority_binding(
                inputs,
                [],
                foreign_context,
                _authority,
            )
        with self.assertRaises(RuntimeContractError):
            _resolve_runtime_authority_binding(
                foreign_binding,
                context,
                runtime_source_envelope_digest=binding.runtime_source_envelope_digest,
            )

    def test_runtime_proof_rejects_cross_source_and_cross_context_pairs(self) -> None:
        inputs_a, context_a, binding_a, authority_a = self._github_chain(
            commit="a" * 40,
            run_id=7,
        )
        inputs_b, context_b, _binding_b, _authority_b = self._github_chain(
            commit="b" * 40,
            run_id=8,
        )
        with self.assertRaises(RuntimeContractError):
            _resolve_runtime_authority_binding(
                binding_a,
                context_b,
                runtime_source_envelope_digest=binding_a.runtime_source_envelope_digest,
            )
        with self.assertRaises(RuntimeContractError):
            capture_runtime_authority_binding(
                inputs_b,
                [],
                context_a,
                authority_a,
            )
        with self.assertRaises(RuntimeContractError):
            capture_runtime_authority_binding(
                inputs_a,
                [],
                context_a,
                {**authority_a, "run_id": 8},
            )
        bad_execution = {
            "kind": "github_run",
            "repository": "owner/repository",
            "workflow_path": ".github/workflows/release.yml",
            "workflow_name": "Release",
            "event": "workflow_dispatch",
            "run_id": 7,
            "attempt": 1,
            "head_sha": "b" * 40,
            "ref": "refs/heads/main",
        }
        with self.assertRaises(RuntimeContractError):
            capture_runtime_execution_context(inputs_a, bad_execution)
        kent_inputs, kent_context, _kent_binding, kent_authority = self._kent_chain()
        with self.assertRaises(RuntimeContractError):
            capture_runtime_authority_binding(
                kent_inputs,
                [],
                kent_context,
                {**kent_authority, "authority_transition": "reject"},
            )

    def test_two_independent_kent_and_github_proof_chains_succeed(self) -> None:
        def schema2_spec(authority_kind: dict) -> ReleaseSpec:
            data = valid_spec()
            data["schema_version"] = 2
            data["operation_variants"][0]["authority_kind"] = authority_kind
            data["operation_variants"][0]["authority_transitions"] = (
                ["approve"] if authority_kind["kind"] == "kent_transition_template" else []
            )
            data["operation_variants"][0]["approval_required"] = False
            return ReleaseSpec.from_dict(data)

        for template, chain_factory, chain_values in (
            (
                {
                    "kind": "kent_transition_template",
                    "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
                    "project_id": "project-123e4567-e89b-12d3-a456-426614174000",
                    "approval_authority": "release-manager",
                },
                lambda values: self._kent_chain(
                    commit=values[0],
                    task_short_id=values[1],
                    workflow_revision=values[2],
                ),
                (("a" * 40, "KIT-42", 2), ("b" * 40, "KIT-43", 3)),
            ),
            (
                {
                    "kind": "github_run_template",
                    "workflow_path": ".github/workflows/release.yml",
                    "workflow_name": "Release",
                    "event": "workflow_dispatch",
                    "ref_policy": {"kind": "exact", "ref": "refs/heads/main"},
                },
                lambda commit: self._github_chain(
                    commit=commit[0],
                    ref="refs/heads/main",
                    run_id=commit[1],
                    attempt=commit[2],
                ),
                (("a" * 40, 7, 1), ("b" * 40, 8, 2)),
            ),
        ):
            spec = schema2_spec(template)
            validated = self._validated_jobs(spec)
            for values in chain_values:
                _inputs, context, binding, authority = chain_factory(values)
                operation = {
                    "schema_version": 1,
                    "variant_key": "publish",
                    "operation_kind": "publish",
                    "repository": "owner/repository",
                    "runtime_source_envelope_digest": (
                        binding.runtime_source_envelope_digest
                    ),
                    "operation_jobs_manifest_digest": (
                        validated.operation_jobs_manifest_digest
                    ),
                    "authority": authority,
                    "project_fields": {"version": "1.2.3"},
                }
                result = canonicalize_publication_operation(
                    operation,
                    spec.operation_variants[0],
                    validated,
                    spec=spec,
                    runtime_execution_context=context,
                    runtime_authority_binding=binding,
                )
                self.assertEqual(result.operation["authority"], authority)

    def test_bound_exact_prefix_ref_and_kent_transition_drift_reject(self) -> None:
        def github_spec(policy: dict) -> ReleaseSpec:
            data = valid_spec()
            data["schema_version"] = 2
            data["operation_variants"][0]["authority_kind"] = {
                "kind": "github_run_template",
                "workflow_path": ".github/workflows/release.yml",
                "workflow_name": "Release",
                "event": "workflow_dispatch",
                "ref_policy": policy,
            }
            data["operation_variants"][0]["authority_transitions"] = []
            data["operation_variants"][0]["approval_required"] = False
            return ReleaseSpec.from_dict(data)

        def canonical_operation(spec, chain):
            _inputs, context, binding, authority = chain
            validated = self._validated_jobs(spec)
            operation = {
                "schema_version": 1,
                "variant_key": "publish",
                "operation_kind": "publish",
                "repository": "owner/repository",
                "runtime_source_envelope_digest": binding.runtime_source_envelope_digest,
                "operation_jobs_manifest_digest": validated.operation_jobs_manifest_digest,
                "authority": authority,
                "project_fields": {"version": "1.2.3"},
            }
            return canonicalize_publication_operation(
                operation,
                spec.operation_variants[0],
                validated,
                spec=spec,
                runtime_execution_context=context,
                runtime_authority_binding=binding,
            )

        with self.assertRaises(ReleaseSpecError):
            canonical_operation(
                github_spec(
                    {
                        "kind": "exact",
                        "ref": "refs/heads/main",
                    }
                ),
                self._github_chain(ref="refs/heads/other"),
            )
        with self.assertRaises(ReleaseSpecError):
            canonical_operation(
                github_spec(
                    {
                        "kind": "prefix_project_field",
                        "prefix": "refs/tags/v",
                        "project_field": "version",
                    }
                ),
                self._github_chain(ref="refs/tags/v2.0.0"),
            )

        kent_data = valid_spec()
        kent_data["schema_version"] = 2
        kent_data["operation_variants"][0]["authority_kind"] = {
            "kind": "kent_transition_template",
            "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
            "project_id": "project-123e4567-e89b-12d3-a456-426614174000",
            "approval_authority": "release-manager",
        }
        kent_data["operation_variants"][0]["authority_transitions"] = ["approve"]
        kent_data["operation_variants"][0]["approval_required"] = False
        kent = ReleaseSpec.from_dict(kent_data)
        _inputs, context, binding, authority = self._kent_chain(transition="reject")
        validated = self._validated_jobs(kent)
        operation = {
            "schema_version": 1,
            "variant_key": "publish",
            "operation_kind": "publish",
            "repository": "owner/repository",
            "runtime_source_envelope_digest": binding.runtime_source_envelope_digest,
            "operation_jobs_manifest_digest": validated.operation_jobs_manifest_digest,
            "authority": authority,
            "project_fields": {"version": "1.2.3"},
        }
        with self.assertRaises(ReleaseSpecError):
            canonicalize_publication_operation(
                operation,
                kent.operation_variants[0],
                validated,
                spec=kent,
                runtime_execution_context=context,
                runtime_authority_binding=binding,
            )

    def test_canonical_json_is_sorted_and_has_no_newline(self) -> None:
        encoded = canonical_json_bytes({"z": 1, "a": True})
        self.assertEqual(encoded, b'{"a":true,"z":1}')
        self.assertEqual(sha256_digest(encoded), sha256_digest(encoded))
        self.assertNotIn(b"\n", encoded)
        source = normalized_workflow().as_dict()
        reordered = {key: source[key] for key in reversed(list(source))}
        reordered_source = NormalizedGitHubWorkflowSourceV1.from_json(
            json.dumps(reordered)
        )
        self.assertEqual(
            NormalizedGitHubWorkflowSourceV1.from_dict(source).as_dict(),
            reordered_source.as_dict(),
        )
        multi = normalized_workflow(
            jobs=[job("z-job"), job("a-job", matrix={"os": "ubuntu"})]
        ).as_dict()
        reversed_multi = deepcopy(multi)
        reversed_multi["jobs"].reverse()
        self.assertEqual(
            NormalizedGitHubWorkflowSourceV1.from_dict(multi).as_dict(),
            NormalizedGitHubWorkflowSourceV1.from_dict(reversed_multi).as_dict(),
        )
        intent_a = WorkflowSourceIntent.from_toml(
            """
            name = "Example Release"
            id = "123e4567-e89b-12d3-a456-426614174000"
            update_kind = "graph-and-metadata"
            expected_project_link = "default"
            expected_project_default = true
            allow_create = false
            allow_default_change = false
            allow_uuid_change = false
            """
        )
        intent_b = WorkflowSourceIntent.from_toml(
            """
            allow_uuid_change = false
            expected_project_default = true
            name = "Example Release"
            allow_create = false
            expected_project_link = "default"
            id = "123e4567-e89b-12d3-a456-426614174000"
            allow_default_change = false
            update_kind = "graph-and-metadata"
            """
        )
        self.assertEqual(intent_a.as_dict(), intent_b.as_dict())
        manifest = source_manifest()
        reordered_manifest = {
            key: manifest[key] for key in reversed(list(manifest))
        }
        self.assertEqual(
            ReleaseSourceManifest.from_json(json.dumps(manifest)).as_dict(),
            ReleaseSourceManifest.from_json(json.dumps(reordered_manifest)).as_dict(),
        )

    def test_closed_spec_and_full_contract_rows(self) -> None:
        spec = ReleaseSpec.from_dict(valid_spec())
        self.assertEqual(spec.operation_variants[0].key, "publish")
        broken = valid_spec()
        del broken["required_jobs_v1"]["jobs"][0]["effective_permissions"]
        with self.assertRaisesRegex(ReleaseSpecError, "missing keys"):
            ReleaseSpec.from_dict(broken)

    def test_workflow_source_intent_is_workflow_identity_not_project_identity(self) -> None:
        data = valid_spec()
        data["workflow_source_intent"]["name"] = "A Different Workflow"
        self.assertEqual(ReleaseSpec.from_dict(data).workflow_source_intent.name, "A Different Workflow")
        data["workflow_source_intent"]["expected_project_link"] = "default"
        data["workflow_source_intent"]["expected_project_default"] = False
        with self.assertRaises(ReleaseSpecError):
            ReleaseSpec.from_dict(data)
        data["workflow_source_intent"]["expected_project_default"] = True
        self.assertTrue(ReleaseSpec.from_dict(data).workflow_source_intent.expected_project_default)

    def test_spec_rejects_unknown_refs_and_global_duplicates(self) -> None:
        broken = valid_spec()
        broken["operation_variants"][0]["effect_job_contract_keys"] = ["missing"]
        with self.assertRaises(ReleaseSpecError):
            ReleaseSpec.from_dict(broken)
        broken = valid_spec()
        duplicate = deepcopy(broken["required_jobs_v1"]["jobs"][0])
        duplicate["contract_key"] = "effect_duplicate"
        broken["effect_jobs_v1"]["jobs"].append(duplicate)
        with self.assertRaises(ReleaseSpecError):
            ReleaseSpec.from_dict(broken)

    def test_normalized_boundary_rejects_mappings_and_unexpanded_matrix(self) -> None:
        workflow = normalized_workflow()
        with self.assertRaises(ReleaseSpecError):
            validate_required_job_sources(workflow.as_dict(), valid_spec()["required_jobs_v1"])
        raw = workflow.as_dict()
        raw["jobs"][0]["matrix"] = {"os": ["ubuntu", "windows"]}
        with self.assertRaises(ReleaseSpecError):
            NormalizedGitHubWorkflowSourceV1.from_dict(raw)

    def test_project_shaped_normalized_goldens(self) -> None:
        goldens = [
            {
                "name": "appsome",
                "event": "push",
                "required": job(
                    "detekt",
                    permissions={"contents": "read", "packages": "read"},
                    runs_on="ubuntu-latest-8-cores",
                    steps=[
                        step(
                            uses="gradle/actions/setup-gradle@" + "a" * 40,
                            with_values={"cache-read-only": True},
                        )
                    ],
                ),
                "qualification": job("unit-tests"),
                "effect": job(
                    "trusted-build",
                    permissions={"contents": "write"},
                    runs_on="ubuntu-latest-8-cores",
                    secret_refs=["TOKEN"],
                    steps=[
                        step(
                            uses="actions/upload-artifact@" + "b" * 40,
                            with_values={
                                "name": "apk",
                                "retention-days": 1,
                                "token": "${{ secrets.TOKEN }}",
                            },
                            secret_refs=["TOKEN"],
                        )
                    ],
                ),
                "effect_trust": "github-hosted-larger-ephemeral-effect",
            },
            {
                "name": "puber",
                "event": "workflow_dispatch",
                "required": job(
                    "unit-tests",
                    steps=[
                        step(
                            uses="gradle/actions/setup-gradle@" + "c" * 40,
                            with_values={"gradle-home-cache-cleanup": True},
                        )
                    ],
                ),
                "qualification": None,
                "effect": job(
                    "release",
                    permissions={"contents": "write"},
                    condition="qualified event",
                    steps=[
                        step(
                            uses="softprops/action-gh-release@" + "d" * 40,
                            with_values={
                                "generate_release_notes": False,
                                "token": "${{ secrets.RELEASE_TOKEN }}",
                            },
                            secret_refs=["RELEASE_TOKEN"],
                        )
                    ],
                    secret_refs=["RELEASE_TOKEN"],
                ),
                "effect_trust": "github-hosted-standard-ephemeral-effect",
            },
            {
                "name": "sdk",
                "event": "push",
                "effect_tags": ["v*"],
                "required": job(
                    "unit-tests",
                    matrix={"os": "ubuntu-latest"},
                    permissions={"contents": "read", "packages": "read"},
                ),
                "qualification": job("quality"),
                "effect": job(
                    "create-update-spec-pull-request",
                    runs_on="arc-runner-light",
                    condition="bot branch event",
                    secret_refs=["OSOME_BOT_TOKEN"],
                ),
                "effect_trust": "organization-arc-ephemeral-effect",
            },
            {
                "name": "slack",
                "event": "schedule",
                "required": job(
                    "test",
                    steps=[
                        step(
                            uses="actions/checkout@" + "e" * 40,
                            effective_environment={"MODE": "strict"},
                        )
                    ],
                ),
                "qualification": None,
                "effect": job(
                    "publish",
                    condition="scheduled release",
                    runs_on="arc-runner-light",
                    steps=[
                        step(
                            uses="archive/github-actions-slack@" + "f" * 40,
                            with_values={
                                "channel": "releases",
                                "token": "${{ secrets.SLACK_TOKEN }}",
                            },
                            secret_refs=["SLACK_TOKEN"],
                        )
                    ],
                    secret_refs=["SLACK_TOKEN"],
                ),
                "effect_trust": "organization-arc-ephemeral-effect",
            },
        ]
        for golden in goldens:
            required_path = f".github/workflows/{golden['name']}-required.yml"
            effect_path = f".github/workflows/{golden['name']}-effect.yml"
            jobs = [golden["required"]]
            if golden["name"] == "sdk":
                expanded = deepcopy(golden["required"])
                expanded["matrix"] = {"os": "macos-latest"}
                jobs.append(expanded)
            if golden["qualification"] is not None:
                jobs.append(golden["qualification"])
            required_raw = normalized_workflow(path=required_path, jobs=jobs).as_dict()
            required_raw["workflow_display_name"] = golden["name"].title()
            required_raw["events"][0] = event_record("pull_request")
            required_parsed = NormalizedGitHubWorkflowSourceV1.from_dict(required_raw)
            effect_event = event_record(
                golden["event"],
                tags=golden.get("effect_tags"),
            )
            effect_raw = normalized_workflow(
                path=effect_path,
                jobs=[golden["effect"]],
            ).as_dict()
            effect_raw["workflow_display_name"] = golden["name"].title()
            effect_raw["events"][0] = effect_event
            effect_parsed = NormalizedGitHubWorkflowSourceV1.from_dict(effect_raw)
            sources = [required_parsed, effect_parsed]
            required_table = {
                "schema": "required_jobs_v1",
                "jobs": [
                    contract_row(
                        "required",
                        f"{golden['name']}-required",
                        golden["required"],
                        event_selector=event_record("pull_request"),
                        workflow_path=required_path,
                    )
                ],
            }
            validate_required_job_sources(sources, required_table)
            if golden["qualification"] is not None:
                qualification_table = {
                    "schema": "qualification_jobs_v1",
                    "jobs": [
                        contract_row(
                            "qualification",
                            f"{golden['name']}-qualification",
                            golden["qualification"],
                            event_selector=event_record("pull_request"),
                            workflow_path=required_path,
                        )
                    ],
                }
                validate_qualification_job_sources(sources, qualification_table)
            effect_table = {
                "schema": "effect_jobs_v1",
                "jobs": [
                    contract_row(
                        "effect",
                        f"{golden['name']}-effect",
                        golden["effect"],
                        event_selector=effect_event,
                        workflow_path=effect_path,
                        runner_trust=golden["effect_trust"],
                        credential_profile=f"{golden['name']}-effect-credentials",
                        allowed_effects=["release-publish"],
                        skip_policy=(
                            "event-gated"
                            if not golden["effect"]["condition"]
                            else "condition-gated"
                        ),
                    )
                ],
            }
            validate_effect_job_sources(sources, effect_table)

    def test_action_shape_and_immutable_refs(self) -> None:
        accepted = normalized_workflow(
            jobs=[
                job("required_release", validation_required=True),
            ]
        ).as_dict()
        accepted["jobs"][0]["steps"][0] = step(
            uses="gradle/actions/setup-gradle@" + "a" * 40,
        )
        NormalizedGitHubWorkflowSourceV1.from_dict(accepted)
        accepted["jobs"][0]["steps"][0]["run"] = "echo bad"
        with self.assertRaises(ReleaseSpecError):
            NormalizedGitHubWorkflowSourceV1.from_dict(accepted)

    def test_nested_secrets_require_exact_accounting(self) -> None:
        raw = normalized_workflow().as_dict()
        raw["jobs"][0]["steps"][0]["run"] = "echo ${{ secrets.TOKEN }}"
        with self.assertRaises(ReleaseSpecError):
            NormalizedGitHubWorkflowSourceV1.from_dict(raw)
        raw["jobs"][0]["steps"][0]["secret_refs"] = ["TOKEN"]
        raw["jobs"][0]["secret_refs"] = ["TOKEN"]
        NormalizedGitHubWorkflowSourceV1.from_dict(raw)
        raw["jobs"][0]["condition"] = "github.ref == '${{ secrets.BRANCH }}'"
        with self.assertRaises(ReleaseSpecError):
            NormalizedGitHubWorkflowSourceV1.from_dict(raw)
        raw["jobs"][0]["secret_refs"] = ["BRANCH", "TOKEN"]
        NormalizedGitHubWorkflowSourceV1.from_dict(raw)
        raw["jobs"][0]["job_display_name"] = "uses ${{ secrets.DISPLAY }}"
        raw["jobs"][0]["secret_refs"] = ["BRANCH", "DISPLAY", "TOKEN"]
        NormalizedGitHubWorkflowSourceV1.from_dict(raw)
        raw["jobs"][0]["steps"][0]["name"] = "uses ${{ secrets.STEP_NAME }}"
        raw["jobs"][0]["secret_refs"] = [
            "BRANCH",
            "DISPLAY",
            "STEP_NAME",
            "TOKEN",
        ]
        with self.assertRaises(ReleaseSpecError):
            NormalizedGitHubWorkflowSourceV1.from_dict(raw)
        raw["jobs"][0]["steps"][0]["secret_refs"] = [
            "STEP_NAME",
            "TOKEN",
        ]
        NormalizedGitHubWorkflowSourceV1.from_dict(raw)

    def test_immutable_images_defaults_continue_flags_and_action_inputs(self) -> None:
        raw = normalized_workflow(
            jobs=[
                job(
                    "unit-tests",
                    steps=[
                        step(
                            with_values={
                                "count": 1,
                                "enabled": True,
                                "text": "ok",
                            }
                        )
                    ],
                )
            ]
        ).as_dict()
        raw["defaults_run"] = {
            "shell": "bash",
            "working_directory": "project",
        }
        raw["jobs"][0]["effective_defaults_run"] = {
            "shell": "bash",
            "working_directory": "project/tests",
        }
        raw["jobs"][0]["services"] = {
            "database": {
                "image": "postgres@sha256:" + "a" * 64,
                "environment": {},
                "ports": ["5432"],
                "options": "",
            }
        }
        raw["jobs"][0]["container"] = {
            "image": "ubuntu@sha256:" + "b" * 64,
            "environment": {},
            "ports": [],
            "options": "",
        }
        parsed = NormalizedGitHubWorkflowSourceV1.from_dict(raw)
        self.assertEqual(parsed.defaults_run["working_directory"], "project")
        self.assertEqual(
            parsed.jobs[0].effective_defaults_run["working_directory"],
            "project/tests",
        )
        mutable = deepcopy(raw)
        mutable["jobs"][0]["container"]["image"] = "ubuntu:latest"
        with self.assertRaises(ReleaseSpecError):
            NormalizedGitHubWorkflowSourceV1.from_dict(mutable)
        mutable = deepcopy(raw)
        mutable["jobs"][0]["services"]["database"]["image"] = "postgres:latest"
        with self.assertRaises(ReleaseSpecError):
            NormalizedGitHubWorkflowSourceV1.from_dict(mutable)
        invalid_input = deepcopy(raw)
        invalid_input["jobs"][0]["steps"][0]["with"]["null"] = None
        with self.assertRaises(ReleaseSpecError):
            NormalizedGitHubWorkflowSourceV1.from_dict(invalid_input)
        invalid_input = deepcopy(raw)
        invalid_input["jobs"][0]["steps"][0]["with"]["float"] = 1.25
        with self.assertRaises(ReleaseSpecError):
            NormalizedGitHubWorkflowSourceV1.from_dict(invalid_input)
        invalid_action = deepcopy(raw)
        invalid_action["jobs"][0]["steps"][0] = step(uses="actions/checkout@v4")
        with self.assertRaises(ReleaseSpecError):
            NormalizedGitHubWorkflowSourceV1.from_dict(invalid_action)
        invalid_action["jobs"][0]["steps"][0] = step(uses="./.github/workflows/reuse.yml@" + "a" * 40)
        with self.assertRaises(ReleaseSpecError):
            NormalizedGitHubWorkflowSourceV1.from_dict(invalid_action)
        invalid_action["jobs"][0]["steps"][0] = step(
            uses="owner/repo/.github/workflows/reuse.yml@" + "a" * 40
        )
        with self.assertRaises(ReleaseSpecError):
            NormalizedGitHubWorkflowSourceV1.from_dict(invalid_action)
        root_continue = deepcopy(raw)
        root_continue["continue_on_error"] = False
        with self.assertRaises(ReleaseSpecError):
            NormalizedGitHubWorkflowSourceV1.from_dict(root_continue)
        source_job = job("required_release", validation_required=True)
        table = {
            "schema": "required_jobs_v1",
            "jobs": [contract_row("required", "continue", source_job)],
        }
        job_continue = deepcopy(source_job)
        job_continue["continue_on_error"] = True
        job_continue_table = {
            "schema": "required_jobs_v1",
            "jobs": [contract_row("required", "continue", job_continue)],
        }
        with self.assertRaisesRegex(ReleaseSpecError, "continue on error"):
            validate_required_job_sources(
                normalized_workflow(jobs=[job_continue]),
                job_continue_table,
            )
        step_continue = deepcopy(source_job)
        step_continue["steps"][0]["continue_on_error"] = True
        step_continue_table = {
            "schema": "required_jobs_v1",
            "jobs": [contract_row("required", "continue", step_continue)],
        }
        with self.assertRaisesRegex(ReleaseSpecError, "failure-masking step"):
            validate_required_job_sources(
                normalized_workflow(jobs=[step_continue]),
                step_continue_table,
            )
        table = {
            "schema": "required_jobs_v1",
            "jobs": [contract_row("required", "checkout-drift", source_job)],
        }
        drifted = normalized_workflow(
            jobs=[
                {
                    **source_job,
                    "checkout_persist_credentials": True,
                }
            ]
        )
        with self.assertRaises(ReleaseSpecError):
            validate_required_job_sources(drifted, table)

    def test_advisory_effect_step_overlay_is_sparse_and_fail_closed(self) -> None:
        effect_job = job(
            "publish_release",
            condition="github.event_name == 'workflow_dispatch'",
            permissions={"contents": "write"},
            steps=[step(run="echo optional", continue_on_error=True)],
        )
        source = normalized_workflow(
            jobs=[
                job("required_release", validation_required=True),
                effect_job,
            ]
        )
        unmarked = {
            "schema": "effect_jobs_v1",
            "jobs": [
                contract_row("effect", "publish_release_contract", effect_job)
            ],
        }
        with self.assertRaisesRegex(ReleaseSpecError, "failure-masking step"):
            validate_effect_job_sources(source, unmarked)

        marked = deepcopy(unmarked)
        marked["jobs"][0]["steps"][0]["advisory_effect"] = True
        spec_data = valid_spec()
        spec_data["effect_jobs_v1"] = marked
        spec = ReleaseSpec.from_dict(spec_data)
        effect_contract = spec.as_dict()["effect_jobs_v1"]["jobs"][0]["steps"][0]
        self.assertIn(
            "advisory_effect",
            effect_contract,
        )
        self.assertTrue(effect_contract["advisory_effect"])
        self.assertNotIn("advisory_effect", effect_job["steps"][0])
        legacy_spec = ReleaseSpec.from_dict(valid_spec())
        self.assertNotIn(
            "advisory_effect",
            legacy_spec.as_dict()["effect_jobs_v1"]["jobs"][0]["steps"][0],
        )
        validated = validate_operation_jobs(
            spec.operation_variants[0],
            source,
            required=spec.required_jobs_v1,
            qualification=spec.qualification_jobs_v1,
            effect=spec.effect_jobs_v1,
        )
        self.assertEqual(
            validated.effect[0].as_dict()["step_contract"],
            [
                {
                    "step_index": 0,
                    "validation_required": False,
                    "advisory_effect": True,
                }
            ],
        )
        self.assertNotIn("advisory_effect", validated.effect[0].job["steps"][0])
        effect_manifest = next(
            item
            for item in validated.operation_jobs_manifest["bindings"]
            if item["set_kind"] == "effect"
        )
        self.assertEqual(
            effect_manifest["step_contract"],
            validated.effect[0].as_dict()["step_contract"],
        )
        legacy = self._validated_jobs(legacy_spec)
        legacy_repeat = self._validated_jobs(ReleaseSpec.from_dict(valid_spec()))
        self.assertEqual(
            legacy.operation_jobs_manifest_digest,
            legacy_repeat.operation_jobs_manifest_digest,
        )
        self.assertNotEqual(
            validated.operation_jobs_manifest_digest,
            legacy.operation_jobs_manifest_digest,
        )
        manifest_without_marker = deepcopy(validated.operation_jobs_manifest)
        next(
            item
            for item in manifest_without_marker["bindings"]
            if item["set_kind"] == "effect"
        )["step_contract"][0].pop(
            "advisory_effect"
        )
        self.assertNotEqual(
            validated.operation_jobs_manifest_digest,
            sha256_digest(manifest_without_marker),
        )

        drifted = deepcopy(source.as_dict())
        next(
            item
            for item in drifted["jobs"]
            if item["job_key"] == "publish_release"
        )["steps"][0]["continue_on_error"] = False
        with self.assertRaisesRegex(ReleaseSpecError, "normalized source drift"):
            validate_effect_job_sources(
                NormalizedGitHubWorkflowSourceV1.from_dict(drifted),
                marked,
            )

        noncontinued = deepcopy(marked)
        noncontinued["jobs"][0]["steps"][0]["advisory_effect"] = True
        noncontinued["jobs"][0]["steps"][0]["continue_on_error"] = False
        noncontinued_source_job = deepcopy(effect_job)
        noncontinued_source_job["steps"][0]["continue_on_error"] = False
        with self.assertRaisesRegex(
            ReleaseSpecError,
            "requires continue_on_error",
        ):
            validate_effect_job_sources(
                normalized_workflow(
                    jobs=[
                        job("required_release", validation_required=True),
                        noncontinued_source_job,
                    ]
                ),
                noncontinued,
            )

        validation_required = deepcopy(marked)
        validation_required["jobs"][0]["steps"][0]["validation_required"] = True
        with self.assertRaisesRegex(
            ReleaseSpecError,
            "may not be validation_required",
        ):
            validate_effect_job_sources(source, validation_required)

        false_marker = deepcopy(marked)
        false_marker["jobs"][0]["steps"][0]["advisory_effect"] = False
        with self.assertRaisesRegex(ReleaseSpecError, "must be true"):
            validate_effect_job_sources(source, false_marker)

        unknown_marker = deepcopy(marked)
        unknown_marker["jobs"][0]["steps"][0]["unexpected"] = True
        with self.assertRaisesRegex(ReleaseSpecError, "unknown keys"):
            validate_effect_job_sources(source, unknown_marker)

        required_marker = contract_row(
            "required",
            "required-marker",
            job("required_release", validation_required=True),
        )
        required_marker["steps"][0]["advisory_effect"] = True
        with self.assertRaisesRegex(ReleaseSpecError, "unknown keys"):
            validate_required_job_sources(
                normalized_workflow(
                    jobs=[job("required_release", validation_required=True)]
                ),
                {"schema": "required_jobs_v1", "jobs": [required_marker]},
            )

        qualification_job = job("unit_tests")
        qualification_marker = contract_row(
            "qualification",
            "qualification-marker",
            qualification_job,
        )
        qualification_marker["steps"][0]["advisory_effect"] = True
        with self.assertRaisesRegex(ReleaseSpecError, "unknown keys"):
            validate_qualification_job_sources(
                normalized_workflow(jobs=[qualification_job]),
                {
                    "schema": "qualification_jobs_v1",
                    "jobs": [qualification_marker],
                },
            )

        two_step_job = job(
            "publish_release",
            condition="github.event_name == 'workflow_dispatch'",
            permissions={"contents": "write"},
            steps=[
                step(run="echo optional", continue_on_error=True),
                step(run="echo also optional", continue_on_error=True),
            ],
        )
        two_step = contract_row("effect", "two-step", two_step_job)
        two_step["steps"][0]["advisory_effect"] = True
        with self.assertRaisesRegex(ReleaseSpecError, "failure-masking step"):
            validate_effect_job_sources(
                normalized_workflow(
                    jobs=[
                        job("required_release", validation_required=True),
                        two_step_job,
                    ]
                ),
                {"schema": "effect_jobs_v1", "jobs": [two_step]},
            )

        job_continue = deepcopy(effect_job)
        job_continue["continue_on_error"] = True
        job_continue_contract = contract_row(
            "effect",
            "job-continue",
            job_continue,
        )
        with self.assertRaisesRegex(ReleaseSpecError, "continue on error"):
            validate_effect_job_sources(
                normalized_workflow(
                    jobs=[
                        job("required_release", validation_required=True),
                        job_continue,
                    ]
                ),
                {
                    "schema": "effect_jobs_v1",
                    "jobs": [job_continue_contract],
                },
            )

    def test_package_read_profile_and_cache_binding(self) -> None:
        profile = "github-packages-classic-pat-step-read"
        required_effects = ["dependency-downloads", "github-package-read"]
        cache_effects = [
            "dependency-downloads",
            "github-actions-cache-read",
            "github-package-read",
        ]

        def source_for(
            event_name: str,
            source_job: dict,
            *,
            workflow_environment: dict | None = None,
        ) -> NormalizedGitHubWorkflowSourceV1:
            raw = normalized_workflow(
                environment=workflow_environment,
                jobs=[source_job],
            ).as_dict()
            raw["events"] = [event_record(event_name)]
            return NormalizedGitHubWorkflowSourceV1.from_dict(raw)

        def row_for(
            set_kind: str,
            source_job: dict,
            *,
            event_name: str = "pull_request",
            effects: list[str] | None = None,
            credential_profile: str = profile,
            scope: bool = True,
        ) -> dict:
            row = contract_row(
                set_kind,
                f"{set_kind}-{source_job['job_key']}-{event_name}",
                source_job,
                event_selector=event_record(event_name),
                credential_profile=credential_profile,
                allowed_effects=effects or required_effects,
            )
            row["credential_scope_is_job_local"] = scope
            return row

        required_job = package_read_job("required_release")
        required_table = {
            "schema": "required_jobs_v1",
            "jobs": [row_for("required", required_job)],
        }
        self.assertEqual(
            len(
                validate_required_job_sources(
                    source_for("pull_request", required_job),
                    required_table,
                )
            ),
            1,
        )
        for event_name in (
            "pull_request",
            "push",
            "deployment",
            "workflow_dispatch",
        ):
            qualification_job = package_read_job("unit_tests")
            qualification_table = {
                "schema": "qualification_jobs_v1",
                "jobs": [
                    row_for(
                        "qualification",
                        qualification_job,
                        event_name=event_name,
                    )
                ],
            }
            self.assertEqual(
                len(
                    validate_qualification_job_sources(
                        source_for(event_name, qualification_job),
                        qualification_table,
                    )
                ),
                1,
            )
        for event_name in ("push", "deployment", "workflow_dispatch"):
            broken_required = package_read_job("required_release")
            with self.assertRaisesRegex(
                ReleaseSpecError,
                "pull_request or merge_group",
            ):
                validate_required_job_sources(
                    source_for(event_name, broken_required),
                    {
                        "schema": "required_jobs_v1",
                        "jobs": [
                            row_for(
                                "required",
                                broken_required,
                                event_name=event_name,
                            )
                        ],
                    },
                )

        old_profile_job = package_read_job("required_release")
        with self.assertRaisesRegex(ReleaseSpecError, "may not use secrets"):
            validate_required_job_sources(
                source_for("pull_request", old_profile_job),
                {
                    "schema": "required_jobs_v1",
                    "jobs": [
                        row_for(
                            "required",
                            old_profile_job,
                            credential_profile="github-platform-contents-read",
                            scope=False,
                        )
                    ],
                },
            )

        secret_free = job(
            "required_release",
            permissions={"contents": "read", "packages": "read"},
        )
        with self.assertRaisesRegex(ReleaseSpecError, "exactly one secret"):
            validate_required_job_sources(
                source_for("pull_request", secret_free),
                {
                    "schema": "required_jobs_v1",
                    "jobs": [row_for("required", secret_free)],
                },
            )
        missing_scope = package_read_job("required_release")
        with self.assertRaisesRegex(ReleaseSpecError, "must be job-local"):
            validate_required_job_sources(
                source_for("pull_request", missing_scope),
                {
                    "schema": "required_jobs_v1",
                    "jobs": [
                        row_for(
                            "required",
                            missing_scope,
                            scope=False,
                        )
                    ],
                },
            )

        with self.assertRaises(ReleaseSpecError):
            source_for(
                "pull_request",
                package_read_job("required_release"),
                workflow_environment={
                    "GITHUB_PACKAGES_TOKEN": PACKAGE_READ_SECRET_EXPRESSION,
                },
            )
        job_environment = package_read_job(
            "required_release",
            effective_environment={
                "PACKAGE_TOKEN": PACKAGE_READ_SECRET_EXPRESSION,
            },
        )
        with self.assertRaisesRegex(ReleaseSpecError, "step-scoped"):
            validate_required_job_sources(
                source_for("pull_request", job_environment),
                {
                    "schema": "required_jobs_v1",
                    "jobs": [row_for("required", job_environment)],
                },
            )

        forbidden_fields = {
            "name": lambda item: item.__setitem__(
                "name",
                PACKAGE_READ_SECRET_EXPRESSION,
            ),
            "condition": lambda item: item.__setitem__(
                "condition",
                PACKAGE_READ_SECRET_EXPRESSION,
            ),
            "run": lambda item: item.__setitem__(
                "run",
                f"echo {PACKAGE_READ_SECRET_EXPRESSION}",
            ),
            "effective_shell": lambda item: item.__setitem__(
                "effective_shell",
                PACKAGE_READ_SECRET_EXPRESSION,
            ),
            "effective_working_directory": lambda item: item.__setitem__(
                "effective_working_directory",
                PACKAGE_READ_SECRET_EXPRESSION,
            ),
            "with": lambda item: item.__setitem__(
                "with",
                {"token": PACKAGE_READ_SECRET_EXPRESSION},
            ),
        }
        for field_name, mutate in forbidden_fields.items():
            broken = package_read_job("unit_tests")
            mutate(broken["steps"][0])
            with self.subTest(field=field_name):
                with self.assertRaisesRegex(
                    ReleaseSpecError,
                    "outside its environment recipient",
                ):
                    validate_qualification_job_sources(
                        source_for("pull_request", broken),
                        {
                            "schema": "qualification_jobs_v1",
                            "jobs": [row_for("qualification", broken)],
                        },
                    )

        another_environment_key = package_read_job("required_release")
        another_environment_key["steps"][0]["effective_environment"][
            "OTHER_TOKEN"
        ] = PACKAGE_READ_SECRET_EXPRESSION
        with self.assertRaisesRegex(ReleaseSpecError, "invalid package-read"):
            validate_required_job_sources(
                source_for("pull_request", another_environment_key),
                {
                    "schema": "required_jobs_v1",
                    "jobs": [row_for("required", another_environment_key)],
                },
            )

        another_step = package_read_job(
            "required_release",
            extra_steps=[
                step(
                    run="echo second",
                    secret_refs=[PACKAGE_READ_SECRET],
                    effective_environment={
                        PACKAGE_READ_SECRET: PACKAGE_READ_SECRET_EXPRESSION,
                    },
                )
            ],
        )
        with self.assertRaisesRegex(ReleaseSpecError, "exactly one recipient"):
            validate_required_job_sources(
                source_for("pull_request", another_step),
                {
                    "schema": "required_jobs_v1",
                    "jobs": [row_for("required", another_step)],
                },
            )

        multiple_secrets = package_read_job("required_release")
        multiple_secrets["steps"][0]["effective_environment"]["OTHER_TOKEN"] = (
            "${{ secrets.OTHER_TOKEN }}"
        )
        multiple_secrets["steps"][0]["secret_refs"] = [
            "GITHUB_PACKAGES_TOKEN",
            "OTHER_TOKEN",
        ]
        multiple_secrets["secret_refs"] = [
            "GITHUB_PACKAGES_TOKEN",
            "OTHER_TOKEN",
        ]
        with self.assertRaisesRegex(ReleaseSpecError, "exactly one secret"):
            validate_required_job_sources(
                source_for("pull_request", multiple_secrets),
                {
                    "schema": "required_jobs_v1",
                    "jobs": [row_for("required", multiple_secrets)],
                },
            )

        uses_recipient = package_read_job("required_release")
        uses_recipient["steps"][0] = step(
            uses="actions/checkout@" + "b" * 40,
            secret_refs=[PACKAGE_READ_SECRET],
            effective_environment={
                PACKAGE_READ_SECRET: PACKAGE_READ_SECRET_EXPRESSION,
            },
        )
        with self.assertRaisesRegex(ReleaseSpecError, "first-party recipient"):
            validate_required_job_sources(
                source_for("pull_request", uses_recipient),
                {
                    "schema": "required_jobs_v1",
                    "jobs": [row_for("required", uses_recipient)],
                },
            )

        action_input_secret = package_read_job(
            "unit_tests",
            extra_steps=[
                step(
                    uses="actions/checkout@" + "d" * 40,
                    with_values={"token": PACKAGE_READ_SECRET_EXPRESSION},
                    secret_refs=[PACKAGE_READ_SECRET],
                )
            ],
        )
        with self.assertRaisesRegex(
            ReleaseSpecError,
            "outside its environment recipient",
        ):
            validate_qualification_job_sources(
                source_for("pull_request", action_input_secret),
                {
                    "schema": "qualification_jobs_v1",
                    "jobs": [row_for("qualification", action_input_secret)],
                },
            )

        effect_job = package_read_job("publish_release")
        with self.assertRaisesRegex(ReleaseSpecError, "only valid"):
            validate_effect_job_sources(
                source_for("workflow_dispatch", effect_job),
                {
                    "schema": "effect_jobs_v1",
                    "jobs": [
                        row_for(
                            "effect",
                            effect_job,
                            event_name="workflow_dispatch",
                            effects=["publish"],
                        )
                    ],
                },
            )

        inherited_policy_cases = (
            (
                "self-hosted",
                lambda item: item.__setitem__("runs_on", "self-hosted"),
                "GitHub-hosted runner",
            ),
            (
                "write permission",
                lambda item: item.__setitem__(
                    "effective_permissions",
                    {"contents": "write", "packages": "read"},
                ),
                "non-read permission",
            ),
            (
                "needs",
                lambda item: item.__setitem__("needs", ["other"]),
                "may not have needs",
            ),
            (
                "fixture",
                lambda item: item.__setitem__(
                    "container",
                    {
                        "image": "ubuntu@sha256:" + "a" * 64,
                        "environment": {},
                        "ports": [],
                        "options": "",
                    },
                ),
                "forbidden runtime fixtures",
            ),
            (
                "persisted credentials",
                lambda item: item.__setitem__(
                    "checkout_persist_credentials",
                    True,
                ),
                "persists checkout credentials",
            ),
        )
        for case_name, mutate, message in inherited_policy_cases:
            broken = package_read_job("required_release")
            mutate(broken)
            with self.subTest(policy=case_name):
                with self.assertRaisesRegex(ReleaseSpecError, message):
                    validate_required_job_sources(
                        source_for("pull_request", broken),
                        {
                            "schema": "required_jobs_v1",
                            "jobs": [row_for("required", broken)],
                        },
                    )

        source_drift_job = package_read_job("required_release")
        source_drift_row = row_for("required", source_drift_job)
        source_drift = source_for("pull_request", source_drift_job).as_dict()
        source_drift["jobs"][0]["steps"][0]["run"] = "echo drift"
        with self.assertRaisesRegex(ReleaseSpecError, "normalized source drift"):
            validate_required_job_sources(
                NormalizedGitHubWorkflowSourceV1.from_dict(source_drift),
                {"schema": "required_jobs_v1", "jobs": [source_drift_row]},
            )

        cache_job = package_read_job(
            "required_release",
            extra_steps=[step(uses=CACHE_RESTORE_ACTION)],
        )
        self.assertEqual(
            len(
                validate_required_job_sources(
                    source_for("pull_request", cache_job),
                    {
                        "schema": "required_jobs_v1",
                        "jobs": [
                            row_for(
                                "required",
                                cache_job,
                                effects=cache_effects,
                            )
                        ],
                    },
                )
            ),
            1,
        )

        old_cache_job = job(
            "required_release",
            permissions={"contents": "read", "packages": "read"},
            steps=[step(uses=CACHE_RESTORE_ACTION)],
        )
        self.assertEqual(
            len(
                validate_required_job_sources(
                    source_for("pull_request", old_cache_job),
                    {
                        "schema": "required_jobs_v1",
                        "jobs": [
                            row_for(
                                "required",
                                old_cache_job,
                                effects=[
                                    "dependency-downloads",
                                    "github-actions-cache-read",
                                    "github-actions-logs",
                                    "github-package-read",
                                ],
                                credential_profile=(
                                    "github-platform-contents-packages-read"
                                ),
                                scope=False,
                            )
                        ],
                    },
                )
            ),
            1,
        )

        cache_absent = package_read_job("required_release")
        with self.assertRaisesRegex(ReleaseSpecError, "requires a cache restore"):
            validate_required_job_sources(
                source_for("pull_request", cache_absent),
                {
                    "schema": "required_jobs_v1",
                    "jobs": [
                        row_for(
                            "required",
                            cache_absent,
                            effects=cache_effects,
                        )
                    ],
                },
            )
        restore_without_effect = package_read_job(
            "required_release",
            extra_steps=[step(uses=CACHE_RESTORE_ACTION)],
        )
        with self.assertRaisesRegex(ReleaseSpecError, "requires"):
            validate_required_job_sources(
                source_for("pull_request", restore_without_effect),
                {
                    "schema": "required_jobs_v1",
                    "jobs": [
                        row_for(
                            "required",
                            restore_without_effect,
                            effects=required_effects,
                        )
                    ],
                },
            )

        cache_shapes = (
            "actions/cache/save@" + "c" * 40,
            "actions/cache@" + "c" * 40,
            "actions/cache/restore/other@" + "c" * 40,
            "actions/cache/restore@v4",
        )
        for cache_shape in cache_shapes:
            broken_cache = package_read_job(
                "required_release",
                extra_steps=[step(uses=cache_shape)],
            )
            with self.subTest(cache_shape=cache_shape):
                with self.assertRaises(ReleaseSpecError):
                    validate_required_job_sources(
                        source_for("pull_request", broken_cache),
                        {
                            "schema": "required_jobs_v1",
                            "jobs": [
                                row_for(
                                    "required",
                                    broken_cache,
                                    effects=cache_effects,
                                )
                            ],
                        },
                    )

        disallowed_cache_effect = package_read_job("required_release")
        with self.assertRaisesRegex(ReleaseSpecError, "unsupported effect"):
            validate_required_job_sources(
                source_for("pull_request", disallowed_cache_effect),
                {
                    "schema": "required_jobs_v1",
                    "jobs": [
                        row_for(
                            "required",
                            disallowed_cache_effect,
                            effects=[
                                "dependency-downloads",
                                "github-actions-cache-read-write",
                                "github-package-read",
                            ],
                        )
                    ],
                },
            )

    def test_expanded_matrix_rows_have_pairwise_identity(self) -> None:
        raw = normalized_workflow(
            jobs=[
                job("required_release", matrix={"os": "ubuntu-latest"}),
                job("required_release", matrix={"os": "windows-latest"}),
            ]
        ).as_dict()
        parsed = NormalizedGitHubWorkflowSourceV1.from_dict(raw)
        self.assertEqual(len(parsed.jobs), 2)
        raw["jobs"].append(deepcopy(raw["jobs"][0]))
        with self.assertRaises(ReleaseSpecError):
            NormalizedGitHubWorkflowSourceV1.from_dict(raw)

    def test_event_and_input_semantics_are_closed(self) -> None:
        raw = normalized_workflow().as_dict()
        raw["events"][0]["name"] = "not-a-github-event"
        with self.assertRaises(ReleaseSpecError):
            NormalizedGitHubWorkflowSourceV1.from_dict(raw)
        raw = normalized_workflow().as_dict()
        raw["events"][0]["dispatch_inputs"] = [
            {
                "name": "dry_run",
                "type": "boolean",
                "required": False,
                "default_present": True,
                "default": "false",
            }
        ]
        with self.assertRaises(ReleaseSpecError):
            NormalizedGitHubWorkflowSourceV1.from_dict(raw)

    def test_closed_nested_roots_are_required(self) -> None:
        broken = valid_spec()
        del broken["required_jobs_v1"]["jobs"]
        with self.assertRaises(ReleaseSpecError):
            ReleaseSpec.from_dict(broken)
        broken = valid_spec()
        del broken["operation_variants"][0]["project_fields"]
        with self.assertRaises(ReleaseSpecError):
            ReleaseSpec.from_dict(broken)

    def test_source_manifest_cross_kind_overlap(self) -> None:
        manifest = source_manifest()
        manifest["additional_paths"] = ["source"]
        manifest["additional_trees"] = ["source/nested"]
        with self.assertRaises(ReleaseSpecError):
            ReleaseSourceManifest.from_dict(manifest).validate()

    def test_source_manifest_external_roots_and_prompt_coverage(self) -> None:
        valid = source_manifest()
        valid["external_roots"] = [
            {"kind": "profile", "key": "a", "runtime_digest_required": True},
            {"kind": "profile", "key": "z", "runtime_digest_required": True},
        ]
        parsed = ReleaseSourceManifest.from_dict(valid)
        self.assertEqual(
            [item.as_dict() for item in parsed.external_roots],
            valid["external_roots"],
        )
        manifest = source_manifest()
        manifest["external_roots"] = [
            {"kind": "profile", "key": "z", "runtime_digest_required": True},
            {"kind": "profile", "key": "a", "runtime_digest_required": True},
        ]
        with self.assertRaisesRegex(ReleaseSpecError, "sorted and unique"):
            ReleaseSourceManifest.from_dict(manifest)
        duplicate = source_manifest()
        duplicate["external_roots"] = [
            {"kind": "profile", "key": "a", "runtime_digest_required": True},
            {"kind": "profile", "key": "a", "runtime_digest_required": True},
        ]
        with self.assertRaisesRegex(ReleaseSpecError, "sorted and unique"):
            ReleaseSourceManifest.from_dict(duplicate)
        missing_digest = source_manifest()
        missing_digest["external_roots"] = [
            {"kind": "profile", "key": "a", "runtime_digest_required": False},
        ]
        with self.assertRaisesRegex(ReleaseSpecError, "runtime_digest_required"):
            ReleaseSourceManifest.from_dict(missing_digest)
        covered = source_manifest()
        covered["additional_paths"] = [".kent/commands/release.md"]
        covered["declared_prompt_references"] = [".kent/commands/release.md"]
        ReleaseSourceManifest.from_dict(covered).validate()
        uncovered = deepcopy(covered)
        uncovered["declared_prompt_references"] = [".kent/commands/missing.md"]
        with self.assertRaises(ReleaseSpecError):
            ReleaseSourceManifest.from_dict(uncovered).validate()

    def test_profile_identity_uses_release_subobject(self) -> None:
        profile = SimpleNamespace(
            schema_version=4,
            project_name="Example",
            release=SimpleNamespace(
                topology_kind="appsome-release-publication",
                adoption_mode="managed-in-place",
            ),
        )
        ReleaseSpec.from_dict(valid_spec(), profile=profile)

    def test_typed_job_validation_and_effect_write_policy(self) -> None:
        spec = ReleaseSpec.from_dict(valid_spec())
        workflow = normalized_workflow(
            jobs=[
                job("required_release", validation_required=True),
                job(
                    "publish_release",
                    condition="github.event_name == 'workflow_dispatch'",
                    permissions={"contents": "write"},
                ),
            ]
        )
        validated = validate_operation_jobs(
            spec.operation_variants[0],
            workflow,
            required=spec.required_jobs_v1,
            qualification=spec.qualification_jobs_v1,
            effect=spec.effect_jobs_v1,
        )
        self.assertEqual(len(validated.bindings), 2)
        with self.assertRaises(ReleaseSpecError):
            replace(validated, variant_key="stale")
        with self.assertRaises(ReleaseSpecError):
            replace(validated, required=validated.required[:-1])
        with self.assertRaises(ReleaseSpecError):
            replace(validated.required[0], _proof=object())
        nested = replace(
            validated.required[0],
            job={**validated.required[0].job, "job_display_name": "stale"},
        )
        with self.assertRaises(ReleaseSpecError):
            replace(validated, required=(nested,))

    def test_effect_jobs_may_bind_job_local_secrets(self) -> None:
        source_job = job(
            "publish_release",
            condition="github.event_name == 'workflow_dispatch'",
            permissions={"contents": "write"},
            run="echo ${{ secrets.TOKEN }}",
            secret_refs=["TOKEN"],
        )
        source = normalized_workflow(jobs=[source_job])
        table = {
            "schema": "effect_jobs_v1",
            "jobs": [contract_row("effect", "effect_with_secret", source_job)],
        }
        self.assertEqual(len(validate_effect_job_sources(source, table)), 1)

    def test_required_policy_negatives(self) -> None:
        spec = ReleaseSpec.from_dict(valid_spec())
        raw = normalized_workflow(jobs=[job("required_release")])
        no_validation = deepcopy(spec.required_jobs_v1.as_dict())
        no_validation["jobs"][0]["steps"][0]["validation_required"] = False
        with self.assertRaises(ReleaseSpecError):
            validate_required_job_sources(raw, no_validation)
        raw = normalized_workflow(
            jobs=[job("required_release", validation_required=True)]
        ).as_dict()
        raw["jobs"][0]["needs"] = ["other"]
        source = NormalizedGitHubWorkflowSourceV1.from_dict(raw)
        with self.assertRaises(ReleaseSpecError):
            validate_required_job_sources(source, spec.required_jobs_v1)
        conditional_step = job("required_release", validation_required=True)
        conditional_step["steps"][0]["condition"] = "github.ref == 'refs/heads/main'"
        conditional_table = {
            "schema": "required_jobs_v1",
            "jobs": [contract_row("required", "conditional-step", conditional_step)],
        }
        with self.assertRaisesRegex(
            ReleaseSpecError,
            "validation steps must be unconditional and non-failing",
        ):
            validate_required_job_sources(
                normalized_workflow(jobs=[conditional_step]),
                conditional_table,
            )

    def test_required_branch_protection_event_and_fixture_policy(self) -> None:
        source_job = job("required_release", validation_required=True)
        for event_name in ("push", "workflow_dispatch", "schedule"):
            raw = normalized_workflow(jobs=[source_job]).as_dict()
            raw["events"][0] = event_record(event_name)
            source = NormalizedGitHubWorkflowSourceV1.from_dict(raw)
            table = {
                "schema": "required_jobs_v1",
                "jobs": [
                    contract_row(
                        "required",
                        f"required-{event_name}",
                        source_job,
                        event_selector=event_record(event_name),
                    )
                ],
            }
            with self.assertRaisesRegex(ReleaseSpecError, "pull_request or merge_group"):
                validate_required_job_sources(source, table)
        fixtures = {
            "container": {
                "image": "ubuntu@sha256:" + "a" * 64,
                "environment": {},
                "ports": [],
                "options": "",
            },
            "services": {
                "database": {
                    "image": "postgres@sha256:" + "b" * 64,
                    "environment": {},
                    "ports": ["5432"],
                    "options": "",
                }
            },
            "github_environment": "release",
        }
        for fixture_name, fixture_value in fixtures.items():
            effect_job = job(
                "publish_release",
                condition="github.event_name == 'workflow_dispatch'",
                permissions={"contents": "write"},
            )
            effect_job[fixture_name] = fixture_value
            effect_table = {
                "schema": "effect_jobs_v1",
                "jobs": [
                    contract_row(
                        "effect",
                        f"effect-{fixture_name}",
                        effect_job,
                    )
                ],
            }
            with self.assertRaisesRegex(
                ReleaseSpecError,
                "forbidden runtime fixtures",
            ):
                validate_effect_job_sources(
                    normalized_workflow(jobs=[effect_job]),
                    effect_table,
                )

    def test_effect_runner_trust_matches_self_hosted_execution(self) -> None:
        source_job = job(
            "publish_release",
            condition="github.event_name == 'workflow_dispatch'",
            permissions={"contents": "write"},
        )
        source_job["runs_on"] = "self-hosted"
        source = normalized_workflow(jobs=[source_job])
        table = {
            "schema": "effect_jobs_v1",
            "jobs": [contract_row("effect", "self_hosted_effect", source_job)],
        }
        with self.assertRaises(ReleaseSpecError):
            validate_effect_job_sources(source, table)
        arc_job = job(
            "arc_effect",
            condition="schedule selected",
            runs_on="self-hosted",
        )
        arc_table = {
            "schema": "effect_jobs_v1",
            "jobs": [contract_row("effect", "arc_effect", arc_job)],
        }
        arc_table["jobs"][0]["runner_trust"] = "organization-arc-ephemeral-effect"
        self.assertEqual(len(validate_effect_job_sources(
            normalized_workflow(jobs=[arc_job]),
            arc_table,
        )), 1)

    def test_required_portfolio_credentials_and_safe_effects(self) -> None:
        spec = ReleaseSpec.from_dict(valid_spec())
        table = deepcopy(spec.required_jobs_v1.as_dict())
        table["jobs"][0]["credential_profile"] = (
            "github-platform-contents-packages-read"
        )
        table["jobs"][0]["allowed_effects"] = [
            "dependency-downloads",
            "github-actions-cache-read-write",
            "github-actions-logs",
            "github-package-read",
        ]
        source = normalized_workflow(
            jobs=[job("required_release", validation_required=True)]
        )
        self.assertEqual(len(validate_required_job_sources(source, table)), 1)

    def test_effective_defaults_and_inheritance_drift_are_bound(self) -> None:
        source_job = job("required_release", validation_required=True)
        source_job["effective_defaults_run"] = {
            "shell": "bash",
            "working_directory": "repo",
        }
        source_job["steps"][0]["effective_shell"] = "bash"
        source_job["steps"][0]["effective_working_directory"] = "repo/tests"
        table = {
            "schema": "required_jobs_v1",
            "jobs": [contract_row("required", "defaults", source_job)],
        }
        source = normalized_workflow(jobs=[source_job])
        self.assertEqual(len(validate_required_job_sources(source, table)), 1)
        drifted = deepcopy(source.as_dict())
        drifted["jobs"][0]["effective_defaults_run"]["working_directory"] = "other"
        with self.assertRaisesRegex(ReleaseSpecError, "normalized source drift"):
            validate_required_job_sources(
                NormalizedGitHubWorkflowSourceV1.from_dict(drifted),
                table,
            )
        drifted = deepcopy(source.as_dict())
        drifted["jobs"][0]["steps"][0]["effective_working_directory"] = "other/tests"
        with self.assertRaisesRegex(ReleaseSpecError, "normalized source drift"):
            validate_required_job_sources(
                NormalizedGitHubWorkflowSourceV1.from_dict(drifted),
                table,
            )

    def test_qualification_policy_can_be_event_gated_without_condition(self) -> None:
        spec_data = valid_spec()
        qualification_job = job("unit-tests")
        spec_data["qualification_jobs_v1"] = {
            "schema": "qualification_jobs_v1",
            "jobs": [contract_row("qualification", "unit-tests", qualification_job)],
        }
        spec_data["operation_variants"][0]["qualification_job_contract_keys"] = [
            "unit-tests"
        ]
        spec = ReleaseSpec.from_dict(spec_data)
        source = normalized_workflow(
            jobs=[
                job("required_release", validation_required=True),
                qualification_job,
                job(
                    "publish_release",
                    condition="github.event_name == 'workflow_dispatch'",
                    permissions={"contents": "write"},
                ),
            ]
        )
        validated = validate_operation_jobs(
            spec.operation_variants[0],
            source,
            required=spec.required_jobs_v1,
            qualification=spec.qualification_jobs_v1,
            effect=spec.effect_jobs_v1,
        )
        self.assertEqual(len(validated.qualification), 1)
        bad = deepcopy(spec.qualification_jobs_v1.as_dict())
        bad["jobs"][0]["skip_policy"] = "condition-gated"
        with self.assertRaises(ReleaseSpecError):
            validate_qualification_job_sources(source, bad)
        bad["jobs"][0]["skip_policy"] = "sometimes"
        with self.assertRaises(ReleaseSpecError):
            validate_qualification_job_sources(source, bad)

    def test_operation_bytes_bind_repository_and_provenance(self) -> None:
        spec = ReleaseSpec.from_dict(valid_spec())
        workflow = normalized_workflow(
            jobs=[
                job("required_release", validation_required=True),
                job(
                    "publish_release",
                    condition="github.event_name == 'workflow_dispatch'",
                    permissions={"contents": "write"},
                ),
            ]
        )
        validated = validate_operation_jobs(
            spec.operation_variants[0],
            workflow,
            required=spec.required_jobs_v1,
            qualification=spec.qualification_jobs_v1,
            effect=spec.effect_jobs_v1,
        )
        operation = {
            "schema_version": 1,
            "variant_key": "publish",
            "operation_kind": "publish",
            "repository": "owner/repository",
            "runtime_source_envelope_digest": "b" * 64,
            "operation_jobs_manifest_digest": validated.operation_jobs_manifest_digest,
            "authority": spec.operation_variants[0].authority_kind.as_dict(),
            "project_fields": {"version": "1.2.3"},
        }
        result = canonicalize_publication_operation(
            operation,
            spec.operation_variants[0],
            validated,
            spec=spec,
        )
        self.assertEqual(result.operation_bytes, canonical_json_bytes(result.operation))
        with self.assertRaises(ReleaseSpecError):
            canonicalize_publication_operation(
                operation,
                spec.operation_variants[0],
                validated,
            )
        operation["repository"] = "other/repository"
        with self.assertRaises(ReleaseSpecError):
            canonicalize_publication_operation(
                operation,
                spec.operation_variants[0],
                validated,
                spec=spec,
            )

    def test_schema2_github_proof_chain_binds_concrete_operation(self) -> None:
        spec_data = valid_spec()
        spec_data["schema_version"] = 2
        spec_data["operation_variants"][0]["authority_kind"] = {
            "kind": "github_run_template",
            "workflow_path": ".github/workflows/release.yml",
            "workflow_name": "Release",
            "event": "workflow_dispatch",
            "ref_policy": {
                "kind": "prefix_project_field",
                "prefix": "refs/tags/v",
                "project_field": "version",
            },
        }
        spec = ReleaseSpec.from_dict(spec_data)
        workflow = normalized_workflow(
            jobs=[
                job("required_release", validation_required=True),
                job(
                    "publish_release",
                    condition="github.event_name == 'workflow_dispatch'",
                    permissions={"contents": "write"},
                ),
            ]
        )
        validated = validate_operation_jobs(
            spec.operation_variants[0],
            workflow,
            required=spec.required_jobs_v1,
            qualification=spec.qualification_jobs_v1,
            effect=spec.effect_jobs_v1,
        )
        inputs = _make_selected_runtime_source_inputs(
            project_name="Example",
            repository="owner/repository",
            topology_kind="appsome-release-publication",
            project_commit="a" * 40,
            source_preview={"selected": True},
            artifact_digests={
                "spec_raw_blob_sha256": "b" * 64,
                "source_manifest_raw_blob_sha256": "c" * 64,
                "snapshot_raw_blob_sha256": "d" * 64,
            },
            external_roots=(),
        )
        current_execution = {
            "kind": "github_run",
            "repository": "owner/repository",
            "workflow_path": ".github/workflows/release.yml",
            "workflow_name": "Release",
            "event": "workflow_dispatch",
            "run_id": 7,
            "attempt": 1,
            "head_sha": "a" * 40,
            "ref": "refs/tags/v1.2.3",
        }
        context = capture_runtime_execution_context(inputs, current_execution)
        binding = capture_runtime_authority_binding(
            inputs,
            [],
            context,
            {
                key: current_execution[key]
                for key in (
                    "kind",
                    "workflow_path",
                    "workflow_name",
                    "event",
                    "run_id",
                    "attempt",
                    "head_sha",
                    "ref",
                )
            },
        )
        operation = {
            "schema_version": 1,
            "variant_key": "publish",
            "operation_kind": "publish",
            "repository": "owner/repository",
            "runtime_source_envelope_digest": binding.runtime_source_envelope_digest,
            "operation_jobs_manifest_digest": validated.operation_jobs_manifest_digest,
            "authority": dict(binding.authority),
            "project_fields": {"version": "1.2.3"},
        }
        result = canonicalize_publication_operation(
            operation,
            spec.operation_variants[0],
            validated,
            spec=spec,
            runtime_execution_context=context,
            runtime_authority_binding=binding,
        )
        self.assertEqual(result.operation["authority"], dict(binding.authority))
        for mutation in (
            lambda value: value["authority"].update({"run_id": 8}),
            lambda value: value.update(
                {"runtime_source_envelope_digest": "c" * 64}
            ),
            lambda value: value["project_fields"].update({"version": "2.0.0"}),
        ):
            broken = deepcopy(operation)
            mutation(broken)
            with self.assertRaises(ReleaseSpecError):
                canonicalize_publication_operation(
                    broken,
                    spec.operation_variants[0],
                    validated,
                    spec=spec,
                    runtime_execution_context=context,
                    runtime_authority_binding=binding,
                )

    def test_schema2_kent_template_accepts_exact_approval_materialization(self) -> None:
        spec_data = valid_spec()
        spec_data["schema_version"] = 2
        spec_data["operation_variants"][0]["authority_kind"] = {
            "kind": "kent_transition_template",
            "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
            "project_id": "project-123e4567-e89b-12d3-a456-426614174000",
            "approval_authority": "release-manager",
        }
        spec_data["operation_variants"][0]["authority_transitions"] = ["approve"]
        spec_data["operation_variants"][0]["approval_required"] = True
        spec_data["approval_materializations"] = [
            {
                "variant_key": "publish",
                "source_path": ".kent/scripts/approve",
                "source_node_key": "approval",
                "source_node_kind": "script",
                "authority_transition_parameter": "authority_transition",
                "summary_language": "ru",
                "summary_sections": ["Нужно от вас", "Почему", "После подтверждения"],
                "materialized_before_pending_approval": True,
                "commentary_equals_summary": True,
                "decision_may_select_approval": False,
                "required_fields": ["version"],
                "templates": {
                    "approve": {
                        "Нужно от вас": "Версия {{version}}",
                        "Почему": "Digest {{operation_digest}}",
                        "После подтверждения": "Продолжить",
                    }
                },
            }
        ]
        spec = ReleaseSpec.from_dict(spec_data)
        operation = {
            "variant_key": "publish",
            "authority_transition": "approve",
            "project_fields": {"version": "1.2.3"},
        }
        summary = render_approval_summary(
            spec.approval_materializations[0],
            operation,
            "b" * 64,
        )
        self.assertEqual(summary, "Версия 1.2.3\nDigest " + "b" * 64 + "\nПродолжить")
        mismatched = deepcopy(spec_data)
        mismatched["workflow_source_intent"]["id"] = (
            "223e4567-e89b-12d3-a456-426614174000"
        )
        with self.assertRaises(ReleaseSpecError):
            ReleaseSpec.from_dict(mismatched)

    def test_nullable_project_fields_and_authority_formats(self) -> None:
        spec_data = valid_spec()
        spec_data["operation_variants"][0]["project_fields"].append(
            {
                "name": "optional_note",
                "type": "string",
                "nullable": True,
                "approval_renderable": False,
            }
        )
        spec_data["operation_variants"][0]["project_fields"].extend(
            [
                {
                    "name": "build_number",
                    "type": "integer",
                    "nullable": False,
                    "approval_renderable": False,
                },
                {
                    "name": "dry_run",
                    "type": "boolean",
                    "nullable": False,
                    "approval_renderable": False,
                },
            ]
        )
        spec = ReleaseSpec.from_dict(spec_data)
        workflow = normalized_workflow(
            jobs=[
                job("required_release", validation_required=True),
                job(
                    "publish_release",
                    condition="github.event_name == 'workflow_dispatch'",
                    permissions={"contents": "write"},
                ),
            ]
        )
        validated = validate_operation_jobs(
            spec.operation_variants[0],
            workflow,
            required=spec.required_jobs_v1,
            qualification=spec.qualification_jobs_v1,
            effect=spec.effect_jobs_v1,
        )
        operation = {
            "schema_version": 1,
            "variant_key": "publish",
            "operation_kind": "publish",
            "repository": "owner/repository",
            "runtime_source_envelope_digest": "b" * 64,
            "operation_jobs_manifest_digest": validated.operation_jobs_manifest_digest,
            "authority": spec.operation_variants[0].authority_kind.as_dict(),
            "project_fields": {
                "version": "1.2.3",
                "optional_note": None,
                "build_number": 7,
                "dry_run": False,
            },
        }
        canonicalize_publication_operation(
            operation,
            spec.operation_variants[0],
            validated,
            spec=spec,
        )
        for field_name, bad_value in (
            ("version", 1),
            ("build_number", "7"),
            ("dry_run", "false"),
            ("optional_note", 1),
        ):
            bad_operation = deepcopy(operation)
            bad_operation["project_fields"][field_name] = bad_value
            with self.assertRaises(ReleaseSpecError):
                canonicalize_publication_operation(
                    bad_operation,
                    spec.operation_variants[0],
                    validated,
                    spec=spec,
                )
        bad_operation = deepcopy(operation)
        bad_operation["project_fields"]["version"] = None
        with self.assertRaises(ReleaseSpecError):
            canonicalize_publication_operation(
                bad_operation,
                spec.operation_variants[0],
                validated,
                spec=spec,
            )
        bad_authority = deepcopy(spec_data)
        bad_authority["operation_variants"][0]["authority_kind"] = {
            **bad_authority["operation_variants"][0]["authority_kind"],
            "kind": "kent_transition",
            "task_short_id": "KIT-42",
            "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
            "workflow_revision": 2,
            "project_id": "project-123e4567-e89b-12d3-a456-426614174000",
            "approval_authority": "release manager",
            "authority_transition": "approve",
        }
        bad_authority["operation_variants"][0]["authority_transitions"] = ["approve"]
        bad_authority["operation_variants"][0]["approval_required"] = True
        with self.assertRaises(ReleaseSpecError):
            ReleaseSpec.from_dict(bad_authority)
        bad_authority["operation_variants"][0]["authority_kind"][
            "approval_authority"
        ] = "release\nmanager"
        with self.assertRaises(ReleaseSpecError):
            ReleaseSpec.from_dict(bad_authority)
        github_authority = spec_data["operation_variants"][0]["authority_kind"]
        for field_name, bad_value in (
            ("run_id", 0),
            ("attempt", 0),
            ("head_sha", "A" * 40),
            ("ref", "main"),
        ):
            broken = deepcopy(github_authority)
            broken[field_name] = bad_value
            with self.assertRaises(ReleaseSpecError):
                AuthoritySpec.from_dict(broken)
        kent_authority = {
            "kind": "kent_transition",
            "task_short_id": "KIT-42",
            "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
            "workflow_revision": 2,
            "project_id": "project-123e4567-e89b-12d3-a456-426614174000",
            "approval_authority": "release-manager",
            "authority_transition": "approve",
        }
        for field_name, bad_value in (
            ("workflow_id", "not-a-uuid"),
            ("task_short_id", "kit-42"),
            ("project_id", "project-123"),
            ("authority_transition", "approve now"),
            ("approval_authority", "release manager"),
        ):
            broken = deepcopy(kent_authority)
            broken[field_name] = bad_value
            with self.assertRaises(ReleaseSpecError):
                AuthoritySpec.from_dict(broken)

    def test_approval_rendering_is_exact_and_safe(self) -> None:
        materialization = ApprovalMaterialization.from_dict(
            {
                "variant_key": "publish",
                "source_path": ".kent/scripts/approve",
                "source_node_key": "approval",
                "source_node_kind": "script",
                "authority_transition_parameter": "authority_transition",
                "summary_language": "ru",
                "summary_sections": ["Нужно от вас", "Почему", "После подтверждения"],
                "materialized_before_pending_approval": True,
                "commentary_equals_summary": True,
                "decision_may_select_approval": False,
                "required_fields": ["version"],
                "templates": {
                    "approve": {
                        "Нужно от вас": "Версия {{version}}",
                        "Почему": "Digest {{operation_digest}}",
                        "После подтверждения": "Продолжить",
                    }
                },
            }
        )
        summary = render_approval_summary(
            materialization,
            {
                "variant_key": "publish",
                "authority_transition": "approve",
                "project_fields": {"version": "1.2.3"},
            },
            "b" * 64,
        )
        self.assertEqual(len(summary.splitlines()), 3)
        bad = deepcopy(materialization.templates)
        bad["approve"]["Нужно от вас"] = "{{version.__class__}}"
        materialization = ApprovalMaterialization(
            **{
                **materialization.__dict__,
                "templates": bad,
            }
        )
        with self.assertRaises(ReleaseSpecError):
            render_approval_summary(
                materialization,
                {
                    "variant_key": "publish",
                    "authority_transition": "approve",
                    "project_fields": {"version": "1.2.3"},
                },
                "b" * 64,
            )

    def test_approval_validation_requires_exact_source_materialization(self) -> None:
        materialization = ApprovalMaterialization.from_dict(
            {
                "variant_key": "publish",
                "source_path": ".kent/scripts/approve",
                "source_node_key": "approval",
                "source_node_kind": "script",
                "authority_transition_parameter": "authority_transition",
                "summary_language": "ru",
                "summary_sections": ["Нужно от вас", "Почему", "После подтверждения"],
                "materialized_before_pending_approval": True,
                "commentary_equals_summary": True,
                "decision_may_select_approval": False,
                "required_fields": ["version"],
                "templates": {
                    "approve": {
                        "Нужно от вас": "Версия {{version}}",
                        "Почему": "Digest {{operation_digest}}",
                        "После подтверждения": "Продолжить",
                    }
                },
            }
        )
        operation = {
            "variant_key": "publish",
            "authority_transition": "approve",
            "project_fields": {"version": "1.2.3"},
        }
        digest = "b" * 64
        summary = render_approval_summary(materialization, operation, digest)
        source = {
            "source_path": ".kent/scripts/approve",
            "source_node_key": "approval",
            "source_node_kind": "script",
            "variant_key": "publish",
            "authority_transition": "approve",
            "operation_digest": digest,
            "summary": summary,
            "commentary": summary,
        }
        self.assertEqual(
            validate_approval_materialization(
                materialization,
                operation,
                digest,
                source_text=source,
                expected_summary=summary,
                expected_commentary=summary,
            ),
            summary,
        )
        with self.assertRaises(ReleaseSpecError):
            validate_approval_materialization(
                materialization,
                operation,
                digest,
                source_text=None,
                expected_summary=summary,
                expected_commentary=summary,
            )
        with self.assertRaises(ReleaseSpecError):
            validate_approval_materialization(
                materialization,
                operation,
                digest,
                source_text={**source, "extra": True},
                expected_summary=summary,
                expected_commentary=summary,
            )
        with self.assertRaises(ReleaseSpecError):
            validate_approval_materialization(
                materialization,
                operation,
                digest,
                source_text=summary,  # type: ignore[arg-type]
                expected_summary=summary,
                expected_commentary=summary,
            )

    def test_typed_preview_requires_complete_bindings_and_digests(self) -> None:
        spec = ReleaseSpec.from_dict(valid_spec())
        workflow = normalized_workflow(
            jobs=[
                job("required_release", validation_required=True),
                job(
                    "publish_release",
                    condition="github.event_name == 'workflow_dispatch'",
                    permissions={"contents": "write"},
                ),
            ]
        )
        validated = validate_operation_jobs(
            spec.operation_variants[0],
            workflow,
            required=spec.required_jobs_v1,
            qualification=spec.qualification_jobs_v1,
            effect=spec.effect_jobs_v1,
        )
        artifacts = SelectedReleaseArtifacts(
            spec_raw_blob_sha256="a" * 64,
            source_manifest_raw_blob_sha256="b" * 64,
            snapshot_raw_blob_sha256="c" * 64,
        )
        preview = render_release_preview(
            spec,
            {validated.variant_key: validated},
            artifacts,
            job_sources_validated=True,
        )
        self.assertTrue(preview["job_sources_validated"])
        changed_artifacts = SelectedReleaseArtifacts(
            spec_raw_blob_sha256="d" * 64,
            source_manifest_raw_blob_sha256="e" * 64,
            snapshot_raw_blob_sha256="f" * 64,
            builder_raw_blob_sha256="1" * 64,
        )
        changed_preview = render_release_preview(
            spec,
            {validated.variant_key: validated},
            changed_artifacts,
            job_sources_validated=True,
        )
        self.assertNotEqual(
            preview["artifact_digests"]["spec_raw_blob_sha256"],
            changed_preview["artifact_digests"]["spec_raw_blob_sha256"],
        )
        self.assertNotEqual(preview, changed_preview)
        with self.assertRaises(ReleaseSpecError):
            render_release_preview(
                spec,
                {validated.variant_key: validated},
                artifacts,
                job_sources_validated="true",
            )
        with self.assertRaises(ReleaseSpecError):
            render_release_preview(spec, {}, artifacts, job_sources_validated=True)

    def test_multi_workflow_selection_and_overlay_digest(self) -> None:
        spec_data = valid_spec()
        required_job = job("required_release", validation_required=True)
        required_job["steps"] = [
            step(run="echo first"),
            step(run="echo second"),
        ]
        spec_data["required_jobs_v1"]["jobs"] = [
            contract_row("required", "required_release_contract", required_job)
        ]
        spec = ReleaseSpec.from_dict(spec_data)
        workflow = normalized_workflow(
            jobs=[
                required_job,
                job(
                    "publish_release",
                    condition="github.event_name == 'workflow_dispatch'",
                    permissions={"contents": "write"},
                ),
            ]
        )
        other = normalized_workflow(path=".github/workflows/other.yml")
        validated = validate_operation_jobs(
            spec.operation_variants[0],
            [workflow, other],
            required=spec.required_jobs_v1,
            qualification=spec.qualification_jobs_v1,
            effect=spec.effect_jobs_v1,
        )
        base = deepcopy(spec.required_jobs_v1.as_dict())
        base["jobs"][0]["steps"][0]["validation_required"] = True
        base["jobs"][0]["steps"][1]["validation_required"] = False
        base_validated = validate_operation_jobs(
            spec.operation_variants[0],
            workflow,
            required=base,
            qualification=spec.qualification_jobs_v1,
            effect=spec.effect_jobs_v1,
        )
        moved = deepcopy(base)
        moved["jobs"][0]["steps"][0]["validation_required"] = False
        moved["jobs"][0]["steps"][1]["validation_required"] = True
        moved_validated = validate_operation_jobs(
            spec.operation_variants[0],
            workflow,
            required=moved,
            qualification=spec.qualification_jobs_v1,
            effect=spec.effect_jobs_v1,
        )
        self.assertNotEqual(
            base_validated.operation_jobs_manifest_digest,
            moved_validated.operation_jobs_manifest_digest,
        )
        both_marked = deepcopy(base)
        both_marked["jobs"][0]["steps"][1]["validation_required"] = True
        both_validated = validate_operation_jobs(
            spec.operation_variants[0],
            workflow,
            required=both_marked,
            qualification=spec.qualification_jobs_v1,
            effect=spec.effect_jobs_v1,
        )
        removed_again = deepcopy(both_marked)
        removed_again["jobs"][0]["steps"][1]["validation_required"] = False
        removed_validated = validate_operation_jobs(
            spec.operation_variants[0],
            workflow,
            required=removed_again,
            qualification=spec.qualification_jobs_v1,
            effect=spec.effect_jobs_v1,
        )
        self.assertNotEqual(
            both_validated.operation_jobs_manifest_digest,
            base_validated.operation_jobs_manifest_digest,
        )
        self.assertEqual(
            removed_validated.operation_jobs_manifest_digest,
            base_validated.operation_jobs_manifest_digest,
        )
        broken = deepcopy(base)
        broken["jobs"][0]["steps"].pop()
        with self.assertRaises(ReleaseSpecError):
            validate_operation_jobs(
                spec.operation_variants[0],
                [workflow, other],
                required=broken,
                qualification=spec.qualification_jobs_v1,
                effect=spec.effect_jobs_v1,
            )
        extra = deepcopy(base)
        extra["jobs"][0]["steps"].append(
            {**step(run="echo extra"), "validation_required": False}
        )
        with self.assertRaises(ReleaseSpecError):
            validate_operation_jobs(
                spec.operation_variants[0],
                [workflow, other],
                required=extra,
                qualification=spec.qualification_jobs_v1,
                effect=spec.effect_jobs_v1,
            )
        with_step_index = deepcopy(base)
        with_step_index["jobs"][0]["steps"][0]["step_index"] = 0
        with self.assertRaisesRegex(ReleaseSpecError, "unknown keys"):
            validate_operation_jobs(
                spec.operation_variants[0],
                [workflow, other],
                required=with_step_index,
                qualification=spec.qualification_jobs_v1,
                effect=spec.effect_jobs_v1,
            )
        reordered_raw = workflow.as_dict()
        required_source = next(
            item for item in reordered_raw["jobs"]
            if item["job_key"] == "required_release"
        )
        required_source["steps"].reverse()
        reordered_source = NormalizedGitHubWorkflowSourceV1.from_dict(reordered_raw)
        with self.assertRaises(ReleaseSpecError):
            validate_operation_jobs(
                spec.operation_variants[0],
                [reordered_source, other],
                required=base,
                qualification=spec.qualification_jobs_v1,
                effect=spec.effect_jobs_v1,
            )
        rebound = deepcopy(base)
        rebound["jobs"][0]["steps"][1]["name"] = "rebound"
        with self.assertRaises(ReleaseSpecError):
            validate_operation_jobs(
                spec.operation_variants[0],
                [workflow, other],
                required=rebound,
                qualification=spec.qualification_jobs_v1,
                effect=spec.effect_jobs_v1,
            )
        reordered = deepcopy(spec.required_jobs_v1.as_dict())
        reordered["jobs"].reverse()
        reordered_validated = validate_operation_jobs(
            spec.operation_variants[0],
            [workflow, other],
            required=reordered,
            qualification=spec.qualification_jobs_v1,
            effect=spec.effect_jobs_v1,
        )
        self.assertEqual(
            validated.operation_jobs_manifest_digest,
            reordered_validated.operation_jobs_manifest_digest,
        )

    def test_kent_authority_and_approval_cardinality(self) -> None:
        spec_data = valid_spec()
        variant = spec_data["operation_variants"][0]
        variant["authority_kind"] = {
            "kind": "kent_transition",
            "task_short_id": "KIT-42",
            "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
            "workflow_revision": 2,
            "project_id": "project-123e4567-e89b-12d3-a456-426614174000",
            "approval_authority": "release-manager",
            "authority_transition": "approve",
        }
        variant["authority_transitions"] = ["approve", "reject"]
        variant["approval_required"] = True
        spec_data["approval_materializations"] = [
            {
                "variant_key": "publish",
                "source_path": ".kent/scripts/approve",
                "source_node_key": "approval",
                "source_node_kind": "script",
                "authority_transition_parameter": "authority_transition",
                "summary_language": "ru",
                "summary_sections": [
                    "Нужно от вас",
                    "Почему",
                    "После подтверждения",
                ],
                "materialized_before_pending_approval": True,
                "commentary_equals_summary": True,
                "decision_may_select_approval": False,
                "required_fields": ["version"],
                "templates": {
                    "approve": {
                        "Нужно от вас": "Version {{version}}",
                        "Почему": "Digest {{operation_digest}}",
                        "После подтверждения": "Continue",
                    },
                    "reject": {
                        "Нужно от вас": "Version {{version}}",
                        "Почему": "Digest {{operation_digest}}",
                        "После подтверждения": "Stop",
                    },
                },
            }
        ]
        self.assertEqual(ReleaseSpec.from_dict(spec_data).schema_version, 1)
        missing_project_placeholder = deepcopy(spec_data)
        missing_project_placeholder["approval_materializations"][0]["templates"][
            "approve"
        ]["Нужно от вас"] = "Версия"
        with self.assertRaises(ReleaseSpecError):
            ReleaseSpec.from_dict(missing_project_placeholder)
        spec_data["approval_materializations"][0]["templates"].pop("reject")
        with self.assertRaises(ReleaseSpecError):
            ReleaseSpec.from_dict(spec_data)


if __name__ == "__main__":
    unittest.main()
