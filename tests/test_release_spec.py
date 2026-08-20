from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest

from workflowkit.release import (
    ApprovalMaterialization,
    AuthoritySpec,
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
        "effective_environment": {},
        "steps": steps or [step(run=default_run, secret_refs=refs)],
    }


def normalized_workflow(
    *,
    path: str = ".github/workflows/release.yml",
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
        "environment": {},
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
