from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from workflowkit.revision import (
    RevisionPreflightError,
    preflight_project_revision,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PROFILE = REPO_ROOT / "contracts" / "project-profile.example.toml"
WORK_KIND_PROCEDURES = (
    ".kent/commands/feature-start.md",
    ".kent/commands/feature-implement.md",
    ".kent/commands/bugfix-start.md",
    ".kent/commands/bugfix-implement.md",
    ".kent/commands/refactor-start.md",
    ".kent/commands/migration-start.md",
    ".kent/commands/dependency-update.md",
    ".kent/commands/test-coverage.md",
)
CONTEXT_MANIFESTS = (
    ".kent/context/plan.md",
    ".kent/context/implement.md",
    ".kent/context/review.md",
    ".kent/context/smoke.md",
    ".kent/context/delivery.md",
)


def schema4_profile_contents(*, metadata_only: bool = False) -> str:
    contents = EXAMPLE_PROFILE.read_text()
    contents = contents.replace(
        "schema_version = 3\n",
        (
            "schema_version = 4\n"
            'kit_managed_commands = ["dispatch"]\n'
        ),
    )
    contents = contents.replace('release_topology = "none"\n', "")
    topology_kind = (
        "sdk-merged-main-publication"
        if metadata_only
        else "appsome-release-publication"
    )
    adoption_mode = "metadata-only" if metadata_only else "managed-in-place"
    builder_path = "" if metadata_only else ".kent/release/build.sh"
    contents += (
        "\n[command_versions]\n"
        'dispatch = "1.2.3"\n'
        "\n[release]\n"
        f'topology_kind = "{topology_kind}"\n'
        f'adoption_mode = "{adoption_mode}"\n'
        'spec_path = ".kent/release/spec.toml"\n'
        f'builder_path = "{builder_path}"\n'
        'snapshot_path = ".kent/release/snapshot.json"\n'
    )
    return contents


def toml_inline(value: object) -> str:
    if value is None:
        return "{ }"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(toml_inline(item) for item in value) + "]"
    if isinstance(value, dict):
        items = [
            f"{json.dumps(str(key))} = {toml_inline(item)}"
            for key, item in value.items()
        ]
        return "{ " + ", ".join(items) + " }"
    raise TypeError(f"unsupported TOML fixture value: {type(value).__name__}")


def release_spec_contents(
    *,
    topology_kind: str = "appsome-release-publication",
    adoption_mode: str = "managed-in-place",
    project_name: str = "example",
    repository: str = "owner/repository",
    workflow_path: str = ".github/workflows/release.yml",
    approval_path: str | None = None,
) -> str:
    event = {
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
    empty_container = {
        "image": "",
        "environment": {},
        "ports": [],
        "options": "",
    }
    source_step = {
        "kind": "run",
        "name": "validate",
        "condition": "",
        "continue_on_error": False,
        "uses": "",
        "with": {},
        "run": "echo ok",
        "effective_shell": "bash",
        "effective_working_directory": "",
        "effective_environment": {},
        "secret_refs": [],
    }

    def job_row(
        job_key: str,
        *,
        effect: bool,
    ) -> dict[str, object]:
        return {
            "contract_key": f"{job_key}_contract",
            "workflow_path": workflow_path,
            "event_selector": event,
            "job_key": job_key,
            "job_display_name": job_key.replace("_", " ").title(),
            "needs": [],
            "matrix": {},
            "condition": (
                "github.event_name == 'workflow_dispatch'" if effect else ""
            ),
            "continue_on_error": False,
            "runs_on": "ubuntu-latest",
            "runner_environment_asserted": True,
            "effective_permissions": {
                "contents": "write" if effect else "read",
            },
            "effective_defaults_run": {
                "shell": "",
                "working_directory": "",
            },
            "github_environment": "",
            "services": {},
            "container": empty_container,
            "checkout_persist_credentials": False,
            "secret_refs": [],
            "effective_environment": {},
            "steps": [
                {
                    **source_step,
                    "validation_required": not effect,
                }
            ],
            "runner_trust": (
                "github-hosted-standard-ephemeral-effect"
                if effect
                else "github-hosted-standard-ephemeral"
            ),
            "credential_profile": "release" if effect else (
                "github-platform-contents-read"
            ),
            "allowed_effects": (
                ["release-publish"]
                if effect
                else ["dependency-downloads", "github-actions-logs"]
            ),
            "skip_policy": "event-gated" if effect else "never",
            "branch_protection_required": not effect,
            "control_plane_fixtures_forbidden": True,
            "credential_scope_is_job_local": effect,
        }

    required = job_row("required_release", effect=False)
    effect = job_row("publish_release", effect=True)
    authority = {
        "kind": "kent_transition",
        "task_short_id": "KIT-42",
        "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
        "workflow_revision": 2,
        "project_id": "project-123e4567-e89b-12d3-a456-426614174000",
        "approval_authority": "release-manager",
        "authority_transition": "approve",
    } if approval_path else {
        "kind": "github_run",
        "workflow_path": workflow_path,
        "workflow_name": "Release",
        "event": "workflow_dispatch",
        "run_id": 7,
        "attempt": 1,
        "head_sha": "a" * 40,
        "ref": "refs/heads/main",
    }
    variant = {
        "key": "publish",
        "operation_kind": "publish",
        "authority_kind": authority,
        "authority_transitions": ["approve"] if approval_path else [],
        "required_job_contract_keys": ["required_release_contract"],
        "qualification_job_contract_keys": [],
        "effect_job_contract_keys": ["publish_release_contract"],
        "approval_required": bool(approval_path),
        "project_fields": [
            {
                "name": "version",
                "type": "string",
                "nullable": False,
                "approval_renderable": True,
            }
        ],
    }
    roots = {
        "schema_version": 1,
        "spec_kind": "release",
        "topology_kind": topology_kind,
        "adoption_mode": adoption_mode,
        "project_name": project_name,
        "repository": repository,
        "runtime_attested": False,
        "workflow_source_intent": {
            "name": "Release",
            "id": "123e4567-e89b-12d3-a456-426614174000",
            "update_kind": (
                "metadata-only"
                if adoption_mode == "metadata-only"
                else "graph-and-metadata"
            ),
            "expected_project_link": "non-default",
            "expected_project_default": False,
            "allow_create": False,
            "allow_default_change": False,
            "allow_uuid_change": False,
        },
        "source_manifest": {
            "schema": "release_source_manifest_v1",
            "path": ".kent/release/source-manifest.json",
            "revision_binding": "runtime-source-envelope",
            "runtime_attested": False,
        },
        "required_jobs_v1": {
            "schema": "required_jobs_v1",
            "jobs": [required],
        },
        "qualification_jobs_v1": {
            "schema": "qualification_jobs_v1",
            "jobs": [],
        },
        "effect_jobs_v1": {
            "schema": "effect_jobs_v1",
            "jobs": [effect],
        },
        "operation_variants": [variant],
    }
    if approval_path:
        roots["approval_materializations"] = [
            {
                "variant_key": "publish",
                "source_path": approval_path,
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
                        "Нужно от вас": "Версия {{version}}",
                        "Почему": "Digest {{operation_digest}}",
                        "После подтверждения": "Продолжить",
                    }
                },
            }
        ]
    lines = []
    scalar_roots = {
        key: value
        for key, value in roots.items()
        if not isinstance(value, dict)
    }
    for key, value in scalar_roots.items():
        lines.append(f"{key} = {toml_inline(value)}")
    for key, value in roots.items():
        if not isinstance(value, dict):
            continue
        lines.append("")
        lines.append(f"[{key}]")
        for child_key, child_value in value.items():
            lines.append(f"{child_key} = {toml_inline(child_value)}")
    return "\n".join(lines) + "\n"


