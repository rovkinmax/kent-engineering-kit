from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest

from workflowkit.delivery import (
    build_canary_workflow,
    build_delivery_workflow,
    build_smoke_lab_workflow,
)
from workflowkit.kent import (
    KentClient,
    canonical_workflow_selector,
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
VERIFY_DISPATCH = (
    REPO_ROOT / "templates" / "project" / "workflow-verification-dispatch"
)
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


def role_prompt(filename: str) -> str:
    return (REPO_ROOT / "agents" / filename).read_text()


def create_work_kind_procedures(root: Path) -> None:
    for configured_path in WORK_KIND_PROCEDURES + CONTEXT_MANIFESTS:
        path = root / configured_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Test context\n")


class WorkflowKitTest(unittest.TestCase):
    def load_profile(self, transform=lambda value: value) -> ProjectProfile:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        profile_directory = root / ".kent"
        profile_directory.mkdir()
        contents = transform(EXAMPLE_PROFILE.read_text())
        (profile_directory / "workflow-profile.toml").write_text(contents)
        create_work_kind_procedures(root)
        return ProjectProfile.load(root)

    def test_global_role_tools_are_mutually_exclusive(self) -> None:
        config = tomllib.loads(
            (REPO_ROOT / "config" / "subagents.toml").read_text()
        )
        conflicts = []
        for name, role in config.get("subagents", {}).items():
            if not isinstance(role, dict):
                continue
            tools = role.get("tools", {})
            if (
                isinstance(tools, dict)
                and tools.get("patch") is True
                and tools.get("edit") is True
            ):
                conflicts.append(name)
        self.assertEqual(conflicts, [])

    def test_global_contract_localizes_user_facing_workflow_text(self) -> None:
        contract = (REPO_ROOT / "global" / "AGENTS.md").read_text()

        for expected in (
            "default to Russian",
            "transition commentary",
            "`blocker_reason`",
            "`closure_reason`",
            "do not paste raw review reports",
            "do not present task-scoped code fixes",
            "Missing agent-produced bookkeeping",
            "ordinary missing operational date",
            "`kent task resume` confirms durable requeueing",
        ):
            self.assertIn(expected, contract)

        workflow_contract = (
            REPO_ROOT / "contracts" / "workflow-contract.md"
        ).read_text()
        for expected in (
            "Retry only that\n  job",
            "`Fixes #N`",
        ):
            self.assertIn(expected, workflow_contract)

        release_manager = (
            REPO_ROOT / "agents" / "release-manager.md"
        ).read_text()
        self.assertIn(
            "current calendar date from the execution environment",
            release_manager,
        )
        self.assertIn(
            "today's date",
            release_manager,
        )

        ci_monitor = (REPO_ROOT / "agents" / "ci-monitor.md").read_text()
        for expected in (
            "The runner has received a shutdown signal",
            "The operation was canceled",
            "gh run rerun <run-id> --job <job-id>",
            "assertions",
            "external service `5xx` responses",
            "three total attempts",
            "failure before eligible tests actually started",
            "Never automatically retry",
        ):
            self.assertIn(expected, ci_monitor)

        smoke_tester = role_prompt("runtime-smoke-tester.md")
        for expected in (
            "task-started test runner",
            "task-owned orphan processes",
            "Never leave a resource unlocked while task-owned",
        ):
            self.assertIn(expected, smoke_tester)

    def test_instruction_ownership_keeps_runtime_prompts_bounded(self) -> None:
        profile = self.load_profile()
        by_key = {
            edge.key: edge.prompt or ""
            for edge in build_delivery_workflow(profile, 1).edges
        }
        budgets = {
            "start_plan": 6500,
            "plan_contract_implement": 5000,
            "gate_fix": 4000,
            "dispatch_standards_review": 1500,
            "dispatch_spec_review": 1300,
            "verification_join_gate": 3200,
            "gate_delivery_ready": 2600,
            "compliance_prepare_pr": 3600,
            "ci_watch_diagnose": 2800,
        }
        for key, maximum in budgets.items():
            self.assertLessEqual(
                len(by_key[key]),
                maximum,
                f"{key} exceeded its generated prompt budget",
            )

        self.assertLessEqual(
            (REPO_ROOT / "skills" / "kent-engineering-kit" / "SKILL.md")
            .stat()
            .st_size,
            8000,
        )
        self.assertLessEqual(
            (REPO_ROOT / "contracts" / "workflow-contract.md").stat().st_size,
            35000,
        )
        self.assertNotIn(
            "The runner has received a shutdown signal",
            by_key["ci_watch_diagnose"],
        )
        self.assertIn(
            "The runner has received a shutdown signal",
            role_prompt("ci-monitor.md"),
        )

    def test_team_delivery_has_direct_fanout_join(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        spec.validate()

        fanout = [
            edge
            for edge in spec.edges
            if edge.source == "verification_dispatch"
            and edge.transition == "verification_dispatch_fanout_verify"
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
        self.assertEqual(roles["plan"], "default")
        self.assertEqual(roles["plan_review"], "spec-reviewer")
        self.assertEqual(roles["plan_revalidation"], "default")
        self.assertEqual(roles["implement"], "implementation-worker")
        self.assertEqual(roles["verification_gate"], "workflow-gate")
        self.assertEqual(roles["fix"], "fix-worker")
        self.assertEqual(roles["smoke"], "runtime-smoke-tester")
        self.assertEqual(roles["standards_review"], "standards-reviewer")
        self.assertEqual(roles["spec_review"], "spec-reviewer")
        self.assertEqual(roles["compliance"], "compliance_reviewer")
        self.assertEqual(roles["prepare_pr"], "delivery-operator")
        self.assertEqual(roles["ci_monitor"], "ci-monitor")
        self.assertEqual(roles["waiting_pr"], "ci-monitor")
        self.assertEqual(roles["cleanup"], "delivery-operator")

    def test_jira_adapter_adds_relation_aware_plan_instruction(self) -> None:
        profile = self.load_profile()
        without_jira = build_delivery_workflow(profile, 1)
        without_jira_plan = next(
            edge.prompt for edge in without_jira.edges if edge.target == "plan"
        )
        self.assertNotIn("normalized `issue_links`", without_jira_plan)

        jira_profile = replace(
            profile,
            required_adapters=("jira_api",),
            adapters={"jira_api": ".kent/adapters/jira/jira-api.sh"},
        )
        with_jira = build_delivery_workflow(jira_profile, 1)
        with_jira_plan = next(
            edge.prompt for edge in with_jira.edges if edge.target == "plan"
        )
        self.assertIn("normalized `issue_links`", with_jira_plan)
        self.assertIn("mandatory bounded product evidence", with_jira_plan)
        self.assertIn("API operations/models", with_jira_plan)
        self.assertIn("only root issues explicitly supplied", with_jira_plan)
        self.assertIn(
            "`checked`, `adopted`, `rejected`, and\n`conflicts`",
            with_jira_plan,
        )
        self.assertIn("deferred/out-of-scope issues", with_jira_plan)

    def test_sentry_adapter_adds_bounded_plan_instruction(self) -> None:
        profile = self.load_profile()
        without_sentry = build_delivery_workflow(profile, 1)
        without_sentry_plan = next(
            edge.prompt for edge in without_sentry.edges if edge.target == "plan"
        )
        self.assertNotIn("explicitly identifies a Sentry issue", without_sentry_plan)

        sentry_profile = replace(
            profile,
            required_adapters=("sentry_issues",),
            adapters={
                "sentry_issues": ".kent/adapters/sentry/sentry-issues.sh",
            },
        )
        with_sentry = build_delivery_workflow(sentry_profile, 1)
        with_sentry_plan = next(
            edge.prompt for edge in with_sentry.edges if edge.target == "plan"
        )
        self.assertIn("explicitly identifies a Sentry issue", with_sentry_plan)
        self.assertIn("bounded latest-event evidence", with_sentry_plan)
        self.assertIn("never persist raw", with_sentry_plan)
        self.assertIn("mark-seen --allow-mutate", with_sentry_plan)
        self.assertIn("Do not resolve or mute", with_sentry_plan)

    def test_delivery_inserts_configured_branch_identity_before_implement(
        self,
    ) -> None:
        profile = self.load_profile(
            lambda contents: (
                contents.replace(
                    'issue_tracker = "none"',
                    'issue_tracker = "jira"',
                ).replace(
                    'branch_identity = "task"',
                    'branch_identity = "jira"',
                ).replace(
                    "[commands]\n",
                    "[commands]\n"
                    'branch_identity = '
                    '".kent/scripts/workflow-branch-identity"\n',
                )
            )
        )
        spec = build_delivery_workflow(profile, 1)
        nodes = {node.key: node for node in spec.nodes}
        edges = {edge.key: edge for edge in spec.edges}

        self.assertEqual(nodes["branch_identity"].kind, "script")
        self.assertEqual(
            nodes["branch_identity"].script_path,
            ".kent/scripts/workflow-branch-identity",
        )
        self.assertEqual(
            edges["start_plan"].target,
            "plan",
        )
        self.assertEqual(
            edges["plan_review"].target,
            "plan_review",
        )
        self.assertEqual(
            edges["plan_review_accept"].target,
            "plan_contract",
        )
        self.assertEqual(
            edges["plan_contract_branch_identity"].target,
            "branch_identity",
        )
        self.assertEqual(
            edges["branch_identity_implement"].target,
            "implement",
        )
        self.assertEqual(
            edges["branch_identity_implement"].transition,
            "branch_identity_ready",
        )
        self.assertEqual(
            edges["branch_identity_implement"].context_source,
            "immediate_source",
        )
        for key in (
            "plan_contract_branch_identity",
            "branch_identity_implement",
        ):
            self.assertEqual(
                tuple(parameter.key for parameter in edges[key].parameters),
                ("workspace_path", "plan_path", "work_kind"),
            )
        self.assertEqual(
            edges["branch_identity_resolution"].target,
            "branch_identity_resolution",
        )
        self.assertEqual(
            edges["branch_identity_retry"].target,
            "branch_identity",
        )
        self.assertNotIn("plan_implement", edges)

    def test_delivery_keeps_direct_plan_start_for_task_branch_policy(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        nodes = {node.key for node in spec.nodes}
        edges = {edge.key: edge for edge in spec.edges}

        self.assertNotIn("branch_identity", nodes)
        self.assertEqual(edges["start_plan"].target, "plan")
        self.assertEqual(edges["plan_review"].target, "plan_review")
        self.assertEqual(edges["plan_review_accept"].target, "plan_contract")

    def test_gate_role_falls_back_to_orchestrator(self) -> None:
        profile = self.load_profile(
            lambda contents: contents.replace('gate = "workflow-gate"\n', "")
        )
        spec = build_delivery_workflow(profile, 1)
        roles = {
            node.key: node.agent
            for node in spec.nodes
            if node.kind == "agent"
        }

        self.assertEqual(roles["verification_gate"], "default")

    def test_delivery_continues_one_plan_step_until_verification(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        by_key = {edge.key: edge for edge in spec.edges}
        implementation_edges = {
            edge.transition: edge
            for edge in spec.edges
            if edge.source == "implement"
        }

        continuation = implementation_edges["implement_continue_implementation"]
        self.assertEqual(continuation.target, "plan_contract")
        self.assertEqual(
            tuple(parameter.key for parameter in continuation.parameters),
            (
                "workspace_path",
                "plan_path",
                "work_kind",
                "plan_route",
                "review_context",
                "plan_contract_mode",
                "task_short_id",
            ),
        )
        self.assertEqual(
            implementation_edges["implement_verify"].target,
            "plan_contract",
        )
        self.assertIn(
            "writer-owned plan step",
            by_key["plan_contract_continue_implement"].prompt,
        )
        self.assertIn(
            "acquire a device",
            role_prompt("implementation-worker.md"),
        )
        self.assertIn(
            "when every\nwriter-owned plan step is complete",
            by_key["plan_contract_continue_implement"].prompt,
        )
        self.assertIn(
            "exact human-authored task-comment ID",
            by_key["start_plan"].prompt,
        )
        self.assertIn(
            "SDK or\nschema upgrades",
            by_key["start_plan"].prompt,
        )
        self.assertIn("pre-edit red run", by_key["start_plan"].prompt)
        self.assertIn(
            "Missing agent-produced evidence is not user authority work",
            role_prompt("implementation-worker.md"),
        )
        self.assertEqual(
            by_key["plan_contract_implement"].context,
            "new_session",
        )
        self.assertEqual(by_key["gate_fix"].context, "new_session")
        self.assertEqual(by_key["gate_fix"].context_source, "immediate_source")
        self.assertEqual(by_key["dispatch_invalid_workspace"].target, "fix")
        self.assertEqual(by_key["dispatch_invalid_workspace"].context, "new_session")
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["dispatch_invalid_workspace"].parameters
            ),
            ("reported_workspace_path", "fix_context"),
        )
        self.assertEqual(by_key["fix_continue"].context, "new_session")
        self.assertEqual(
            tuple(parameter.key for parameter in by_key["fix_continue"].parameters),
            ("workspace_path", "fix_context"),
        )
        for key in ("implement_needs_user_action", "fix_needs_user_action"):
            self.assertEqual(by_key[key].context, "new_session")
            self.assertIn("fresh bounded session", by_key[key].prompt)
            self.assertIn(
                "Read `.kent/context/implement.md` first",
                by_key[key].prompt,
            )
            self.assertIn(
                ".kent/scripts/workflow-evidence-ledger append",
                by_key[key].prompt,
            )
            self.assertIn(
                "Approval means only that the exact reported blocker action",
                by_key[key].prompt,
            )
            self.assertIn(
                "Do not approve until the reported external action is complete",
                by_key[key].transition_description,
            )
        self.assertIn(
            "legacy plan renders them as unchecked entries",
            by_key["implement_needs_user_action"].prompt,
        )
        for key in (
            "plan_needs_user_action",
            "smoke_needs_user_action",
            "compliance_needs_user_action",
            "prepare_pr_needs_user_action",
            "ci_monitor_needs_user_action",
            "waiting_pr_needs_user_action",
            "cleanup_needs_user_action",
        ):
            self.assertEqual(
                by_key[key].context,
                "compact_and_continue_session",
            )
        self.assertIn(
            "must stop for confirmation",
            by_key["start_plan"].prompt,
        )
        self.assertIn(
            "`not-applicable`",
            by_key["start_plan"].prompt,
        )

    def test_delivery_reviews_and_revalidates_the_plan_contract(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        nodes = {node.key: node for node in spec.nodes}
        by_key = {edge.key: edge for edge in spec.edges}

        self.assertEqual(nodes["plan_review"].agent, "spec-reviewer")
        self.assertEqual(nodes["plan_contract"].kind, "script")
        self.assertEqual(
            nodes["plan_contract"].script_path,
            ".kent/scripts/workflow-plan-contract",
        )
        self.assertEqual(nodes["plan_revalidation"].agent, "default")
        self.assertEqual(by_key["plan_review"].target, "plan_review")
        self.assertEqual(
            by_key["plan_review_revalidate"].target,
            "plan_revalidation",
        )
        self.assertEqual(
            by_key["plan_review_revalidate"].context_source,
            "node:plan",
        )
        self.assertEqual(
            by_key["plan_revalidation_review"].target,
            "plan_review",
        )
        self.assertEqual(
            by_key["plan_contract_revalidate"].target,
            "plan_revalidation",
        )
        self.assertEqual(
            by_key["plan_contract_verify"].target,
            "verification_dispatch",
        )
        self.assertIn(
            "checkbox state alone",
            by_key["plan_contract_revalidate"].prompt,
        )

    def test_writer_prompts_preserve_report_only_and_scope_boundaries(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        by_key = {edge.key: edge for edge in spec.edges}

        for filename in ("implementation-worker.md", "fix-worker.md"):
            prompt = role_prompt(filename)
            self.assertIn("do not edit tracked or staged files", prompt)
            self.assertIn("Do not ask\n  whether to absorb it", prompt)

        for key in ("plan_contract_implement", "gate_fix"):
            self.assertIn(
                "approval is a resume signal",
                by_key[key].prompt,
            )

    def test_delivery_preserves_continuous_writer_compatibility(self) -> None:
        profile = self.load_profile(
            lambda contents: contents.replace(
                'writer_sessions = "fresh_per_slice"\n',
                "",
            )
        )
        spec = build_delivery_workflow(profile, 1)
        by_key = {edge.key: edge for edge in spec.edges}

        self.assertEqual(profile.writer_session_policy(), "continuous")
        self.assertEqual(
            by_key["plan_contract_implement"].context,
            "compact_and_continue_session",
        )
        self.assertEqual(
            by_key["plan_contract_continue_implement"].context,
            "continue_session",
        )
        self.assertEqual(
            by_key["gate_fix"].context,
            "compact_and_continue_session",
        )
        self.assertEqual(
            by_key["gate_fix"].context_source,
            "previous_target_or_new",
        )
        self.assertNotIn("fix_continue", by_key)
        self.assertEqual(
            by_key["fix_needs_user_action"].context,
            "compact_and_continue_session",
        )
        self.assertIn(
            "one dependency-ordered repair bundle",
            by_key["gate_fix"].prompt,
        )
        self.assertIn(
            "resolve every compatible group",
            by_key["gate_fix"].prompt,
        )

    def test_continuous_writer_resumes_plan_after_branch_identity_script(
        self,
    ) -> None:
        profile = self.load_profile(
            lambda contents: (
                contents.replace('writer_sessions = "fresh_per_slice"\n', "")
                .replace(
                    'issue_tracker = "none"',
                    'issue_tracker = "jira"',
                )
                .replace(
                    'branch_identity = "task"',
                    'branch_identity = "jira"',
                )
                .replace(
                    "[commands]\n",
                    "[commands]\n"
                    'branch_identity = '
                    '".kent/scripts/workflow-branch-identity"\n',
                )
            )
        )
        by_key = {
            edge.key: edge
            for edge in build_delivery_workflow(profile, 1).edges
        }

        handoff = by_key["branch_identity_implement"]
        self.assertEqual(handoff.context, "compact_and_continue_session")
        self.assertEqual(handoff.context_source, "node:plan")

    def test_delivery_writer_prompts_reserve_final_review_for_graph(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        prompts = {edge.key: edge.prompt or "" for edge in spec.edges}

        self.assertIn(
            "Do not duplicate workflow-owned Standards",
            role_prompt("implementation-worker.md"),
        )
        self.assertIn(
            "Apply the `implementation-worker` role contract",
            prompts["plan_contract_implement"],
        )
        self.assertIn(
            "Do not duplicate workflow-owned Standards",
            role_prompt("fix-worker.md"),
        )
        self.assertIn(
            "Apply the `fix-worker` role contract",
            prompts["gate_fix"],
        )
        self.assertIn("checkpoint ref", prompts["start_plan"])
        self.assertIn("exact referenced comments", prompts["start_plan"])
        self.assertIn(
            "fresh writer session",
            prompts["plan_contract_implement"],
        )
        self.assertIn(
            "exactly one independently verifiable fix slice",
            prompts["gate_fix"],
        )
        self.assertIn("`fix_continue_fix`", prompts["gate_fix"])
        self.assertIn(
            "Do not create a transition-only session",
            prompts["gate_fix"],
        )
        self.assertIn(
            "bookkeeping-only evidence",
            prompts["gate_fix"],
        )
        self.assertIn(
            "exactly one independently verifiable PR or branch recovery slice",
            prompts["prepare_pr_fix"],
        )
        self.assertIn("`fix_continue_fix`", prompts["prepare_pr_fix"])
        self.assertIn(
            "exactly one independently verifiable PR-feedback slice",
            prompts["waiting_pr_fix"],
        )
        self.assertIn("`fix_continue_fix`", prompts["waiting_pr_fix"])
        self.assertIn(
            "Fix only findings proven to be task-scoped",
            role_prompt("fix-worker.md"),
        )
        self.assertIn(
            "git branch --show-current",
            prompts["compliance_prepare_pr"],
        )
        self.assertIn(
            "Read the task's current\n`source_url`",
            prompts["compliance_prepare_pr"],
        )

    def test_standards_and_gate_require_task_scoped_differential_evidence(
        self,
    ) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        by_key = {edge.key: edge for edge in spec.edges}

        standards_prompt = by_key["dispatch_standards_review"].prompt or ""
        gate_prompt = by_key["verification_join_gate"].prompt or ""
        standards_role = role_prompt("standards-reviewer.md")
        for expected in (
            "whole-repository analyzer",
            "task-introduced or task-worsened",
            "changed file or touched method",
            "baseline debt",
            "policy\ncontradiction",
            "do not\nsubstitute a newer merge-target tip",
            "Target-only commits",
            "free-form strings",
            "typed source contract",
        ):
            self.assertIn(expected, standards_role)
        self.assertIn(
            "Apply the `standards-reviewer` role contract",
            standards_prompt,
        )
        spec_role = role_prompt("spec-reviewer.md")
        for expected in (
            "fixed point",
            "target-only commits",
            "Do not ask the writer to copy",
        ):
            self.assertIn(expected, spec_role)
        self.assertIn(
            "Apply the `spec-reviewer` role contract",
            by_key["dispatch_spec_review"].prompt or "",
        )
        for expected in (
            "Route task-scoped failures to Fix",
            "Route missing authority",
            "Missing agent-produced bookkeeping is not a user decision",
            "target-only commits",
            "merge/replay conflict",
        ):
            self.assertIn(expected, role_prompt("workflow-gate.md"))
        self.assertIn("Apply the `workflow-gate` role contract", gate_prompt)
        self.assertIn("deduplicated, dependency-ordered repair bundle", gate_prompt)
        self.assertIn(
            "dependency-ordered repair bundle",
            role_prompt("workflow-gate.md"),
        )
        self.assertIn(
            "stable ID",
            role_prompt("workflow-gate.md"),
        )
        self.assertIn(
            "one-finding-per-session",
            role_prompt("workflow-gate.md"),
        )
        self.assertIn(
            "linked, cloned, sibling, and dependency issues",
            spec_role,
        )

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

    def test_worktree_wrapper_uses_explicit_binary_with_restricted_path(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fake_kent = Path(temporary.name) / "kent-cli"
        fake_kent.write_text(
            "#!/bin/sh\n"
            'printf "args=%s\\n" "$*"\n'
        )
        fake_kent.chmod(0o755)
        environment = dict(os.environ)
        environment.update(
            {
                "PATH": "/usr/bin:/bin",
                "KENT_BIN": str(fake_kent),
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
                "session-sdk",
                "--delete-branch",
                "--json",
                "OSDK-2",
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
                (
                    "args=worktree delete --session session-sdk "
                    "--delete-branch --json OSDK-2"
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
        self.assertIn("work_kind", prompts)
        self.assertIn(".kent/commands/feature-start.md", prompts)
        self.assertNotIn("gate_smoke_required", by_key)
        self.assertEqual(by_key["gate_delivery_ready"].target, "cleanup")

    def test_pull_request_tail_uses_canonical_project_contract(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        by_key = {edge.key: edge for edge in spec.edges}

        self.assertEqual(
            by_key["prepare_pr_ci_watch"].transition,
            "prepare_pr_monitor_ci",
        )
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["prepare_pr_ci_watch"].parameters
            ),
            ("workspace_path", "pr_url", "branch_name", "merge_strategy"),
        )
        self.assertIsNone(by_key["prepare_pr_ci_watch"].prompt)
        self.assertEqual(by_key["ci_watch_waiting_pr"].target, "waiting_pr")
        self.assertEqual(by_key["ci_watch_diagnose"].target, "ci_monitor")
        self.assertEqual(by_key["ci_watch_merged"].target, "cleanup")
        self.assertEqual(
            tuple(parameter.key for parameter in by_key["prepare_pr_no_pr"].parameters),
            ("pr_report",),
        )

        self.assertTrue(by_key["prepare_pr_fix"].requires_approval)
        self.assertEqual(
            tuple(parameter.key for parameter in by_key["prepare_pr_fix"].parameters),
            ("workspace_path", "blocker_reason"),
        )
        self.assertEqual(
            by_key["ci_monitor_waiting_pr"].transition,
            "ci_monitor_waiting_pr",
        )
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["ci_monitor_watch"].parameters
            ),
            (
                "workspace_path",
                "pr_url",
                "branch_name",
                "merge_strategy",
                "ci_report",
            ),
        )
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["ci_monitor_waiting_pr"].parameters
            ),
            (
                "workspace_path",
                "pr_url",
                "branch_name",
                "merge_strategy",
                "ci_report",
            ),
        )
        self.assertEqual(
            by_key["ci_monitor_merged"].transition,
            "ci_monitor_pr_merged",
        )
        self.assertEqual(by_key["ci_monitor_merged"].target, "cleanup")
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["ci_monitor_merged"].parameters
            ),
            ("workspace_path", "pr_url", "branch_name", "merge_report"),
        )
        self.assertIn(
            "never route the merged task branch to Fix",
            by_key["ci_watch_diagnose"].prompt,
        )
        self.assertIn(
            "task-differential evidence",
            role_prompt("ci-monitor.md"),
        )
        self.assertIn(
            "exact terminal watcher report",
            by_key["ci_watch_diagnose"].prompt,
        )
        self.assertIn(
            "Never use `ci_monitor_needs_user_action` merely because CI is still running",
            by_key["ci_watch_diagnose"].prompt,
        )
        for expected in (
            "The runner has received a shutdown signal",
            "The operation was canceled",
            "gh run rerun <run-id> --job <job-id>",
            "assertions",
            "external service `5xx` responses",
            "three total attempts",
            "failure before eligible tests actually started",
        ):
            self.assertIn(expected, role_prompt("ci-monitor.md"))
        self.assertIn("Fixes #N", by_key["compliance_prepare_pr"].prompt)
        self.assertEqual(
            by_key["fix_pr_merged_cleanup"].transition,
            "fix_pr_merged",
        )
        self.assertEqual(by_key["fix_pr_merged_cleanup"].target, "cleanup")
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["fix_pr_merged_cleanup"].parameters
            ),
            ("workspace_path", "pr_url", "branch_name", "merge_report"),
        )
        self.assertTrue(by_key["waiting_pr_needs_user_action"].requires_approval)
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["waiting_pr_needs_user_action"].parameters
            ),
            (
                "workspace_path",
                "pr_url",
                "branch_name",
                "merge_strategy",
                "blocker_reason",
            ),
        )
        self.assertEqual(
            tuple(parameter.key for parameter in by_key["waiting_pr_fix"].parameters),
            ("workspace_path", "merge_strategy", "pr_report"),
        )
        self.assertEqual(by_key["waiting_pr_watch_merge"].target, "merge_watch")
        self.assertFalse(by_key["waiting_pr_watch_merge"].requires_approval)
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["waiting_pr_watch_merge"].parameters
            ),
            (
                "workspace_path",
                "pr_url",
                "branch_name",
                "merge_strategy",
                "pr_head_oid",
                "pr_base_oid",
            ),
        )
        self.assertEqual(
            by_key["merge_watch_still_waiting"].target,
            "merge_watch",
        )
        self.assertEqual(
            by_key["merge_watch_state_changed"].target,
            "waiting_pr",
        )
        self.assertEqual(
            by_key["merge_watch_cleanup"].target,
            "cleanup",
        )
        self.assertEqual(
            by_key["waiting_pr_ci_monitor"].target,
            "ci_watch",
        )
        self.assertIn(
            "Merely waiting for review or merge is not a blocker",
            by_key["ci_monitor_waiting_pr"].prompt,
        )
        self.assertTrue(by_key["waiting_pr_close_without_merge"].requires_approval)
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["waiting_pr_close_without_merge"].parameters
            ),
            ("workspace_path", "pr_report", "closure_reason"),
        )
        generated_prompts = "\n".join(
            by_key[key].prompt or ""
            for key in (
                "compliance_prepare_pr",
                "ci_watch_diagnose",
                "ci_monitor_waiting_pr",
                "waiting_pr_needs_user_action",
                "waiting_pr_fix",
            )
        )
        role_contracts = "\n".join(
            (
                role_prompt("delivery-operator.md"),
                role_prompt("ci-monitor.md"),
            )
        )
        for expected in (
            "canBeRebased",
            "forced replay",
            "temporary clone or branch",
        ):
            self.assertIn(expected, role_contracts)
        for expected in (
            "force-with-lease",
            "outcome=needs_user_action",
            "kent-resolve-github-merge-strategy",
        ):
            self.assertIn(expected, generated_prompts)
        strategy_source = (
            REPO_ROOT / "workflowkit" / "merge_strategy.py"
        ).read_text()
        self.assertIn("required_linear_history", strategy_source)

    def test_manual_package_publish_topology_is_approval_gated_after_merge(
        self,
    ) -> None:
        profile = self.load_profile(
            lambda contents: (
                contents.replace(
                    'release_topology = "none"',
                    'release_topology = "manual-package-publish-after-main"',
                )
                .replace(
                    'publish = ""',
                    'publish = ".kent/commands/feature-start.md"',
                )
            )
        )
        spec = build_delivery_workflow(profile, 1)
        by_key = {edge.key: edge for edge in spec.edges}
        roles = {
            node.key: node.agent
            for node in spec.nodes
            if node.kind == "agent"
        }

        self.assertEqual(roles["publish_package"], "release-manager")
        for key in (
            "ci_watch_merged",
            "ci_monitor_merged",
            "fix_pr_merged_cleanup",
            "waiting_pr_cleanup",
            "merge_watch_cleanup",
        ):
            self.assertEqual(by_key[key].target, "publish_package")
            self.assertTrue(by_key[key].requires_approval)
            self.assertIn("exact merged source tree", by_key[key].prompt)
        self.assertEqual(by_key["publish_cleanup"].target, "cleanup")
        self.assertFalse(by_key["publish_cleanup"].requires_approval)
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["publish_cleanup"].parameters
            ),
            (
                "workspace_path",
                "pr_url",
                "branch_name",
                "merge_report",
                "publication_report",
            ),
        )
        self.assertEqual(
            by_key["publish_needs_user_action"].target,
            "publish_package",
        )
        self.assertTrue(by_key["publish_needs_user_action"].requires_approval)
        self.assertEqual(
            by_key["publish_needs_user_action"].context,
            "compact_and_continue_session",
        )
        publish_prompts = "\n".join(
            (
                by_key["merge_watch_cleanup"].prompt,
                by_key["publish_needs_user_action"].prompt,
            )
        )
        for expected in (
            "project-declared credential source",
            "ambient CLI authentication",
            "publish subprocess",
        ):
            self.assertIn(expected, publish_prompts)

    def test_manual_package_publish_requires_procedure_and_role(self) -> None:
        topology = (
            'release_topology = "manual-package-publish-after-main"'
        )
        with self.assertRaisesRegex(SpecError, "requires procedures.publish"):
            self.load_profile(
                lambda contents: contents.replace(
                    'release_topology = "none"',
                    topology,
                )
            )

        with self.assertRaisesRegex(
            SpecError,
            "profile role 'package_release' is required",
        ):
            self.load_profile(
                lambda contents: (
                    contents.replace(
                        'release_topology = "none"',
                        topology,
                    ).replace(
                        'publish = ""',
                        'publish = ".kent/commands/feature-start.md"',
                    ).replace(
                        'package_release = "release-manager"\n',
                        "",
                    )
                )
            )

    def test_dispatch_accepts_exact_git_execution_root(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)

        result = subprocess.run(
            [str(VERIFY_DISPATCH)],
            cwd=root,
            input=json.dumps(
                {
                    "workspace_path": str(root),
                    "review_context": "ready",
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["transition"],
            "verification_dispatch_fanout_verify",
        )
        self.assertEqual(payload["workspace_path"], str(root.resolve()))

    def test_dispatch_rejects_todo_artifact_as_workspace(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        artifact = root / ".todo" / "feature"
        artifact.mkdir(parents=True)

        result = subprocess.run(
            [str(VERIFY_DISPATCH)],
            cwd=root,
            input=json.dumps(
                {
                    "workspace_path": str(artifact),
                    "review_context": "ready",
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["transition"],
            "verification_dispatch_invalid_workspace",
        )
        self.assertEqual(payload["reported_workspace_path"], str(artifact))
        self.assertIn(str(root.resolve()), payload["fix_context"])

    def test_dispatch_rejects_missing_workspace(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        missing = root / "missing-workspace"

        result = subprocess.run(
            [str(VERIFY_DISPATCH)],
            cwd=root,
            input=json.dumps(
                {
                    "workspace_path": str(missing),
                    "review_context": "ready",
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["transition"],
            "verification_dispatch_invalid_workspace",
        )
        self.assertEqual(payload["reported_workspace_path"], str(missing))
        self.assertIn(str(root.resolve()), payload["fix_context"])

    def test_final_compliance_separates_attestation_from_early_reviews(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        by_key = {edge.key: edge for edge in spec.edges}

        self.assertEqual(by_key["gate_delivery_ready"].target, "compliance")
        self.assertEqual(by_key["smoke_prepare_pr"].target, "compliance")
        self.assertEqual(by_key["compliance_prepare_pr"].target, "prepare_pr")
        self.assertEqual(
            by_key["compliance_prepare_pr"].transition,
            "compliance_ship_pr",
        )
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
            "exact human-authored task-comment ID",
            role_prompt("spec-reviewer.md"),
        )
        self.assertIn(
            "exact human-authored task-comment ID",
            role_prompt("compliance_reviewer.md"),
        )
        self.assertIn(
            "Missing agent-produced bookkeeping is not a user decision",
            role_prompt("compliance_reviewer.md"),
        )
        self.assertIn(
            "Final Compliance Review: {{.Params.compliance_report}}",
            by_key["compliance_prepare_pr"].prompt,
        )
        self.assertEqual(
            by_key["compliance_evidence_repair"].target,
            "evidence_repair",
        )
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["compliance_evidence_repair"].parameters
            ),
            ("workspace_path", "review_context", "evidence_context"),
        )
        self.assertEqual(
            by_key["evidence_repair_compliance"].target,
            "compliance",
        )
        self.assertEqual(
            by_key["evidence_repair_fix"].target,
            "fix",
        )
        self.assertIn(
            "do not build, install, launch, reacquire a device",
            by_key["compliance_evidence_repair"].prompt,
        )
        self.assertIn(
            "Read `.kent/context/implement.md` first",
            by_key["evidence_repair_needs_user_action"].prompt,
        )
        self.assertIn(
            ".kent/scripts/workflow-evidence-ledger append",
            by_key["evidence_repair_needs_user_action"].prompt,
        )

    def test_managed_worktree_cleanup_uses_task_janitor(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        nodes = {node.key: node for node in spec.nodes}
        by_key = {edge.key: edge for edge in spec.edges}

        self.assertEqual(nodes["task_janitor"].kind, "script")
        self.assertEqual(
            nodes["task_janitor"].script_path,
            ".kent/scripts/workflow-task-janitor",
        )
        self.assertEqual(by_key["cleanup_task_janitor"].target, "task_janitor")
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["cleanup_task_janitor"].parameters
            ),
            (
                "workspace_path",
                "task_short_id",
                "pr_url",
                "branch_name",
                "merge_report",
                "cleanup_mode",
                "cleanup_session_id",
                "cleanup_report",
            ),
        )
        self.assertEqual(by_key["task_janitor_done"].target, "done")
        self.assertEqual(by_key["task_janitor_blocked"].target, "cleanup")
        self.assertIn(
            "after this resource-owning Cleanup session exits",
            by_key["waiting_pr_cleanup"].prompt,
        )
        self.assertIn(
            "kent worktree leave",
            by_key["waiting_pr_cleanup"].prompt,
        )
        for key in (
            "prepare_pr_no_pr",
            "waiting_pr_close_without_merge",
        ):
            self.assertIn(
                "`git branch --show-current`",
                by_key[key].prompt,
            )
            self.assertIn(
                "never use\n  `null`, `none`, `not-applicable`",
                by_key[key].prompt,
            )

    def test_fix_and_smoke_prompts_require_durable_checkpoints(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 1)
        prompts = {
            edge.key: edge.prompt or ""
            for edge in spec.edges
        }

        self.assertIn(
            ".kent/scripts/workflow-checkpoint",
            prompts["gate_fix"],
        )
        self.assertIn(
            "--stage fix",
            prompts["gate_fix"],
        )
        self.assertIn(
            "--stage smoke",
            prompts["gate_smoke_required"],
        )
        self.assertIn(
            ".kent/runtime/<task-short-id>/fix-checkpoint.json",
            role_prompt("fix-worker.md"),
        )
        self.assertIn(
            ".kent/runtime/<task-short-id>/smoke-checkpoint.json",
            role_prompt("runtime-smoke-tester.md"),
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
                .replace('ci = "ci-monitor"\n', "")
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
            "verification_gate_smoke_required",
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
            "verification_gate_delivery_ready",
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
            "Uncertainty must route to `verification_gate_smoke_required`",
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
            lambda contents: (
                contents.replace(
                    'smoke = "conditional"',
                    'smoke = "disabled"',
                ).replace(
                    'qa = "runtime-smoke-tester"\n',
                    "",
                )
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
            edge for edge in spec.edges if edge.transition.endswith("_wont_do")
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
            edge.source
            for edge in spec.edges
            if edge.transition.endswith("_wont_do")
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

    def test_profile_requires_explicit_standards_review_capability(self) -> None:
        with self.assertRaisesRegex(
            SpecError,
            "missing capability 'standards_review'",
        ):
            self.load_profile(
                lambda contents: contents.replace(
                    "standards_review = true\n",
                    "",
                )
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

    def test_profile_rejects_unknown_writer_session_policy(self) -> None:
        with self.assertRaisesRegex(
            SpecError,
            "unsupported policies.writer_sessions",
        ):
            self.load_profile(
                lambda contents: contents.replace(
                    'writer_sessions = "fresh_per_slice"',
                    'writer_sessions = "sometimes"',
                )
            )

    def test_profile_defaults_pr_merge_strategy_to_auto(self) -> None:
        profile = self.load_profile(
            lambda contents: contents.replace(
                'pr_merge_strategy = "auto"\n',
                "",
            )
        )
        self.assertEqual(profile.pr_merge_strategy(), "auto")

    def test_profile_accepts_every_pr_merge_strategy(self) -> None:
        for strategy in ("auto", "merge", "squash", "rebase"):
            with self.subTest(strategy=strategy):
                profile = self.load_profile(
                    lambda contents, strategy=strategy: contents.replace(
                        'pr_merge_strategy = "auto"',
                        f'pr_merge_strategy = "{strategy}"',
                    )
                )
                self.assertEqual(profile.pr_merge_strategy(), strategy)

    def test_profile_rejects_unknown_pr_merge_strategy(self) -> None:
        with self.assertRaisesRegex(
            SpecError,
            "unsupported policies.pr_merge_strategy",
        ):
            self.load_profile(
                lambda contents: contents.replace(
                    'pr_merge_strategy = "auto"',
                    'pr_merge_strategy = "fast-forward"',
                )
            )

    def test_profile_rejects_unknown_branch_identity_policy(self) -> None:
        with self.assertRaisesRegex(
            SpecError,
            "unsupported policies.branch_identity",
        ):
            self.load_profile(
                lambda contents: contents.replace(
                    'branch_identity = "task"',
                    'branch_identity = "magic"',
                )
            )

    def test_profile_requires_jira_tracker_for_jira_branch_identity(self) -> None:
        with self.assertRaisesRegex(
            SpecError,
            "requires issue_tracker = 'jira'",
        ):
            self.load_profile(
                lambda contents: contents.replace(
                    'branch_identity = "task"',
                    'branch_identity = "jira"',
                )
            )

    def test_profile_requires_branch_identity_command_when_enabled(self) -> None:
        with self.assertRaisesRegex(
            SpecError,
            "command 'branch_identity' is required",
        ):
            self.load_profile(
                lambda contents: (
                    contents.replace(
                        'issue_tracker = "none"',
                        'issue_tracker = "jira"',
                    )
                    .replace(
                        'branch_identity = "task"',
                        'branch_identity = "jira"',
                    )
                )
            )

    def test_profile_rejects_execution_policy_in_role_prompt_frontmatter(
        self,
    ) -> None:
        for field in ("model: sonnet", "tools: Read, Grep"):
            with self.subTest(field=field):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name)
                profile_directory = root / ".kent"
                profile_directory.mkdir()
                (profile_directory / "workflow-profile.toml").write_text(
                    EXAMPLE_PROFILE.read_text()
                )
                create_work_kind_procedures(root)
                role_directory = profile_directory / "subagents"
                role_directory.mkdir()
                (role_directory / "reviewer.md").write_text(
                    "---\n"
                    "name: reviewer\n"
                    f"{field}\n"
                    "---\n"
                    "\n"
                    "# Role\n"
                )

                with self.assertRaisesRegex(
                    SpecError,
                    "role prompts must not declare model or tools",
                ):
                    ProjectProfile.load(root)

    def test_profile_allows_model_text_outside_role_prompt_frontmatter(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        profile_directory = root / ".kent"
        profile_directory.mkdir()
        (profile_directory / "workflow-profile.toml").write_text(
            EXAMPLE_PROFILE.read_text()
        )
        create_work_kind_procedures(root)
        role_directory = profile_directory / "subagents"
        role_directory.mkdir()
        (role_directory / "researcher.md").write_text(
            "# Role\n\n"
            "Inspect the domain model: one bounded area at a time.\n"
        )

        ProjectProfile.load(root)

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
        create_work_kind_procedures(root)

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
        create_work_kind_procedures(root)

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
        contents = (
            EXAMPLE_PROFILE.read_text()
            .replace(
                "required_adapters = []",
                'required_adapters = ["jira_api", '
                '"android_apk_install", "mobile_evidence_audit", '
                '"mobile_resource_lock", '
                '"sentry_issues"]',
            )
            .replace(
                "kit_managed_adapters = []",
                'kit_managed_adapters = ["jira_api", '
                '"android_apk_install", "mobile_evidence_audit", '
                '"mobile_resource_lock", '
                '"sentry_issues"]',
            )
            .replace(
                "[commands]\n",
                "[commands]\n"
                'branch_identity = ".kent/scripts/workflow-branch-identity"\n',
            )
        ) + (
            "\n[adapters]\n"
            'android_apk_install = '
            '".kent/adapters/mobile/android-apk-install-preserve"\n'
            'jira_api = ".kent/adapters/jira/jira-api.sh"\n'
            'mobile_evidence_audit = '
            '".kent/adapters/mobile/mobile-evidence-audit.sh"\n'
            'mobile_resource_lock = '
            '".kent/adapters/mobile/emulator-resource-lock.sh"\n'
            'sentry_issues = ".kent/adapters/sentry/sentry-issues.sh"\n'
        )
        (profile_directory / "workflow-profile.toml").write_text(contents)
        create_work_kind_procedures(root)
        script = REPO_ROOT / "scripts" / "sync-project-adapters"
        target = (
            root / ".kent" / "adapters" / "mobile" / "emulator-resource-lock.sh"
        )
        install_target = (
            root
            / ".kent"
            / "adapters"
            / "mobile"
            / "android-apk-install-preserve"
        )
        evidence_target = (
            root / ".kent" / "adapters" / "mobile" / "mobile-evidence-audit.sh"
        )
        jira_target = root / ".kent" / "adapters" / "jira" / "jira-api.sh"
        sentry_target = (
            root / ".kent" / "adapters" / "sentry" / "sentry-issues.sh"
        )
        branch_identity_target = (
            root / ".kent" / "scripts" / "workflow-branch-identity"
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
        self.assertTrue(install_target.is_file())
        self.assertTrue(install_target.stat().st_mode & 0o111)
        self.assertTrue(evidence_target.is_file())
        self.assertTrue(evidence_target.stat().st_mode & 0o111)
        self.assertTrue(jira_target.is_file())
        self.assertTrue(jira_target.stat().st_mode & 0o111)
        self.assertTrue(sentry_target.is_file())
        self.assertTrue(sentry_target.stat().st_mode & 0o111)
        self.assertTrue(branch_identity_target.is_file())
        self.assertTrue(branch_identity_target.stat().st_mode & 0o111)
        self.assertEqual(
            install_target.read_bytes(),
            (
                REPO_ROOT
                / "templates"
                / "project"
                / "android-apk-install-preserve"
            ).read_bytes(),
        )
        self.assertEqual(
            jira_target.read_bytes(),
            (
                REPO_ROOT
                / "templates"
                / "project"
                / "jira-api.sh"
            ).read_bytes(),
        )
        self.assertEqual(
            sentry_target.read_bytes(),
            (
                REPO_ROOT
                / "templates"
                / "project"
                / "sentry-issues.sh"
            ).read_bytes(),
        )
        self.assertEqual(
            evidence_target.read_bytes(),
            (
                REPO_ROOT
                / "templates"
                / "project"
                / "mobile-evidence-audit.sh"
            ).read_bytes(),
        )
        self.assertEqual(
            branch_identity_target.read_bytes(),
            (
                REPO_ROOT
                / "templates"
                / "project"
                / "workflow-branch-identity"
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

    def test_sync_project_adapters_preserves_project_owned_adapter(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        profile_directory = root / ".kent"
        profile_directory.mkdir()
        policy = root / ".kent" / "adapters" / "mcp" / "policy"
        policy.parent.mkdir(parents=True)
        policy.write_text("#!/usr/bin/env bash\necho inherit\n")
        policy.chmod(0o755)
        before = policy.read_bytes()
        verifier = root / ".kent" / "scripts" / "workflow-verify"
        verifier.parent.mkdir(parents=True)
        verifier.write_text("#!/usr/bin/env bash\necho project verifier\n")
        verifier.chmod(0o755)
        verifier_before = verifier.read_bytes()
        contents = EXAMPLE_PROFILE.read_text() + (
            "\n[adapters]\n"
            'mcp_policy = ".kent/adapters/mcp/policy"\n'
        )
        (profile_directory / "workflow-profile.toml").write_text(contents)
        create_work_kind_procedures(root)
        script = REPO_ROOT / "scripts" / "sync-project-adapters"

        result = subprocess.run(
            [str(script), "--project", str(root), "--update"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["adapters"],
            [
                {
                    "adapter": "mcp_policy",
                    "status": "project-owned",
                    "target": str(policy.resolve()),
                }
            ],
        )
        self.assertEqual(policy.read_bytes(), before)
        self.assertEqual(verifier.read_bytes(), verifier_before)

    def test_sync_preserves_known_template_when_profile_marks_it_project_owned(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        profile_directory = root / ".kent"
        profile_directory.mkdir()
        jira = root / ".kent" / "adapters" / "jira" / "jira-api.sh"
        jira.parent.mkdir(parents=True)
        jira.write_text("#!/usr/bin/env bash\necho project release jira\n")
        jira.chmod(0o755)
        before = jira.read_bytes()
        contents = EXAMPLE_PROFILE.read_text().replace(
            "required_adapters = []",
            'required_adapters = ["jira_api"]',
        ) + (
            "\n[adapters]\n"
            'jira_api = ".kent/adapters/jira/jira-api.sh"\n'
        )
        (profile_directory / "workflow-profile.toml").write_text(contents)
        create_work_kind_procedures(root)

        result = subprocess.run(
            [
                str(REPO_ROOT / "scripts" / "sync-project-adapters"),
                "--project",
                str(root),
                "--update",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["adapters"][0]["status"], "project-owned")
        self.assertEqual(jira.read_bytes(), before)

    def test_profile_accepts_newer_minimum_kent_version(self) -> None:
        profile = self.load_profile(
            lambda contents: contents.replace(
                'minimum_kent_version = "2.5.0"',
                'minimum_kent_version = "2.5.1"',
            )
        )
        self.assertEqual(profile.minimum_version_tuple(), (2, 5, 1))

    def test_profile_rejects_older_minimum_kent_version(self) -> None:
        with self.assertRaisesRegex(SpecError, "2.5.0 or newer"):
            self.load_profile(
                lambda contents: contents.replace(
                    'minimum_kent_version = "2.5.0"',
                    'minimum_kent_version = "2.4.9"',
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
        self.assertEqual(
            context_source_string(
                {"kind": "selected_node", "node_key": "plan"}
            ),
            "node:plan",
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
                "id": "workflow-11111111-1111-4111-8111-111111111111",
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

    def test_apply_rejects_non_atomic_graph_mutation_when_tasks_exist(self) -> None:
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
                "id": "workflow-11111111-1111-4111-8111-111111111111",
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

        with self.assertRaisesRegex(
            SpecError,
            "edge-by-edge reconciliation is non-atomic",
        ):
            client.apply(spec)
        self.assertEqual(commands, [])

    def test_apply_explicit_workflow_selector_never_creates_duplicate(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        spec = WorkflowSpec(
            name="Generated v3",
            description="Generated.",
            execution_target="head",
            nodes=(
                NodeSpec("backlog", "start", "Backlog"),
                NodeSpec("done", "terminal", "Done"),
            ),
            edges=(),
        )
        commands: list[list[str]] = []
        client = KentClient(Path(temporary.name))
        client.require_version = lambda *version: None
        client.preflight_scripts = lambda current: None
        client.inspect = lambda workflow: None
        client.run_json = lambda args: commands.append(args) or {}

        with self.assertRaisesRegex(SpecError, "explicit workflow.*was not found"):
            client.apply(
                spec,
                workflow_selector="11111111-1111-4111-8111-111111111111",
            )
        self.assertEqual(commands, [])

    def test_workflow_task_check_scans_every_linked_project(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        bare_id = "22222222-2222-4222-8222-222222222222"
        for workflow_id in (bare_id, f"workflow-{bare_id}"):
            with self.subTest(workflow_id=workflow_id):
                definition = {
                    "workflow": {
                        "id": workflow_id,
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
                    if args[:2] == ["workflow", "list"]:
                        return subprocess.CompletedProcess(
                            args,
                            0,
                            stdout=json.dumps(
                                {
                                    "workflows": [{"id": bare_id}],
                                    "next_offset": None,
                                }
                            ),
                            stderr="",
                        )
                    tasks = [] if project_id == "project-one" else [{}]
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        stdout=json.dumps({"tasks": tasks}),
                        stderr="",
                    )

                client.run = run

                self.assertTrue(client.workflow_has_tasks(definition))
                task_calls = [
                    args for args in calls if args[:2] == ["task", "list"]
                ]
                self.assertEqual(
                    [
                        args[args.index("--project") + 1]
                        for args in task_calls
                    ],
                    ["project-one", "project-two"],
                )
                self.assertEqual(
                    {
                        args[args.index("--workflow") + 1]
                        for args in task_calls
                    },
                    {bare_id},
                )

    def test_workflow_task_check_skips_unlinked_projects(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        workflow_id = "33333333-3333-4333-8333-333333333333"
        definition = {
            "workflow": {
                "id": workflow_id,
                "name": "Unlinked Lab",
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
            if args[:2] == ["workflow", "list"]:
                workflows = (
                    [{"id": workflow_id}]
                    if project_id == "project-one"
                    else []
                )
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=json.dumps(
                        {
                            "workflows": workflows,
                            "next_offset": None,
                        }
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps({"tasks": []}),
                stderr="",
            )

        client.run = run

        self.assertFalse(client.workflow_has_tasks(definition))
        task_calls = [
            args for args in calls if args[:2] == ["task", "list"]
        ]
        self.assertEqual(len(task_calls), 1)
        self.assertEqual(
            task_calls[0][task_calls[0].index("--project") + 1],
            "project-one",
        )

    def test_link_targets_explicit_project_workspace(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        candidate = root / "candidate"
        primary = root / "primary"
        candidate.mkdir()
        primary.mkdir()
        calls: list[list[str]] = []
        client = KentClient(candidate, project_workspace=primary)
        client.run = lambda args, check=True: (
            calls.append(args)
            or subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")
        )

        client.link(
            "33333333-3333-4333-8333-333333333333",
            set_default=False,
        )

        self.assertEqual(
            calls,
            [
                [
                    "workflow",
                    "link",
                    str(primary.resolve()),
                    "33333333-3333-4333-8333-333333333333",
                    "--json",
                ]
            ],
        )

    def test_client_resolves_primary_git_worktree_for_project_link(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        primary = Path(temporary.name) / "primary"
        candidate = Path(temporary.name) / "candidate"
        subprocess.run(["git", "init", "-q", str(primary)], check=True)
        subprocess.run(
            ["git", "-C", str(primary), "config", "user.name", "Kent Test"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(primary),
                "config",
                "user.email",
                "kent@example.invalid",
            ],
            check=True,
        )
        (primary / "tracked").write_text("tracked\n")
        subprocess.run(["git", "-C", str(primary), "add", "tracked"], check=True)
        subprocess.run(
            ["git", "-C", str(primary), "commit", "-qm", "Initial"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(primary),
                "worktree",
                "add",
                "-qb",
                "candidate",
                str(candidate),
            ],
            check=True,
        )

        client = KentClient(candidate)

        self.assertEqual(client.workspace, candidate.resolve())
        self.assertEqual(client.project_workspace, primary.resolve())

    def test_workflow_name_resolution_uses_bare_uuid_selector(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        bare_id = "33333333-3333-4333-8333-333333333333"
        for listed_id in (bare_id, f"workflow-{bare_id}"):
            with self.subTest(listed_id=listed_id):
                calls: list[list[str]] = []
                client = KentClient(Path(temporary.name))

                def run(
                    args: list[str],
                    *,
                    check: bool = True,
                ) -> subprocess.CompletedProcess[str]:
                    calls.append(args)
                    if args[:2] == ["workflow", "list"]:
                        return subprocess.CompletedProcess(
                            args,
                            0,
                            stdout=json.dumps(
                                {
                                    "workflows": [
                                        {
                                            "id": listed_id,
                                            "name": "Shared Lab",
                                        }
                                    ],
                                    "next_offset": None,
                                }
                            ),
                            stderr="",
                        )
                    if args[:2] == ["workflow", "inspect"]:
                        return subprocess.CompletedProcess(
                            args,
                            0,
                            stdout=json.dumps(
                                {
                                    "workflow": {
                                        "id": listed_id,
                                        "name": "Shared Lab",
                                    }
                                }
                            ),
                            stderr="",
                        )
                    raise AssertionError(args)

                client.run = run

                definition = client.inspect("Shared Lab")

                self.assertEqual(definition["workflow"]["id"], listed_id)
                self.assertIn(
                    ["workflow", "inspect", listed_id, "--json"],
                    calls,
                )

    def test_canonical_workflow_selector_accepts_old_and_new_ids(self) -> None:
        bare_id = "44444444-4444-4444-8444-444444444444"
        self.assertEqual(canonical_workflow_selector(bare_id), bare_id)
        self.assertEqual(
            canonical_workflow_selector(f"workflow-{bare_id}"),
            bare_id,
        )
        self.assertIsNone(canonical_workflow_selector("Shared Lab"))

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
                    "one_reported",
                    "done",
                    transition_description="Finish.",
                ),
                EdgeSpec(
                    "two_done",
                    "two",
                    "two_reported",
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
        self.assertEqual(
            result["transition"],
            "deterministic_verify_reported",
        )
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
        self.assertEqual(
            result["transition"],
            "deterministic_verify_reported",
        )
        self.assertEqual(result["verification_status"], "blocked")

    def test_malformed_verifier_output_still_reports_blocked(self) -> None:
        result = self.run_report("echo not-json\n")
        self.assertEqual(result["verification_status"], "blocked")

    def test_invalid_workflow_input_still_reports_blocked(self) -> None:
        result = self.run_report("exit 99\n", workflow_input="not-json")
        self.assertEqual(result["verification_status"], "blocked")


if __name__ == "__main__":
    unittest.main()
