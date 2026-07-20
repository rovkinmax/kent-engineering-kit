from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from workflowkit.delivery import (
    build_canary_workflow,
    build_delivery_workflow,
    build_smoke_lab_workflow,
)
from workflowkit.kent import (
    KentClient,
    context_source_string,
    edge_index,
    execution_target_from_policy,
)
from workflowkit.model import (
    EdgeSpec,
    NodeSpec,
    SpecError,
    WorkflowSpec,
    validate_execution_target,
)
from workflowkit.naming import snapshot_filename
from workflowkit.profile import ProjectProfile


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PROFILE = REPO_ROOT / "contracts" / "project-profile.example.toml"
VERIFY_REPORT = REPO_ROOT / "templates" / "project" / "workflow-verify-report"


class WorkflowKitTest(unittest.TestCase):
    def load_profile(self, transform=lambda value: value) -> ProjectProfile:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        profile_directory = root / ".kent"
        profile_directory.mkdir()
        contents = transform(EXAMPLE_PROFILE.read_text())
        (profile_directory / "workflow-profile.toml").write_text(contents)
        return ProjectProfile.load(root)

    def test_team_delivery_has_direct_fanout_join(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        spec.validate()

        fanout = [
            edge
            for edge in spec.edges
            if edge.source == "verification_dispatch"
            and edge.transition == "fanout_verify"
        ]
        self.assertEqual(
            {edge.target for edge in fanout},
            {"deterministic_verify", "standards_review", "spec_review"},
        )
        for branch in fanout:
            outgoing = [edge for edge in spec.edges if edge.source == branch.target]
            self.assertEqual(len(outgoing), 1)
            self.assertEqual(outgoing[0].target, "verification_join")
        standards_report = next(
            edge for edge in spec.edges if edge.key == "standards_report_join"
        )
        self.assertEqual(
            tuple(parameter.key for parameter in standards_report.parameters),
            ("standards_status", "standards_report"),
        )
        standards_dispatch = next(
            edge for edge in spec.edges if edge.key == "dispatch_standards_review"
        )
        self.assertIn(
            "{{.Params.workspace_path}}",
            standards_dispatch.prompt,
        )

    def test_delivery_is_versioned_and_asks_for_execution_target(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 3)
        self.assertEqual(spec.name, "Example Engineering Delivery v3")
        self.assertEqual(spec.execution_target, "ask-on-first-execution")
        roles = {
            node.key: node.agent
            for node in spec.nodes
            if node.kind == "agent"
        }
        self.assertEqual(roles["implement"], "default")
        self.assertEqual(roles["verification_gate"], "default")
        self.assertEqual(roles["standards_review"], "standards-reviewer")
        self.assertEqual(roles["spec_review"], "spec-reviewer")
        self.assertEqual(roles["compliance"], "compliance_reviewer")

    def test_delivery_continues_one_plan_step_until_verification(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        implementation_edges = {
            edge.transition: edge
            for edge in spec.edges
            if edge.source == "implement"
        }

        continuation = implementation_edges["continue_implementation"]
        self.assertEqual(continuation.target, "implement")
        self.assertEqual(continuation.context, "continue_session")
        self.assertEqual(
            tuple(parameter.key for parameter in continuation.parameters),
            ("workspace_path", "plan_path"),
        )
        self.assertEqual(implementation_edges["verify"].target, "verification_dispatch")

    def test_install_adopts_byte_identical_managed_agent_file(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        kent_root = Path(temporary.name)
        source = REPO_ROOT / "agents" / "compliance_reviewer.md"
        target = kent_root / "agents" / source.name
        target.parent.mkdir(parents=True)
        target.write_bytes(source.read_bytes())

        environment = dict(os.environ)
        environment["KENT_PERSISTENCE_ROOT"] = str(kent_root)
        script = REPO_ROOT / "scripts" / "install"
        first = subprocess.run(
            [str(script)],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve(), source.resolve())

        backup = target.with_name(
            target.name + ".pre-kent-engineering-kit"
        )
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_bytes(), source.read_bytes())

        repeated = subprocess.run(
            [str(script)],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)

    def test_worktree_wrapper_clears_inherited_kent_context(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fake_bin = Path(temporary.name)
        fake_kent = fake_bin / "kent"
        fake_kent.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'printf "session=%s\\n" "${KENT_SESSION_ID-unset}"\n'
            'printf "run=%s\\n" "${KENT_RUN_ID-unset}"\n'
            'printf "step=%s\\n" "${KENT_STEP_ID-unset}"\n'
            'printf "args=%s\\n" "$*"\n'
        )
        fake_kent.chmod(0o755)
        environment = dict(os.environ)
        environment.update(
            {
                "PATH": f"{fake_bin}:{environment['PATH']}",
                "KENT_SESSION_ID": "session-appsome",
                "KENT_RUN_ID": "run-appsome",
                "KENT_STEP_ID": "step-appsome",
            }
        )

        result = subprocess.run(
            [
                str(REPO_ROOT / "scripts" / "kent-worktree"),
                "delete",
                "--session",
                "session-puber",
                "--json",
                "PUB-25",
            ],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "session=unset",
                "run=unset",
                "step=unset",
                (
                    "args=worktree delete --session session-puber "
                    "--json PUB-25"
                ),
            ],
        )

    def test_canary_uses_core_flow_without_device_or_delivery_tail(self) -> None:
        profile = self.load_profile()
        spec = build_canary_workflow(profile, 1)
        node_keys = {node.key for node in spec.nodes}
        by_key = {edge.key: edge for edge in spec.edges}

        self.assertEqual(spec.name, "Example Engineering Canary v1")
        self.assertEqual(spec.execution_target, "head")
        self.assertNotIn("smoke", node_keys)
        self.assertNotIn("prepare_pr", node_keys)
        self.assertNotIn("ci_monitor", node_keys)
        self.assertNotIn("waiting_pr", node_keys)
        self.assertNotIn("compliance", node_keys)
        self.assertIn("verification_join", node_keys)
        self.assertIn("cleanup", node_keys)
        prompts = "\n".join(
            edge.prompt for edge in spec.edges if edge.prompt is not None
        )
        self.assertNotIn(".kent/commands/feature-", prompts)
        self.assertNotIn("gate_smoke_required", by_key)
        self.assertEqual(by_key["gate_delivery_ready"].target, "cleanup")

    def test_pull_request_tail_uses_canonical_project_contract(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        by_key = {edge.key: edge for edge in spec.edges}

        self.assertEqual(by_key["prepare_pr_ci_monitor"].transition, "monitor_ci")
        self.assertEqual(
            tuple(parameter.key for parameter in by_key["prepare_pr_no_pr"].parameters),
            ("pr_report",),
        )
        self.assertTrue(by_key["prepare_pr_fix"].requires_approval)
        self.assertEqual(
            tuple(parameter.key for parameter in by_key["prepare_pr_fix"].parameters),
            ("workspace_path", "blocker_reason"),
        )
        self.assertEqual(by_key["ci_monitor_waiting_pr"].transition, "waiting_pr")
        self.assertTrue(by_key["waiting_pr_needs_user_action"].requires_approval)
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["waiting_pr_needs_user_action"].parameters
            ),
            ("workspace_path", "pr_url", "branch_name", "blocker_reason"),
        )
        self.assertTrue(by_key["waiting_pr_close_without_merge"].requires_approval)
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["waiting_pr_close_without_merge"].parameters
            ),
            ("workspace_path", "pr_report", "closure_reason"),
        )

    def test_final_compliance_separates_attestation_from_early_reviews(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        by_key = {edge.key: edge for edge in spec.edges}

        self.assertEqual(by_key["gate_delivery_ready"].target, "compliance")
        self.assertEqual(by_key["smoke_prepare_pr"].target, "compliance")
        self.assertEqual(by_key["compliance_prepare_pr"].target, "prepare_pr")
        self.assertEqual(by_key["compliance_prepare_pr"].transition, "ship_pr")
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["compliance_prepare_pr"].parameters
            ),
            ("workspace_path", "review_context", "compliance_report"),
        )
        self.assertEqual(by_key["compliance_fix"].target, "fix")
        self.assertEqual(
            tuple(parameter.key for parameter in by_key["compliance_fix"].parameters),
            ("workspace_path", "fix_context"),
        )
        self.assertTrue(by_key["compliance_needs_user_action"].requires_approval)
        self.assertEqual(
            by_key["compliance_needs_user_action"].target,
            "compliance",
        )
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["compliance_needs_user_action"].parameters
            ),
            ("workspace_path", "review_context", "blocker_reason"),
        )
        self.assertTrue(by_key["compliance_wont_do"].requires_approval)
        self.assertIn(
            "thin final attestation",
            by_key["gate_delivery_ready"].prompt,
        )
        self.assertIn(
            "Final Compliance Review: {{.Params.compliance_report}}",
            by_key["compliance_prepare_pr"].prompt,
        )

    def test_standards_and_final_compliance_capabilities_are_independent(self) -> None:
        no_standards = self.load_profile(
            lambda contents: contents.replace(
                "standards_review = true",
                "standards_review = false",
            )
        )
        no_standards_spec = build_delivery_workflow(no_standards, 1)
        no_standards_nodes = {node.key for node in no_standards_spec.nodes}
        no_standards_edges = {
            edge.key: edge for edge in no_standards_spec.edges
        }
        self.assertNotIn("standards_review", no_standards_nodes)
        self.assertIn("compliance", no_standards_nodes)
        self.assertNotIn(
            "Standards Review",
            no_standards_edges["gate_delivery_ready"].prompt,
        )

        no_compliance = self.load_profile(
            lambda contents: contents.replace(
                "compliance_review = true",
                "compliance_review = false",
            )
        )
        no_compliance_spec = build_delivery_workflow(no_compliance, 1)
        no_compliance_nodes = {node.key for node in no_compliance_spec.nodes}
        no_compliance_edges = {
            edge.key: edge for edge in no_compliance_spec.edges
        }
        self.assertIn("standards_review", no_compliance_nodes)
        self.assertNotIn("compliance", no_compliance_nodes)
        self.assertEqual(
            no_compliance_edges["gate_delivery_ready"].target,
            "prepare_pr",
        )

        no_pr_no_compliance_role = self.load_profile(
            lambda contents: (
                contents.replace("pull_requests = true", "pull_requests = false")
                .replace("ci_monitoring = true", "ci_monitoring = false")
                .replace('compliance = "compliance_reviewer"\n', "")
            )
        )
        no_pr_spec = build_delivery_workflow(no_pr_no_compliance_role, 1)
        self.assertNotIn(
            "compliance",
            {node.key for node in no_pr_spec.nodes},
        )

    def test_conditional_smoke_splits_gate_with_explicit_decision_data(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        by_key = {edge.key: edge for edge in spec.edges}

        self.assertIn("smoke", {node.key for node in spec.nodes})
        self.assertEqual(
            by_key["gate_smoke_required"].transition,
            "smoke_required",
        )
        self.assertEqual(by_key["gate_smoke_required"].target, "smoke")
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["gate_smoke_required"].parameters
            ),
            (
                "workspace_path",
                "review_context",
                "smoke_rationale",
                "smoke_scope",
            ),
        )
        self.assertEqual(
            by_key["gate_delivery_ready"].transition,
            "delivery_ready",
        )
        self.assertEqual(by_key["gate_delivery_ready"].target, "compliance")
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["gate_delivery_ready"].parameters
            ),
            ("workspace_path", "review_context", "smoke_rationale"),
        )
        self.assertIn(
            "Uncertainty must route to `smoke_required`",
            by_key["verification_join_gate"].prompt,
        )

    def test_required_smoke_has_no_gate_bypass(self) -> None:
        profile = self.load_profile(
            lambda contents: contents.replace(
                'smoke = "conditional"',
                'smoke = "required"',
            )
        )
        spec = build_delivery_workflow(profile, 1)
        by_key = {edge.key: edge for edge in spec.edges}

        self.assertIn("gate_smoke_required", by_key)
        self.assertNotIn("gate_delivery_ready", by_key)

    def test_disabled_smoke_has_no_smoke_node(self) -> None:
        profile = self.load_profile(
            lambda contents: contents.replace(
                'smoke = "conditional"',
                'smoke = "disabled"',
            )
        )
        spec = build_delivery_workflow(profile, 1)
        by_key = {edge.key: edge for edge in spec.edges}

        self.assertNotIn("smoke", {node.key for node in spec.nodes})
        self.assertNotIn("gate_smoke_required", by_key)
        self.assertEqual(by_key["gate_delivery_ready"].target, "compliance")

    def test_smoke_lab_exercises_both_gate_paths_without_delivery_tail(self) -> None:
        profile = self.load_profile()
        spec = build_smoke_lab_workflow(profile)
        node_keys = {node.key for node in spec.nodes}
        by_key = {edge.key: edge for edge in spec.edges}

        self.assertEqual(spec.name, "Example Engineering Smoke Lab")
        self.assertEqual(spec.execution_target, "head")
        self.assertIn("smoke", node_keys)
        self.assertNotIn("prepare_pr", node_keys)
        self.assertNotIn("ci_monitor", node_keys)
        self.assertNotIn("waiting_pr", node_keys)
        self.assertNotIn("compliance", node_keys)
        self.assertEqual(by_key["gate_smoke_required"].target, "smoke")
        self.assertEqual(by_key["gate_delivery_ready"].target, "cleanup")
        self.assertEqual(by_key["smoke_cleanup"].target, "cleanup")

    def test_smoke_lab_supports_free_form_rollover_label(self) -> None:
        profile = self.load_profile()
        spec = build_smoke_lab_workflow(profile, label="iteration beta")
        self.assertEqual(
            spec.name,
            "Example Engineering Smoke Lab iteration beta",
        )

    def test_smoke_lab_rejects_nonconditional_policy(self) -> None:
        for policy in ("disabled", "required"):
            with self.subTest(policy=policy):
                profile = self.load_profile(
                    lambda contents, policy=policy: contents.replace(
                        'smoke = "conditional"',
                        f'smoke = "{policy}"',
                    )
                )
                with self.assertRaisesRegex(
                    SpecError,
                    "requires conditional Smoke policy",
                ):
                    build_smoke_lab_workflow(profile)

    def test_smoke_lab_label_is_rejected_for_other_workflow_kinds(self) -> None:
        profile = self.load_profile()
        with self.assertRaisesRegex(SpecError, "only for smoke-lab"):
            profile.workflow_name(
                "delivery",
                version=1,
                label="iteration beta",
            )

    def test_labeled_snapshot_names_do_not_collide_after_slugging(self) -> None:
        first = snapshot_filename(
            "Example Engineering Smoke Lab iteration beta",
            disambiguate=True,
        )
        second = snapshot_filename(
            "Example Engineering Smoke Lab iteration-beta",
            disambiguate=True,
        )
        self.assertNotEqual(first, second)
        self.assertTrue(first.endswith(".json"))
        self.assertTrue(second.endswith(".json"))

    def test_unlabelled_snapshot_name_remains_readable(self) -> None:
        self.assertEqual(
            snapshot_filename(
                "Example Engineering Smoke Lab",
                disambiguate=False,
            ),
            "example-engineering-smoke-lab.json",
        )

    def test_cancellation_edges_require_closure_reason(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        cancellation_edges = [
            edge for edge in spec.edges if edge.transition == "wont_do"
        ]
        self.assertGreater(len(cancellation_edges), 0)
        for edge in cancellation_edges:
            self.assertTrue(edge.requires_approval)
            self.assertEqual(
                tuple(parameter.key for parameter in edge.parameters),
                ("closure_reason",),
            )

    def test_every_cancellable_node_prompt_documents_closure_reason(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        cancellable_nodes = {
            edge.source for edge in spec.edges if edge.transition == "wont_do"
        }

        for node_key in cancellable_nodes:
            incoming = [
                edge
                for edge in spec.edges
                if edge.target == node_key and edge.prompt is not None
            ]
            self.assertGreater(len(incoming), 0, node_key)
            for edge in incoming:
                self.assertIn("closure_reason", edge.prompt, edge.key)

    def test_generated_prompts_have_no_trailing_whitespace(self) -> None:
        profile = self.load_profile()
        for spec in (
            build_delivery_workflow(profile, 1),
            build_canary_workflow(profile, 1),
        ):
            for edge in spec.edges:
                if edge.prompt is not None:
                    self.assertEqual(edge.prompt, edge.prompt.rstrip(), edge.key)

    def test_lite_profile_can_disable_optional_review_and_delivery_tail(self) -> None:
        def transform(contents: str) -> str:
            return (
                contents.replace('delivery_profile = "standard"', 'delivery_profile = "lite"')
                .replace("pull_requests = true", "pull_requests = false")
                .replace("ci_monitoring = true", "ci_monitoring = false")
                .replace("standards_review = true", "standards_review = false")
                .replace("compliance_review = true", "compliance_review = false")
                .replace("spec_review = true", "spec_review = false")
            )

        profile = self.load_profile(transform)
        spec = build_delivery_workflow(profile, 1)
        node_keys = {node.key for node in spec.nodes}
        self.assertNotIn("verification_join", node_keys)
        self.assertNotIn("prepare_pr", node_keys)
        self.assertNotIn("ci_monitor", node_keys)
        self.assertNotIn("standards_review", node_keys)
        self.assertNotIn("spec_review", node_keys)
        self.assertNotIn("compliance", node_keys)

    def test_profile_rejects_ci_without_pull_requests(self) -> None:
        def transform(contents: str) -> str:
            return contents.replace("pull_requests = true", "pull_requests = false")

        with self.assertRaisesRegex(SpecError, "ci_monitoring requires pull_requests"):
            self.load_profile(transform)

    def test_schema_three_preserves_legacy_review_capability_semantics(self) -> None:
        profile = self.load_profile(
            lambda contents: (
                contents.replace("standards_review = true\n", "")
                .replace('compliance = "compliance_reviewer"\n', "")
            )
        )
        spec = build_delivery_workflow(profile, 1)
        node_keys = {node.key for node in spec.nodes}
        by_key = {edge.key: edge for edge in spec.edges}

        self.assertTrue(profile.capability("standards_review"))
        self.assertFalse(profile.capability("compliance_review"))
        self.assertTrue(profile.legacy_review_contract)
        self.assertIn("standards_review", node_keys)
        self.assertNotIn("compliance", node_keys)
        self.assertEqual(by_key["gate_delivery_ready"].target, "prepare_pr")
        standards_edge = by_key["standards_report_join"]
        self.assertEqual(
            tuple(
                (parameter.key, parameter.description)
                for parameter in standards_edge.parameters
            ),
            (
                (
                    "standards_status",
                    "Repository standards status: passed, needs_changes, or blocked.",
                ),
                (
                    "compliance_report",
                    "Read-only repository standards and architecture compliance report.",
                ),
            ),
        )
        self.assertIn(
            "plus `compliance_report`",
            by_key["dispatch_standards_review"].prompt,
        )
        self.assertIn(
            "Standards report: {{.Params.compliance_report}}",
            by_key["verification_join_gate"].prompt,
        )
        self.assertNotIn(
            "{{.Params.standards_report}}",
            by_key["verification_join_gate"].prompt,
        )

    def test_profile_rejects_schema_two(self) -> None:
        with self.assertRaisesRegex(SpecError, "expected 3"):
            self.load_profile(
                lambda contents: (
                    contents.replace(
                        "schema_version = 3",
                        "schema_version = 2",
                    ).replace(
                        '[policies]\nsmoke = "conditional"\n\n',
                        "",
                    )
                )
            )

    def test_profile_rejects_legacy_device_smoke_capability(self) -> None:
        with self.assertRaisesRegex(SpecError, "device_smoke was removed"):
            self.load_profile(
                lambda contents: contents.replace(
                    "managed_worktrees = true",
                    "managed_worktrees = true\ndevice_smoke = true",
                )
            )

    def test_profile_rejects_unknown_smoke_policy(self) -> None:
        with self.assertRaisesRegex(SpecError, "unsupported policies.smoke"):
            self.load_profile(
                lambda contents: contents.replace(
                    'smoke = "conditional"',
                    'smoke = "sometimes"',
                )
            )

    def test_profile_requires_declared_adapter_keys(self) -> None:
        with self.assertRaisesRegex(
            SpecError,
            "required adapter 'mobile_resource_lock'",
        ):
            self.load_profile(
                lambda contents: contents.replace(
                    "required_adapters = []",
                    'required_adapters = ["mobile_resource_lock"]',
                )
            )

    def test_required_adapter_must_exist_and_be_user_executable(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        profile_directory = root / ".kent"
        profile_directory.mkdir()
        contents = EXAMPLE_PROFILE.read_text().replace(
            "required_adapters = []",
            'required_adapters = ["mobile_resource_lock"]',
        )
        contents += (
            "\n[adapters]\n"
            'mobile_resource_lock = '
            '".kent/adapters/mobile/emulator-resource-lock.sh"\n'
        )
        (profile_directory / "workflow-profile.toml").write_text(contents)

        with self.assertRaisesRegex(SpecError, "required adapter not found"):
            ProjectProfile.load(root)

        adapter = (
            root / ".kent" / "adapters" / "mobile" / "emulator-resource-lock.sh"
        )
        adapter.parent.mkdir(parents=True)
        adapter.write_text("#!/usr/bin/env bash\n")
        adapter.chmod(0o001)
        with self.assertRaisesRegex(
            SpecError,
            "not executable by the current user",
        ):
            ProjectProfile.load(root)

        adapter.chmod(0o755)
        profile = ProjectProfile.load(root)
        self.assertEqual(
            profile.adapter("mobile_resource_lock"),
            ".kent/adapters/mobile/emulator-resource-lock.sh",
        )

    def test_required_adapter_rejects_symlink_path_components(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        profile_directory = root / ".kent"
        profile_directory.mkdir()
        contents = EXAMPLE_PROFILE.read_text().replace(
            "required_adapters = []",
            'required_adapters = ["mobile_resource_lock"]',
        )
        contents += (
            "\n[adapters]\n"
            'mobile_resource_lock = '
            '".kent/adapters/mobile/emulator-resource-lock.sh"\n'
        )
        (profile_directory / "workflow-profile.toml").write_text(contents)

        unrelated = root / "unrelated.sh"
        unrelated.write_text("#!/usr/bin/env bash\n")
        unrelated.chmod(0o755)
        adapter = (
            root / ".kent" / "adapters" / "mobile" / "emulator-resource-lock.sh"
        )
        adapter.parent.mkdir(parents=True)
        adapter.symlink_to(unrelated)

        with self.assertRaisesRegex(SpecError, "must not contain symlinks"):
            ProjectProfile.load(root)

    def test_sync_project_adapters_installs_and_updates_declared_adapter(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        profile_directory = root / ".kent"
        profile_directory.mkdir()
        contents = EXAMPLE_PROFILE.read_text() + (
            "\n[adapters]\n"
            'mobile_evidence_audit = '
            '".kent/adapters/mobile/mobile-evidence-audit.sh"\n'
            'mobile_resource_lock = '
            '".kent/adapters/mobile/emulator-resource-lock.sh"\n'
        )
        (profile_directory / "workflow-profile.toml").write_text(contents)
        script = REPO_ROOT / "scripts" / "sync-project-adapters"
        target = (
            root / ".kent" / "adapters" / "mobile" / "emulator-resource-lock.sh"
        )
        evidence_target = (
            root / ".kent" / "adapters" / "mobile" / "mobile-evidence-audit.sh"
        )

        created = subprocess.run(
            [str(script), "--project", str(root)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertTrue(target.is_file())
        self.assertTrue(target.stat().st_mode & 0o111)
        self.assertTrue(evidence_target.is_file())
        self.assertTrue(evidence_target.stat().st_mode & 0o111)
        self.assertEqual(
            evidence_target.read_bytes(),
            (
                REPO_ROOT
                / "templates"
                / "project"
                / "mobile-evidence-audit.sh"
            ).read_bytes(),
        )

        target.write_text("#!/usr/bin/env bash\necho foreign\n")
        refused = subprocess.run(
            [str(script), "--project", str(root)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(refused.returncode, 1)
        self.assertIn("rerun with --update", refused.stderr)

        updated = subprocess.run(
            [str(script), "--project", str(root), "--update"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertEqual(
            target.read_bytes(),
            (
                REPO_ROOT
                / "templates"
                / "project"
                / "emulator-resource-lock.sh"
            ).read_bytes(),
        )

        unrelated = root / "unrelated.sh"
        unrelated.write_text("#!/usr/bin/env bash\necho keep\n")
        unrelated_before = unrelated.read_bytes()
        target.unlink()
        target.symlink_to(unrelated)
        refused_symlink = subprocess.run(
            [str(script), "--project", str(root), "--update"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(refused_symlink.returncode, 1)
        self.assertIn("symlink", refused_symlink.stderr)
        self.assertEqual(unrelated.read_bytes(), unrelated_before)

    def test_profile_accepts_newer_minimum_kent_version(self) -> None:
        profile = self.load_profile(
            lambda contents: contents.replace(
                'minimum_kent_version = "2.3.0"',
                'minimum_kent_version = "2.4.1"',
            )
        )
        self.assertEqual(profile.minimum_version_tuple(), (2, 4, 1))

    def test_profile_rejects_older_minimum_kent_version(self) -> None:
        with self.assertRaisesRegex(SpecError, "2.3.0 or newer"):
            self.load_profile(
                lambda contents: contents.replace(
                    'minimum_kent_version = "2.3.0"',
                    'minimum_kent_version = "2.2.9"',
                )
            )

    def test_execution_target_rejects_whitespace_in_revision(self) -> None:
        for target in ("ref:", "ref: ", "ref: main", "ref:main "):
            with self.subTest(target=target):
                with self.assertRaises(SpecError):
                    validate_execution_target(target)

    def test_execution_target_policy_normalization(self) -> None:
        self.assertEqual(
            execution_target_from_policy({"mode": "ask_on_first_execution"}),
            "ask-on-first-execution",
        )
        self.assertEqual(
            execution_target_from_policy({"mode": "default_branch"}),
            "default-branch",
        )
        self.assertEqual(
            execution_target_from_policy(
                {"mode": "custom_ref", "custom_ref": "refs/tags/v1"}
            ),
            "ref:refs/tags/v1",
        )

    def test_context_source_normalization(self) -> None:
        self.assertEqual(
            context_source_string({"kind": "previous_target_or_new"}),
            "previous_target_or_new",
        )
        self.assertEqual(
            context_source_string({"kind": "node", "node_key": "implement"}),
            "node:implement",
        )

    def test_empty_kent_graph_indexes_as_no_edges(self) -> None:
        definition = {
            "nodes": [],
            "edges": None,
            "transition_groups": None,
            "derived_wiring": None,
        }
        self.assertEqual(edge_index(definition), {})

    def test_exact_graph_rejects_node_semantic_mismatch(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        spec = WorkflowSpec(
            name="Minimal v1",
            description="Minimal workflow.",
            execution_target="head",
            nodes=(
                NodeSpec("backlog", "start", "Backlog"),
                NodeSpec("done", "terminal", "Done"),
            ),
            edges=(),
        )
        definition = {
            "workflow": {
                "description": "Minimal workflow.",
                "execution_target_policy": {"mode": "head"},
            },
            "nodes": [
                {
                    "id": "node-backlog",
                    "key": "backlog",
                    "kind": "start",
                    "display_name": "Backlog",
                },
                {
                    "id": "node-done",
                    "key": "done",
                    "kind": "terminal",
                    "display_name": "Wrong",
                },
            ],
            "edges": None,
            "transition_groups": None,
            "derived_wiring": None,
        }

        client = KentClient(Path(temporary.name))
        with self.assertRaisesRegex(SpecError, "semantic mismatch"):
            client.assert_exact_graph(spec, definition)

    def test_apply_preflight_rejects_extra_edge_without_cli_mutation(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        spec = WorkflowSpec(
            name="Minimal v1",
            description="Minimal workflow.",
            execution_target="head",
            nodes=(
                NodeSpec("backlog", "start", "Backlog"),
                NodeSpec("done", "terminal", "Done"),
            ),
            edges=(),
        )
        definition = {
            "workflow": {
                "id": "workflow-minimal",
                "name": "Minimal v1",
                "description": "Old description.",
                "execution_target_policy": {"mode": "head"},
            },
            "nodes": [
                {
                    "id": "node-backlog",
                    "key": "backlog",
                    "kind": "start",
                    "display_name": "Backlog",
                },
                {
                    "id": "node-done",
                    "key": "done",
                    "kind": "terminal",
                    "display_name": "Done",
                },
            ],
            "transition_groups": [
                {
                    "id": "group-extra",
                    "source_node_id": "node-backlog",
                    "transition_id": "finish",
                    "description": "Extra.",
                }
            ],
            "edges": [
                {
                    "id": "edge-extra",
                    "key": "extra",
                    "transition_group_id": "group-extra",
                    "target_node_id": "node-done",
                    "context_mode": "new_session",
                    "context_source": {"kind": "immediate_source"},
                    "requires_approval": False,
                    "prompt_template": None,
                }
            ],
            "derived_wiring": {
                "edges": [
                    {
                        "edge_id": "edge-extra",
                        "required_provision_fields": [],
                    }
                ]
            },
        }
        commands: list[list[str]] = []
        client = KentClient(Path(temporary.name))
        client.require_version = lambda *version: None
        client.inspect = lambda workflow: definition
        client.run_json = lambda args: commands.append(args) or {}

        with self.assertRaisesRegex(SpecError, "unexpected edges"):
            client.apply(spec)
        self.assertEqual(commands, [])

    def test_apply_preflight_rejects_graph_mutation_when_tasks_exist(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        spec = WorkflowSpec(
            name="Minimal v1",
            description="New description.",
            execution_target="head",
            nodes=(
                NodeSpec("backlog", "start", "Backlog"),
                NodeSpec("done", "terminal", "Done"),
            ),
            edges=(),
        )
        definition = {
            "workflow": {
                "id": "workflow-minimal",
                "name": "Minimal v1",
                "description": "Old description.",
                "execution_target_policy": {"mode": "head"},
            },
            "nodes": [
                {
                    "id": "node-backlog",
                    "key": "backlog",
                    "kind": "start",
                    "display_name": "Backlog",
                },
                {
                    "id": "node-done",
                    "key": "done",
                    "kind": "terminal",
                    "display_name": "Done",
                },
            ],
            "transition_groups": None,
            "edges": None,
            "derived_wiring": None,
        }
        commands: list[list[str]] = []
        client = KentClient(Path(temporary.name))
        client.require_version = lambda *version: None
        client.inspect = lambda workflow: definition
        client.workflow_has_tasks = lambda current: True
        client.run_json = lambda args: commands.append(args) or {}

        with self.assertRaisesRegex(SpecError, "has tasks and cannot be mutated"):
            client.apply(spec)
        self.assertEqual(commands, [])

    def test_workflow_task_check_scans_every_linked_project(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        definition = {
            "workflow": {
                "id": "workflow-shared",
                "name": "Shared Lab",
            }
        }
        calls: list[list[str]] = []
        client = KentClient(Path(temporary.name))

        def run(
            args: list[str],
            *,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args == ["project", "list"]:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=(
                        "project-one\tOne\t/repo/one\n"
                        "project-two\tTwo\t/repo/two\n"
                    ),
                    stderr="",
                )
            project_id = args[args.index("--project") + 1]
            tasks = [] if project_id == "project-one" else [{}]
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps({"tasks": tasks}),
                stderr="",
            )

        client.run = run

        self.assertTrue(client.workflow_has_tasks(definition))
        task_projects = [
            args[args.index("--project") + 1]
            for args in calls
            if args[:2] == ["task", "list"]
        ]
        self.assertEqual(task_projects, ["project-one", "project-two"])

    def test_edge_reconcile_clears_stale_prompt_and_context_source(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        definition = {
            "nodes": [
                {"id": "node-start", "key": "backlog"},
                {"id": "node-done", "key": "done"},
            ],
            "transition_groups": [
                {
                    "id": "group-start",
                    "source_node_id": "node-start",
                    "transition_id": "start",
                    "description": "Old description.",
                }
            ],
            "edges": [
                {
                    "id": "edge-start",
                    "key": "start_done",
                    "transition_group_id": "group-start",
                    "target_node_id": "node-done",
                    "context_mode": "compact_and_continue_session",
                    "context_source": {"kind": "previous_target_or_new"},
                    "requires_approval": False,
                    "prompt_template": "Stale prompt.",
                }
            ],
            "derived_wiring": {
                "edges": [
                    {
                        "edge_id": "edge-start",
                        "required_provision_fields": [],
                    }
                ]
            },
        }
        spec = EdgeSpec(
            key="start_done",
            source="backlog",
            transition="start",
            target="done",
            transition_description="Finish.",
        )
        captured: list[list[str]] = []
        client = KentClient(Path(temporary.name))
        client.run_json = lambda args: captured.append(args) or {}

        client.ensure_edge("Minimal v1", spec, definition)

        args = captured[0]
        self.assertEqual(args[args.index("--context-source") + 1], "immediate_source")
        self.assertEqual(args[args.index("--prompt") + 1], "")

    def test_model_rejects_fanout_branch_that_does_not_join(self) -> None:
        spec = WorkflowSpec(
            name="Broken v1",
            description="Broken fan-out.",
            execution_target="head",
            nodes=(
                NodeSpec("backlog", "start", "Backlog"),
                NodeSpec("dispatch", "script", "Dispatch", script_path="dispatch"),
                NodeSpec(
                    "one",
                    "agent",
                    "One",
                    agent="default",
                    completion_mode="shell_command",
                ),
                NodeSpec(
                    "two",
                    "agent",
                    "Two",
                    agent="default",
                    completion_mode="shell_command",
                ),
                NodeSpec("done", "terminal", "Done"),
            ),
            edges=(
                EdgeSpec(
                    "start_dispatch",
                    "backlog",
                    "start",
                    "dispatch",
                    transition_description="Start.",
                ),
                EdgeSpec(
                    "dispatch_one",
                    "dispatch",
                    "fanout",
                    "one",
                    prompt="Review.",
                    transition_description="Fan out.",
                ),
                EdgeSpec(
                    "dispatch_two",
                    "dispatch",
                    "fanout",
                    "two",
                    prompt="Review.",
                    transition_description="Fan out.",
                ),
                EdgeSpec(
                    "one_done",
                    "one",
                    "reported",
                    "done",
                    transition_description="Finish.",
                ),
                EdgeSpec(
                    "two_done",
                    "two",
                    "reported",
                    "done",
                    transition_description="Finish.",
                ),
            ),
        )
        with self.assertRaisesRegex(SpecError, "does not target Join"):
            spec.validate()

    def test_agent_targets_require_prompts(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        edge = next(edge for edge in spec.edges if edge.key == "start_plan")
        broken = replace(edge, prompt=None)
        broken_spec = replace(
            spec,
            edges=tuple(
                broken if candidate.key == edge.key else candidate
                for candidate in spec.edges
            ),
        )
        with self.assertRaisesRegex(SpecError, "without a prompt"):
            broken_spec.validate()


class VerificationReportTest(unittest.TestCase):
    def run_report(
        self,
        verifier_body: str,
        *,
        workflow_input: str = "{}",
        log_contents: str | None = None,
    ) -> dict[str, str]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        verifier = root / "verify"
        verifier.write_text("#!/usr/bin/env bash\n" + verifier_body)
        verifier.chmod(0o755)
        log_path = root / "verify.log"
        if log_contents is not None:
            log_path.write_text(log_contents)

        environment = os.environ.copy()
        environment["KENT_WORKFLOW_VERIFY_SCRIPT"] = str(verifier)
        environment["KENT_WORKFLOW_VERIFY_LOG"] = str(log_path)
        result = subprocess.run(
            [str(VERIFY_REPORT)],
            cwd=root,
            env=environment,
            input=workflow_input,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_passed_verifier_reports_passed_even_with_stderr_diagnostics(self) -> None:
        result = self.run_report(
            """echo "diagnostic" >&2
printf '%s\n' '{"transition":"passed","commentary":"ok","verification_report":"log"}'
"""
        )
        self.assertEqual(result["transition"], "reported")
        self.assertEqual(result["verification_status"], "passed")

    def test_code_failure_reports_needs_changes(self) -> None:
        result = self.run_report(
            """printf '%s\n' \
'{"transition":"failed","commentary":"tests failed","verification_report":"assertion"}'
"""
        )
        self.assertEqual(result["verification_status"], "needs_changes")

    def test_environment_failure_reports_blocked(self) -> None:
        result = self.run_report(
            """printf '%s\n' \
'{"transition":"failed","commentary":"compile failed","verification_report":"see log"}'
""",
            log_contents="SDK location not found",
        )
        self.assertEqual(result["verification_status"], "blocked")

    def test_stderr_environment_failure_is_case_insensitive(self) -> None:
        result = self.run_report(
            """echo "permission denied while opening toolchain" >&2
printf '%s\n' \
'{"transition":"failed","commentary":"compile failed","verification_report":"no log"}'
"""
        )
        self.assertEqual(result["verification_status"], "blocked")

    def test_nonzero_verifier_still_reports_blocked(self) -> None:
        result = self.run_report('echo "boom" >&2\nexit 2\n')
        self.assertEqual(result["transition"], "reported")
        self.assertEqual(result["verification_status"], "blocked")

    def test_malformed_verifier_output_still_reports_blocked(self) -> None:
        result = self.run_report("echo not-json\n")
        self.assertEqual(result["verification_status"], "blocked")

    def test_invalid_workflow_input_still_reports_blocked(self) -> None:
        result = self.run_report("exit 99\n", workflow_input="not-json")
        self.assertEqual(result["verification_status"], "blocked")


if __name__ == "__main__":
    unittest.main()