def source_manifest_contents(
    *,
    project_name: str = "example",
    repository: str = "owner/repository",
    topology_kind: str = "appsome-release-publication",
    additional_paths: list[str] | None = None,
    additional_trees: list[str] | None = None,
    declared_prompt_references: list[str] | None = None,
    external_roots: list[dict[str, object]] | None = None,
) -> str:
    return json.dumps(
        {
            "schema": "release_source_manifest_v1",
            "closure_algorithm": "project-instruction-closure-v1",
            "project_name": project_name,
            "repository": repository,
            "topology_kind": topology_kind,
            "additional_paths": additional_paths or [],
            "additional_trees": additional_trees or [],
            "declared_prompt_references": declared_prompt_references or [],
            "external_roots": external_roots or [],
            "runtime_attested": False,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ) + "\n"


class RevisionPreflightTest(unittest.TestCase):
    def create_project(
        self,
        *,
        schema4: bool = False,
        metadata_only: bool = False,
        approval: bool = False,
    ) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.run_git(root, "init", "-q")
        self.run_git(root, "config", "user.name", "Kent Test")
        self.run_git(root, "config", "user.email", "kent@example.invalid")
        self.run_git(root, "config", "core.fileMode", "true")

        kent = root / ".kent"
        scripts = kent / "scripts"
        scripts.mkdir(parents=True)
        (kent / "workflow-profile.toml").write_text(
            schema4_profile_contents(metadata_only=metadata_only)
            if schema4
            else EXAMPLE_PROFILE.read_text()
        )
        (kent / "project-contract.md").write_text("# Project contract\n")
        for configured_path in WORK_KIND_PROCEDURES + CONTEXT_MANIFESTS:
            path = root / configured_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Test procedure\n")
        for name in (
            "workflow-checkpoint",
            "workflow-evidence-ledger",
            "workflow-plan-contract",
            "workflow-plan-contract-accept",
            "workflow-plan-contract-continue",
            "workflow-plan-contract-fix-continue",
            "workflow-plan-contract-verify",
            "workflow-task-janitor",
            "workflow-verification-dispatch",
            "workflow-verify",
            "workflow-wait-github-ci",
            "workflow-wait-github-pr",
        ):
            path = scripts / name
            path.write_text("#!/usr/bin/env bash\nexit 0\n")
            path.chmod(0o755)
        if schema4:
            for configured_path, contents in (
                (
                    ".kent/release/spec.toml",
                    release_spec_contents(
                        topology_kind=(
                            "sdk-merged-main-publication"
                            if metadata_only
                            else "appsome-release-publication"
                        ),
                        adoption_mode=(
                            "metadata-only"
                            if metadata_only
                            else "managed-in-place"
                        ),
                        approval_path=(
                            ".kent/scripts/approve-release"
                            if approval
                            else None
                        ),
                    ),
                ),
                (
                    ".kent/release/source-manifest.json",
                    source_manifest_contents(
                        topology_kind=(
                            "sdk-merged-main-publication"
                            if metadata_only
                            else "appsome-release-publication"
                        ),
                    ),
                ),
                (".kent/release/snapshot.json", "{}\n"),
                (".github/workflows/release.yml", "name: Release\n"),
            ):
                path = root / configured_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents)
            if not metadata_only:
                builder = root / ".kent/release/build.sh"
                builder.write_text("#!/usr/bin/env bash\nexit 0\n")
                builder.chmod(0o755)
            if approval:
                approval_script = root / ".kent/scripts/approve-release"
                approval_script.write_text("#!/usr/bin/env bash\nexit 0\n")
                approval_script.chmod(0o755)
        self.commit_all(root, "Add project workflow adapter")
        return root

    def test_preflight_accepts_ready_revision(self) -> None:
        root = self.create_project()
        result = preflight_project_revision(root, "HEAD")

        self.assertEqual(result.project_name, "example")
        self.assertEqual(result.workflow_prefix, "Example")
        self.assertEqual(
            {path.path for path in result.checked_paths},
            {
                ".kent/project-contract.md",
                ".kent/scripts/workflow-checkpoint",
                ".kent/scripts/workflow-evidence-ledger",
                ".kent/scripts/workflow-plan-contract",
                ".kent/scripts/workflow-plan-contract-accept",
                ".kent/scripts/workflow-plan-contract-continue",
                ".kent/scripts/workflow-plan-contract-fix-continue",
                ".kent/scripts/workflow-plan-contract-verify",
                ".kent/scripts/workflow-task-janitor",
                ".kent/scripts/workflow-verification-dispatch",
                ".kent/scripts/workflow-verify",
                ".kent/scripts/workflow-wait-github-ci",
                ".kent/scripts/workflow-wait-github-pr",
                ".kent/workflow-profile.toml",
                *WORK_KIND_PROCEDURES,
                *CONTEXT_MANIFESTS,
            },
        )
        self.assertEqual(
            set(result.as_json()),
            {
                "project",
                "requested_ref",
                "commit_oid",
                "project_name",
                "workflow_prefix",
                "checked_paths",
                "ready",
            },
        )

    def test_preflight_rejects_revision_without_profile(self) -> None:
        root = self.create_project()
        self.run_git(root, "rm", "-q", ".kent/workflow-profile.toml")
        self.commit_all(root, "Remove profile")

        with self.assertRaisesRegex(
            RevisionPreflightError,
            "project profile not found",
        ):
            preflight_project_revision(root, "HEAD")

    def test_preflight_rejects_missing_command(self) -> None:
        root = self.create_project()
        self.run_git(root, "rm", "-q", ".kent/scripts/workflow-verify")
        self.commit_all(root, "Remove verifier")

        with self.assertRaisesRegex(
            RevisionPreflightError,
            "required path not found.*workflow-verify",
        ):
            preflight_project_revision(root, "HEAD")

    def test_preflight_rejects_non_executable_command(self) -> None:
        root = self.create_project()
        verifier = root / ".kent" / "scripts" / "workflow-verify"
        verifier.chmod(0o644)
        self.commit_all(root, "Drop executable mode")

        with self.assertRaisesRegex(
            RevisionPreflightError,
            "not executable.*100644",
        ):
            preflight_project_revision(root, "HEAD")

    def test_preflight_uses_selected_profile_adapter_requirements(self) -> None:
        root = self.create_project()
        profile = root / ".kent" / "workflow-profile.toml"
        profile.write_text(
            profile.read_text().replace(
                "required_adapters = []",
                'required_adapters = ["mobile_resource_lock"]',
            )
            + (
                "\n[adapters]\n"
                "mobile_resource_lock = "
                '".kent/adapters/mobile/emulator-resource-lock.sh"\n'
            )
        )
        self.commit_all(root, "Require mobile resource lock")

        with self.assertRaisesRegex(
            RevisionPreflightError,
            "required path not found.*emulator-resource-lock",
        ):
            preflight_project_revision(root, "HEAD")

    def test_preflight_includes_schema_four_release_closure(self) -> None:
        root = self.create_project(schema4=True)

        result = preflight_project_revision(root, "HEAD")

        checked = {path.path for path in result.checked_paths}
        self.assertIn(".kent/release/spec.toml", checked)
        self.assertIn(".kent/release/build.sh", checked)
        self.assertIn(".kent/release/snapshot.json", checked)

    def test_preflight_omits_optional_metadata_only_builder(self) -> None:
        root = self.create_project(schema4=True, metadata_only=True)

        checked = {
            path.path
            for path in preflight_project_revision(root, "HEAD").checked_paths
        }

        self.assertIn(".kent/release/spec.toml", checked)
        self.assertNotIn(".kent/release/build.sh", checked)
        self.assertIn(".kent/release/snapshot.json", checked)

    def test_preflight_requires_schema_four_release_files(self) -> None:
        root = self.create_project(schema4=True)
        self.run_git(root, "rm", "-q", ".kent/release/snapshot.json")
        self.commit_all(root, "Remove release snapshot")

        with self.assertRaisesRegex(
            RevisionPreflightError,
            "required path not found.*release/snapshot.json",
        ):
            preflight_project_revision(root, "HEAD")

    def test_schema4_preview_closure_digests_and_flags(self) -> None:
        root = self.create_project(schema4=True)
        result = preflight_project_revision(root, "HEAD")

        self.assertIsNotNone(result.release_preview)
        preview = result.release_preview
        self.assertEqual(
            {
                key: preview[key]
                for key in (
                    "source_contract_valid",
                    "runtime_attested",
                    "job_sources_validated",
                    "activation_authorized",
                    "snapshot_json_valid",
                )
            },
            {
                "source_contract_valid": True,
                "runtime_attested": False,
                "job_sources_validated": False,
                "activation_authorized": False,
                "snapshot_json_valid": True,
            },
        )
        digests = preview["artifact_digests"]
        for key, path in (
            ("spec_raw_blob_sha256", ".kent/release/spec.toml"),
            (
                "source_manifest_raw_blob_sha256",
                ".kent/release/source-manifest.json",
            ),
            ("snapshot_raw_blob_sha256", ".kent/release/snapshot.json"),
            ("builder_raw_blob_sha256", ".kent/release/build.sh"),
        ):
            self.assertEqual(
                digests[key],
                hashlib.sha256((root / path).read_bytes()).hexdigest(),
            )
        checked = {item.path for item in result.checked_paths}
        self.assertTrue(
            {
                ".kent/workflow-profile.toml",
                ".kent/project-contract.md",
                ".kent/release/spec.toml",
                ".kent/release/source-manifest.json",
                ".kent/release/snapshot.json",
                ".github/workflows/release.yml",
                ".kent/release/build.sh",
            } <= checked
        )
        self.assertNotIn("nodes", preview)
        self.assertNotIn("edges", preview)

    def test_schema4_derives_workflow_and_approval_script_paths(self) -> None:
        root = self.create_project(schema4=True, approval=True)
        checked = {
            item.path for item in preflight_project_revision(root, "HEAD").checked_paths
        }
        self.assertIn(".kent/scripts/approve-release", checked)
        approval = root / ".kent/scripts/approve-release"
        approval.chmod(0o644)
        self.commit_all(root, "Drop approval script executable mode")
        with self.assertRaisesRegex(
            RevisionPreflightError,
            "approve-release.*not executable.*100644",
        ):
            preflight_project_revision(root, "HEAD")
        root = self.create_project(schema4=True)
        spec = root / ".kent/release/spec.toml"
        spec.write_text(
            spec.read_text().replace(
                ".github/workflows/release.yml",
                ".github/workflows/missing.yml",
            )
        )
        self.commit_all(root, "Remove declared workflow source")
        with self.assertRaisesRegex(
            RevisionPreflightError,
            "required path not found.*missing.yml",
        ):
            preflight_project_revision(root, "HEAD")

    def test_schema4_manifest_additions_and_recursive_tree_closure(self) -> None:
        root = self.create_project(schema4=True)
        (root / "extra.md").write_text("# Extra\n")
        (root / "docs" / "nested").mkdir(parents=True)
        (root / "docs" / "nested" / "guide.md").write_text("# Guide\n")
        manifest_path = root / ".kent/release/source-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["additional_paths"] = ["extra.md"]
        manifest["additional_trees"] = ["docs"]
        manifest["declared_prompt_references"] = [
            "docs/nested/guide.md",
            "extra.md",
        ]
        manifest_path.write_text(json.dumps(manifest, separators=(",", ":")) + "\n")
        self.commit_all(root, "Add release source additions")

        result = preflight_project_revision(root, "HEAD")
        checked = {item.path for item in result.checked_paths}
        self.assertIn("extra.md", checked)
        self.assertIn("docs/nested/guide.md", checked)
        self.assertEqual(
            result.release_preview["source_manifest"]["additional_source_count"],
            2,
        )
        root = self.create_project(schema4=True)
        (root / "docs").mkdir()
        (root / "docs" / "guide.md").write_text("# Guide\n")
        manifest_path = root / ".kent/release/source-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["additional_trees"] = ["docs"]
        manifest["declared_prompt_references"] = ["docs/missing.md"]
        manifest_path.write_text(json.dumps(manifest, separators=(",", ":")) + "\n")
        self.commit_all(root, "Add uncovered tree prompt reference")
        with self.assertRaisesRegex(RevisionPreflightError, "covered"):
            preflight_project_revision(root, "HEAD")

    def test_schema4_manifest_external_root_order_and_runtime_requirement(self) -> None:
        root = self.create_project(schema4=True)
        manifest_path = root / ".kent/release/source-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["external_roots"] = [
            {"kind": "profile", "key": "a", "runtime_digest_required": True},
            {"kind": "profile", "key": "z", "runtime_digest_required": True},
        ]
        manifest_path.write_text(json.dumps(manifest, separators=(",", ":")) + "\n")
        self.commit_all(root, "Add ordered external roots")
        self.assertTrue(
            preflight_project_revision(root, "HEAD").release_preview[
                "source_contract_valid"
            ]
        )
        cases = (
            (
                [
                    {"kind": "profile", "key": "z", "runtime_digest_required": True},
                    {"kind": "profile", "key": "a", "runtime_digest_required": True},
                ],
                "sorted and unique",
            ),
            (
                [
                    {"kind": "profile", "key": "a", "runtime_digest_required": True},
                    {"kind": "profile", "key": "a", "runtime_digest_required": True},
                ],
                "sorted and unique",
            ),
            (
                [{"kind": "profile", "key": "a", "runtime_digest_required": False}],
                "runtime_digest_required",
            ),
        )
        for external_roots, pattern in cases:
            with self.subTest(pattern=pattern):
                root = self.create_project(schema4=True)
                manifest_path = root / ".kent/release/source-manifest.json"
                manifest = json.loads(manifest_path.read_text())
                manifest["external_roots"] = external_roots
                manifest_path.write_text(
                    json.dumps(manifest, separators=(",", ":")) + "\n"
                )
                self.commit_all(root, "Change external roots")
                with self.assertRaisesRegex(RevisionPreflightError, pattern):
                    preflight_project_revision(root, "HEAD")

    def test_schema4_profile_spec_manifest_identity_mismatch(self) -> None:
        cases = (
            (
                ".kent/release/source-manifest.json",
                lambda data: data.update(project_name="other"),
                "project_name",
            ),
            (
                ".kent/release/source-manifest.json",
                lambda data: data.update(repository="other/repository"),
                "repository",
            ),
            (
                ".kent/release/source-manifest.json",
                lambda data: data.update(topology_kind="puber-release"),
                "topology_kind",
            ),
        )
        for path, mutate, pattern in cases:
            with self.subTest(path=path, pattern=pattern):
                root = self.create_project(schema4=True)
                target = root / path
                data = json.loads(target.read_text())
                mutate(data)
                target.write_text(json.dumps(data, separators=(",", ":")) + "\n")
                self.commit_all(root, "Change release identity")
                with self.assertRaisesRegex(RevisionPreflightError, pattern):
                    preflight_project_revision(root, "HEAD")
        root = self.create_project(schema4=True)
        spec = root / ".kent/release/spec.toml"
        spec.write_text(spec.read_text().replace(
            'project_name = "example"',
            'project_name = "other"',
        ))
        self.commit_all(root, "Change release spec identity")
        with self.assertRaisesRegex(RevisionPreflightError, "project_name"):
            preflight_project_revision(root, "HEAD")

    def test_schema4_strict_text_and_snapshot_json_validation(self) -> None:
        cases = (
            (".kent/release/spec.toml", b"\xff", "UTF-8"),
            (".kent/release/source-manifest.json", b"{", "source manifest"),
            (".kent/release/snapshot.json", b"[]\n", "JSON object"),
            (".kent/release/snapshot.json", b"{", "release snapshot"),
        )
        for path, contents, pattern in cases:
            with self.subTest(path=path, pattern=pattern):
                root = self.create_project(schema4=True)
                (root / path).write_bytes(contents)
                self.commit_all(root, "Break release artifact")
                with self.assertRaisesRegex(RevisionPreflightError, pattern):
                    preflight_project_revision(root, "HEAD")

    def test_schema4_addition_rejections_cover_modes_and_root_overlap(self) -> None:
        cases = (
            (
                {"additional_paths": [".kent/workflow-profile.toml"]},
                "repeat a derived path",
            ),
            (
                {"additional_trees": ["docs", "docs/nested"]},
                "additional_trees may not overlap",
            ),
            (
                {
                    "additional_paths": ["docs/file.md"],
                    "additional_trees": ["docs"],
                },
                "beneath an additional tree",
            ),
            ({"additional_trees": [".kent"]}, "manifest"),
            (
                {"additional_trees": [".github"]},
                "beneath an additional tree",
            ),
            (
                {"additional_trees": [".github/workflows/release.yml"]},
                "beneath an additional tree",
            ),
            (
                {"additional_trees": [".github/workflows/release.yml/nested"]},
                "beneath an additional tree",
            ),
        )
        for changes, pattern in cases:
            with self.subTest(pattern=pattern):
                root = self.create_project(schema4=True)
                manifest_path = root / ".kent/release/source-manifest.json"
                manifest = json.loads(manifest_path.read_text())
                manifest.update(changes)
                manifest_path.write_text(
                    json.dumps(manifest, separators=(",", ":")) + "\n"
                )
                self.commit_all(root, "Add invalid source roots")
                with self.assertRaisesRegex(RevisionPreflightError, pattern):
                    preflight_project_revision(root, "HEAD")
        root = self.create_project(schema4=True)
        (root / "extra.md").write_text("# Extra\n")
        manifest_path = root / ".kent/release/source-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.update(
            additional_paths=["extra.md"],
            additional_trees=["extra.md"],
        )
        manifest_path.write_text(json.dumps(manifest, separators=(",", ":")) + "\n")
        self.commit_all(root, "Overlap additional file and tree")
        with self.assertRaisesRegex(
            RevisionPreflightError,
            "beneath an additional tree",
        ):
            preflight_project_revision(root, "HEAD")
        root = self.create_project(schema4=True)
        (root / "docs").write_text("# Docs\n")
        manifest_path = root / ".kent/release/source-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.update(
            additional_paths=["docs"],
            additional_trees=["docs/nested"],
        )
        manifest_path.write_text(json.dumps(manifest, separators=(",", ":")) + "\n")
        self.commit_all(root, "Nest additional tree beneath file")
        with self.assertRaisesRegex(
            RevisionPreflightError,
            "beneath an additional tree",
        ):
            preflight_project_revision(root, "HEAD")
        root = self.create_project(schema4=True)
        (root / "docs").mkdir()
        (root / "docs" / "file.md").write_text("# Docs\n")
        manifest_path = root / ".kent/release/source-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["additional_paths"] = ["docs"]
        manifest_path.write_text(json.dumps(manifest, separators=(",", ":")) + "\n")
        self.commit_all(root, "Use tree as additional path")
        with self.assertRaisesRegex(RevisionPreflightError, "regular file"):
            preflight_project_revision(root, "HEAD")

    def test_schema4_symlink_addition_and_uncovered_prompt_reject(self) -> None:
        root = self.create_project(schema4=True)
        (root / "target.md").write_text("# Target\n")
        (root / "link.md").symlink_to("target.md")
        manifest_path = root / ".kent/release/source-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["additional_paths"] = ["link.md"]
        manifest_path.write_text(json.dumps(manifest, separators=(",", ":")) + "\n")
        self.commit_all(root, "Add invalid source link")
        with self.assertRaisesRegex(RevisionPreflightError, "regular file"):
            preflight_project_revision(root, "HEAD")
        root = self.create_project(schema4=True)
        (root / "target.md").write_text("# Target\n")
        (root / "docs").mkdir()
        (root / "docs" / "link.md").symlink_to("../target.md")
        manifest_path = root / ".kent/release/source-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["additional_trees"] = ["docs"]
        manifest_path.write_text(json.dumps(manifest, separators=(",", ":")) + "\n")
        self.commit_all(root, "Add symlink inside source tree")
        with self.assertRaisesRegex(RevisionPreflightError, "unsupported Git entry"):
            preflight_project_revision(root, "HEAD")
        root = self.create_project(schema4=True)
        manifest_path = root / ".kent/release/source-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["declared_prompt_references"] = ["missing.md"]
        manifest_path.write_text(json.dumps(manifest, separators=(",", ":")) + "\n")
        self.commit_all(root, "Add uncovered prompt reference")
        with self.assertRaisesRegex(RevisionPreflightError, "covered"):
            preflight_project_revision(root, "HEAD")

    def test_schema4_tree_rejects_empty_and_non_blob_leaves(self) -> None:
        for tree_name, mode, object_kind, pattern in (
            ("empty-tree", "040000", "tree", "empty"),
        ):
            with self.subTest(tree_name=tree_name):
                root = self.create_project(schema4=True)
                manifest_path = root / ".kent/release/source-manifest.json"
                manifest = json.loads(manifest_path.read_text())
                manifest["additional_trees"] = [tree_name]
                manifest_path.write_text(
                    json.dumps(manifest, separators=(",", ":")) + "\n"
                )
                self.commit_all(root, "Declare special tree")
                object_id = (
                    "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
                    if object_kind == "tree"
                    else self.run_git(root, "rev-parse", "HEAD").stdout.strip()
                )
                self.commit_root_tree_entry(
                    root,
                    tree_name,
                    mode,
                    object_kind,
                    object_id,
                )
                with self.assertRaisesRegex(RevisionPreflightError, pattern):
                    preflight_project_revision(root, "HEAD")
        root = self.create_project(schema4=True)
        manifest_path = root / ".kent/release/source-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["additional_trees"] = ["gitlink-tree"]
        manifest_path.write_text(
            json.dumps(manifest, separators=(",", ":")) + "\n"
        )
        self.commit_all(root, "Declare gitlink tree")
        gitlink_tree = self.run_git_input(
            root,
            ["mktree"],
            f"160000 commit {self.run_git(root, 'rev-parse', 'HEAD').stdout.strip()}"
            "\tchild\n",
        ).stdout.strip()
        self.commit_root_tree_entry(
            root,
            "gitlink-tree",
            "040000",
            "tree",
            gitlink_tree,
        )
        with self.assertRaisesRegex(RevisionPreflightError, "unsupported Git entry"):
            preflight_project_revision(root, "HEAD")

    def test_schema4_non_executable_builder_and_metadata_only_omission(self) -> None:
        root = self.create_project(schema4=True)
        builder = root / ".kent/release/build.sh"
        builder.chmod(0o644)
        self.commit_all(root, "Drop builder executable mode")
        with self.assertRaisesRegex(RevisionPreflightError, "build.sh.*not executable"):
            preflight_project_revision(root, "HEAD")
        metadata = self.create_project(schema4=True, metadata_only=True)
        result = preflight_project_revision(metadata, "HEAD")
        checked = {item.path for item in result.checked_paths}
        self.assertNotIn(".kent/release/build.sh", checked)
        artifact_digests = result.release_preview["artifact_digests"]
        self.assertIsNone(artifact_digests.get("builder_raw_blob_sha256"))
        self.assertNotIn("builder_raw_blob_sha256", artifact_digests)

    def test_schema4_raw_digest_changes_preserve_semantic_preview(self) -> None:
        for path, rewrite, digest_key in (
            (
                ".kent/release/spec.toml",
                lambda value: value.replace(
                    "schema_version = 1\n",
                    "schema_version = 1\n\n",
                ),
                "spec_raw_blob_sha256",
            ),
            (
                ".kent/release/source-manifest.json",
                lambda value: json.dumps(
                    dict(reversed(list(json.loads(value).items()))),
                    separators=(",", ":"),
                )
                + "\n",
                "source_manifest_raw_blob_sha256",
            ),
            (
                ".kent/release/snapshot.json",
                lambda value: '{ "stable": true }\n',
                "snapshot_raw_blob_sha256",
            ),
        ):
            with self.subTest(path=path):
                root = self.create_project(schema4=True)
                base = preflight_project_revision(root, "HEAD").release_preview
                target = root / path
                target.write_text(rewrite(target.read_text()))
                self.commit_all(root, "Reorder selected release bytes")
                changed = preflight_project_revision(root, "HEAD").release_preview
                self.assertNotEqual(
                    base["artifact_digests"][digest_key],
                    changed["artifact_digests"][digest_key],
                )
                self.assertEqual(
                    {
                        key: value
                        for key, value in base.items()
                        if key != "artifact_digests"
                    },
                    {
                        key: value
                        for key, value in changed.items()
                        if key != "artifact_digests"
                    },
                )

    def test_schema4_uses_selected_commit_not_working_tree_release_files(self) -> None:
        root = self.create_project(schema4=True)
        (root / ".kent/release/spec.toml").write_bytes(b"\xff")
        result = preflight_project_revision(root, "HEAD")
        self.assertTrue(result.release_preview["source_contract_valid"])

    def test_schema4_cli_preview_is_deterministic(self) -> None:
        root = self.create_project(schema4=True)
        before = sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if not path.is_symlink()
        )
        outputs = []
        for _ in range(2):
            result = subprocess.run(
                [
                    str(REPO_ROOT / "scripts" / "preflight-revision"),
                    "--project",
                    str(root),
                    "--ref",
                    "HEAD",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    **os.environ,
                    "KENT_ENGINEERING_KIT_PYTHON": sys.executable,
                },
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(result.stdout)
        self.assertEqual(outputs[0], outputs[1])
        payload = json.loads(outputs[0])
        self.assertTrue(payload["release_preview"]["source_contract_valid"])
        after = sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if not path.is_symlink()
        )
        self.assertEqual(before, after)

    def test_cli_reports_ready_revision_as_json(self) -> None:
        root = self.create_project()
        script = REPO_ROOT / "scripts" / "preflight-revision"
        launcher = root / "kent-preflight-revision"
        launcher.symlink_to(script)
        result = subprocess.run(
            [
                str(launcher),
                "--project",
                str(root),
                "--ref",
                "HEAD",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_ENGINEERING_KIT_PYTHON": sys.executable,
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["requested_ref"], "HEAD")

    def test_cli_rejects_invalid_explicit_python_override(self) -> None:
        root = self.create_project()
        fake_python = root / "python3-old"
        fake_python.write_text("#!/bin/sh\nexit 1\n")
        fake_python.chmod(0o755)
        result = subprocess.run(
            [
                str(REPO_ROOT / "scripts" / "preflight-revision"),
                "--project",
                str(root),
                "--ref",
                "HEAD",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_ENGINEERING_KIT_PYTHON": str(fake_python),
            },
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("must be Python 3.11 or newer", result.stderr)

    def test_cli_does_not_import_project_local_workflowkit(self) -> None:
        root = self.create_project()
        marker = root / "shadow-imported"
        shadow = root / "workflowkit"
        shadow.mkdir()
        (shadow / "__init__.py").write_text("")
        (shadow / "revision_cli.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('unsafe')\n"
        )
        hostile_path = root / "hostile-python-path"
        hostile_path.mkdir()
        (hostile_path / "sitecustomize.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('unsafe')\n"
        )
        result = subprocess.run(
            [
                str(REPO_ROOT / "scripts" / "preflight-revision"),
                "--project",
                str(root),
                "--ref",
                "HEAD",
            ],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_ENGINEERING_KIT_PYTHON": sys.executable,
                "PYTHONPATH": str(hostile_path),
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertTrue(json.loads(result.stdout)["ready"])

    def test_cli_help_uses_stable_program_name(self) -> None:
        result = subprocess.run(
            [str(REPO_ROOT / "scripts" / "preflight-revision"), "--help"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_ENGINEERING_KIT_PYTHON": sys.executable,
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            result.stdout.startswith("usage: kent-preflight-revision"),
            result.stdout,
        )
        self.assertNotIn("--baseline-ref", result.stdout)

    def commit_all(self, root: Path, message: str) -> None:
        self.run_git(root, "add", "-A")
        self.run_git(root, "commit", "-q", "-m", message)

    def run_git(
        self,
        root: Path,
        *args: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def commit_root_tree_entry(
        self,
        root: Path,
        name: str,
        mode: str,
        object_kind: str,
        object_id: str,
    ) -> None:
        base_tree = self.run_git(root, "rev-parse", "HEAD^{tree}").stdout.strip()
        entries = self.run_git(root, "ls-tree", base_tree).stdout
        tree_contents = entries + f"{mode} {object_kind} {object_id}\t{name}\n"
        tree = self.run_git_input(root, ["mktree"], tree_contents).stdout.strip()
        commit = self.run_git_input(
            root,
            ["commit-tree", tree, "-p", "HEAD"],
            "special tree\n",
        ).stdout.strip()
        head_ref = self.run_git(root, "symbolic-ref", "-q", "HEAD").stdout.strip()
        self.run_git(root, "update-ref", head_ref, commit)

    def run_git_input(
        self,
        root: Path,
        args: list[str],
        input_text: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
