from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from workflowkit.delivery import build_delivery_workflow
from workflowkit.profile import ProjectProfile


REPO_ROOT = Path(__file__).resolve().parents[1]


def runtime_v2_profile(*, ci: bool, source_ci: bool = False) -> ProjectProfile:
    contents = (REPO_ROOT / "contracts" / "project-profile.example.toml").read_text()
    contents = contents.replace(
        "schema_version = 3\n",
        'schema_version = 4\nkit_managed_commands = ["dispatch"]\n',
    ).replace('release_topology = "none"\n', "")
    contents += (
        "\n[command_versions]\n"
        'dispatch = "1.0.0"\n'
        "\n[release]\n"
        'topology_kind = "appsome-release-publication"\n'
        'adoption_mode = "managed-in-place"\n'
        'spec_path = ".kent/release/spec.toml"\n'
        'builder_path = ".kent/release/build.sh"\n'
        'snapshot_path = ".kent/release/snapshot.toml"\n'
    )
    contents = contents.replace(
        'kit_managed_commands = ["dispatch"]',
        (
            'kit_managed_commands = ["runtime_contracts", "verify", "evidence", '
            '"janitor", "wait_pr", "github_observation"'
            + (', "wait_ci"' if ci else "")
            + "]"
        ),
    ).replace(
        'dispatch = ".kent/scripts/workflow-verification-dispatch"\n',
        (
            'dispatch = ".kent/scripts/workflow-verification-dispatch"\n'
            'runtime_contracts = ".kent/scripts/workflow_runtime_contracts.py"\n'
        ),
    ).replace(
        '[command_versions]\n'
        'dispatch = "1.0.0"\n',
        (
            "[command_versions]\n"
            'runtime_contracts = "2.0.0"\n'
            'verify = "2.0.0"\n'
            'evidence = "2.0.0"\n'
            'janitor = "2.0.0"\n'
            'wait_pr = "3.0.0"\n'
            'github_observation = "1.0.0"\n'
            + ('wait_ci = "3.0.0"\n' if ci else "")
        ),
    ).replace(
        "ci_monitoring = true",
        f"ci_monitoring = {'true' if ci else 'false'}",
    )
    if source_ci:
        contents = (
            contents.replace(
                '"github_observation"',
                '"github_observation", "prepare_ci"',
            )
            .replace(
                'github_observation = "1.0.0"\n',
                'github_observation = "1.0.0"\nprepare_ci = "2.0.0"\n',
            )
            .replace(
                'wait_ci = ".kent/scripts/workflow-wait-github-ci"\n',
                'wait_ci = ".kent/scripts/workflow-wait-github-ci"\n'
                'prepare_ci = ".kent/scripts/workflow-prepare-github-ci"\n',
            )
        )
    return ProjectProfile.from_toml(REPO_ROOT, contents, check_files=False)


def edge_map(profile: ProjectProfile):
    return {
        edge.key: edge
        for edge in build_delivery_workflow(profile, 1).edges
    }


def create_repository(testcase: unittest.TestCase) -> Path:
    temporary = tempfile.TemporaryDirectory()
    testcase.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    for args in (
        ("init", "-q"),
        ("config", "user.name", "Kent Test"),
        ("config", "user.email", "kent@example.invalid"),
    ):
        subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    (root / ".kent").mkdir()
    (root / ".gitignore").write_text("/.kent/runtime/\n")
    (root / "tracked.txt").write_text("ready\n")
    subprocess.run(
        ["git", "-C", str(root), "add", "."],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "Initial"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return root.resolve()


def materialize_templates(root: Path, *names: str) -> Path:
    scripts = root / ".kent" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for name in names:
        source = REPO_ROOT / "templates" / "project" / name
        target = scripts / name
        shutil.copyfile(source, target)
        target.chmod(0o755)
    shutil.copyfile(
        REPO_ROOT / "workflowkit" / "runtime.py",
        scripts / "workflow_runtime_contracts.py",
    )
    return scripts


def run_json_command(
    command: list[str],
    *,
    root: Path,
    payload: dict[str, object],
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    result = subprocess.run(
        command,
        cwd=root,
        input=json.dumps(payload),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **(environment or {})},
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {result.stderr}"
        )
    output = json.loads(result.stdout)
    if not isinstance(output, dict):
        raise AssertionError("command did not emit a JSON object")
    return output


def assert_edge_projection(
    testcase: unittest.TestCase,
    *,
    spec,
    source: str,
    output: dict[str, object],
) -> object:
    edge = next(
        (
            candidate
            for candidate in spec.edges
            if candidate.source == source
            and candidate.transition == output.get("transition")
        ),
        None,
    )
    testcase.assertIsNotNone(edge, (source, output.get("transition")))
    required = {parameter.key for parameter in edge.parameters}
    testcase.assertTrue(required <= output.keys(), (edge.key, required, output))
    return edge


def direct_parameter_references(prompt: str) -> set[str]:
    return set(
        re.findall(
            r"\{\{\s*\.Params\.([A-Za-z_][A-Za-z0-9_]*)",
            prompt,
        )
    )


class DeliveryContinuityTest(unittest.TestCase):
    def test_shared_lifecycle_edges_carry_delivery_context(self) -> None:
        profiles = {
            "runtime-v2-with-ci": runtime_v2_profile(ci=True, source_ci=True),
            "runtime-v2-without-ci": runtime_v2_profile(ci=False),
            "kit-lite-cursor": ProjectProfile.from_toml(
                REPO_ROOT,
                (REPO_ROOT / ".kent" / "workflow-profile.toml").read_text(),
                check_files=False,
            ),
        }
        shared_edges = {
            "plan_review",
            "plan_review_accept",
            "plan_review_revalidate",
            "plan_revalidation_review",
            "plan_contract_continue_revalidate",
            "plan_contract_verify_revalidate",
            "plan_contract_branch_identity",
            "branch_identity_implement",
            "branch_identity_resolution",
            "branch_identity_retry",
            "plan_contract_implement",
            "plan_contract_continue_implement",
            "plan_contract_verify",
            "plan_contract_checked_continue",
            "plan_contract_checked_verify",
            "implement_continue",
            "implement_verify",
            "fix_verify",
            "dispatch_deterministic_verify",
            "dispatch_invalid_workspace",
            "dispatch_standards_review",
            "dispatch_spec_review",
            "gate_fix",
            "gate_reverify_after_user_action",
            "gate_smoke_required",
            "gate_delivery_ready",
            "smoke_fix",
            "compliance_prepare_pr",
            "compliance_fix",
            "compliance_evidence_repair",
            "compliance_needs_user_action",
            "evidence_repair_compliance",
            "evidence_repair_fix",
            "prepare_pr_fix",
            "ci_monitor_fix",
            "waiting_pr_fix",
            "ci_monitor_watch",
            "fix_needs_user_action",
        }
        for name, profile in profiles.items():
            with self.subTest(profile=name):
                edges = edge_map(profile)
                for key in sorted(shared_edges):
                    if key not in edges:
                        continue
                    self.assertIn(
                        "delivery_context",
                        {parameter.key for parameter in edges[key].parameters},
                        key,
                    )

    def test_plan_initializes_pre_pr_and_gate_uses_latest_scoped_dispatch(self) -> None:
        profile = runtime_v2_profile(ci=True, source_ci=True)
        spec = build_delivery_workflow(profile, 1)
        edges = {edge.key: edge for edge in spec.edges}

        self.assertIn(
            "delivery_context",
            {parameter.key for parameter in edges["plan_review"].parameters},
        )
        self.assertIn("pre_pr", edges["start_plan"].prompt)
        self.assertIn(
            "{{.Params.verification_dispatch_fanout_verify.delivery_context}}",
            edges["verification_join_gate"].prompt or "",
        )
        self.assertNotIn(
            "delivery_context",
            {
                parameter.key
                for parameter in edges["deterministic_report_join"].parameters
            },
        )

    def test_implement_prompt_displays_the_packet_once(self) -> None:
        profiles = (
            runtime_v2_profile(ci=False),
            ProjectProfile.from_toml(
                REPO_ROOT,
                (REPO_ROOT / ".kent" / "workflow-profile.toml").read_text(),
                check_files=False,
            ),
        )
        for profile in profiles:
            with self.subTest(schema=profile.schema_version):
                prompt = edge_map(profile)["plan_contract_implement"].prompt or ""
                self.assertEqual(
                    prompt.count("{{.Params.delivery_context}}"),
                    1,
                )

    def test_gate_reads_latest_scoped_dispatch_without_fanout(self) -> None:
        profile = runtime_v2_profile(ci=True, source_ci=True)
        profile = replace(
            profile,
            capabilities={
                **profile.capabilities,
                "standards_review": False,
                "spec_review": False,
            }
        )
        edges = edge_map(profile)
        gate = edges["deterministic_report_gate"]

        self.assertIn(
            "{{.Params.verification_dispatch_fanout_verify.delivery_context}}",
            gate.prompt or "",
        )
        self.assertEqual(
            tuple(parameter.key for parameter in gate.parameters),
            ("verification_status", "verification_report"),
        )

    def test_prepare_pr_passes_real_post_pr_context_to_ci_producer(self) -> None:
        profile = runtime_v2_profile(ci=True, source_ci=True)
        edges = edge_map(profile)
        keys = tuple(
            parameter.key
            for parameter in edges["prepare_pr_ci_prepare"].parameters
        )

        self.assertEqual(
            keys,
            ("workspace_path", "task_short_id", "delivery_context"),
        )
        self.assertIn(
            "post_pr",
            edges["prepare_pr_ci_prepare"].parameters[-1].description,
        )
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in edges["ci_monitor_watch"].parameters
            ),
            ("workspace_path", "task_short_id", "delivery_context"),
        )

    def test_agent_prompt_parameters_exist_on_the_incoming_edge(self) -> None:
        profile = runtime_v2_profile(ci=True, source_ci=True)
        edges = edge_map(profile)
        edge_keys = (
            "ci_prepare_failed",
            "ci_watch_diagnose",
            "ci_watch_waiting_pr",
            "ci_watch_state_changed",
            "waiting_pr_needs_user_action",
            "prepare_pr_fix",
            "waiting_pr_fix",
        )
        flat_observation_edges = {
            "ci_prepare_failed",
            "ci_watch_diagnose",
            "ci_watch_waiting_pr",
            "ci_watch_state_changed",
            "waiting_pr_needs_user_action",
        }
        scoped_prior_key = "verification_dispatch_fanout_verify"
        dispatch = edges["dispatch_deterministic_verify"]
        self.assertIn(
            "delivery_context",
            {parameter.key for parameter in dispatch.parameters},
        )
        for key in edge_keys:
            edge = edges[key]
            references = direct_parameter_references(edge.prompt or "")
            references.discard("TaskShortId")
            self.assertNotIn("verification_dispatch_fanout_verify", references, key)
            self.assertLessEqual(
                references,
                {parameter.key for parameter in edge.parameters},
                key,
            )
            if key in flat_observation_edges:
                self.assertNotIn(
                    "{{.Params.delivery_context}}",
                    edge.prompt or "",
                    key,
                )
        gate = edges["verification_join_gate"]
        self.assertIn(
            f"{{{{.Params.{scoped_prior_key}.delivery_context}}}}",
            gate.prompt or "",
        )

    def test_observer_recovery_loops_keep_flat_delivery_contracts(self) -> None:
        source_profile = runtime_v2_profile(ci=True, source_ci=True)
        source_edges = edge_map(source_profile)
        ci_prompt = source_edges["ci_watch_diagnose"].prompt or ""
        ci_prompt_compact = " ".join(ci_prompt.split())
        waiting_prompt = (
            source_edges["waiting_pr_needs_user_action"].prompt or ""
        )
        waiting_prompt_compact = " ".join(waiting_prompt.split())

        self.assertIn("For `ci_monitor_needs_changes`, pack", ci_prompt)
        self.assertIn(
            "Keep `ci_monitor_needs_user_action` on its existing flat "
            "self-recovery contract.",
            ci_prompt_compact,
        )
        self.assertIn(
            "For source-contract `waiting_pr_ci_required`",
            waiting_prompt,
        )
        self.assertIn(
            "Keep `waiting_pr_needs_user_action` on its existing flat "
            "self-recovery contract.",
            waiting_prompt_compact,
        )
        for key in (
            "ci_monitor_needs_user_action",
            "waiting_pr_needs_user_action",
        ):
            self.assertNotIn(
                "delivery_context",
                {parameter.key for parameter in source_edges[key].parameters},
                key,
            )

        non_source_profile = ProjectProfile.from_toml(
            REPO_ROOT,
            (REPO_ROOT / ".kent" / "workflow-profile.toml").read_text(),
            check_files=False,
        )
        non_source_profile = replace(
            non_source_profile,
            capabilities={
                **non_source_profile.capabilities,
                "ci_monitoring": True,
            },
            commands={
                **non_source_profile.commands,
                "wait_ci": ".kent/scripts/workflow-wait-github-ci",
            },
        )
        non_source_prompt = (
            edge_map(non_source_profile)["waiting_pr_needs_user_action"].prompt
            or ""
        )
        self.assertIn(
            "non-source-contract `waiting_pr_ci_required`",
            non_source_prompt,
        )
        self.assertNotIn(
            "For source-contract `waiting_pr_ci_required`",
            non_source_prompt,
        )

    def test_legacy_standard_profile_has_no_delivery_context_contract(self) -> None:
        profile = ProjectProfile.from_toml(
            REPO_ROOT,
            (REPO_ROOT / "contracts" / "project-profile.example.toml").read_text(),
            check_files=False,
        )
        self.assertEqual(profile.schema_version, 3)
        self.assertEqual(profile.delivery_profile, "standard")
        spec = build_delivery_workflow(profile, 1)

        self.assertFalse(
            any(
                parameter.key == "delivery_context"
                for edge in spec.edges
                for parameter in edge.parameters
            )
        )
        self.assertFalse(
            any(
                "delivery_context" in (edge.prompt or "")
                for edge in spec.edges
            )
        )

    def test_real_wrapper_stdout_projects_to_selected_edge_specs(self) -> None:
        profile = runtime_v2_profile(ci=True, source_ci=True)
        spec = build_delivery_workflow(profile, 1)
        root = create_repository(self)
        scripts = materialize_templates(
            root,
            "workflow-plan-contract",
            "workflow-plan-contract-accept",
            "workflow-plan-contract-continue",
            "workflow-plan-contract-verify",
            "workflow-plan-contract-fix-continue",
            "workflow-verification-dispatch",
            "workflow-verify-report",
        )
        pre_pr = ' { "schema" : "workflow-delivery-context-v1", "phase" : "pre_pr" } '
        post_pr = json.dumps(
            {
                "schema": "workflow-delivery-context-v1",
                "phase": "post_pr",
                "pr_url": "https://github.com/acme/widget/pull/42",
                "branch_name": "task/continuity",
                "merge_strategy": "squash",
                "pr_feedback_cursor": "uninitialized",
                "ci_contract": '{ "schema" : "github-ci-contract-v1" }',
                "ci_report": '{ "schema" : "github-ci-report-v1" }',
            },
            ensure_ascii=False,
        )
        plan = root / ".todo" / "task" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n\n- [ ] Implement continuity\n")

        accepted = run_json_command(
            [str(scripts / "workflow-plan-contract-accept")],
            root=root,
            payload={
                "workspace_path": str(root),
                "plan_path": ".todo/task/plan.md",
                "work_kind": "feature",
                "plan_route": "start",
                "plan_route_context": "not-applicable",
                "review_context": "accepted plan",
                "task_short_id": "TASK-CONTINUITY",
                "delivery_context": pre_pr,
            },
        )
        accept_edge = assert_edge_projection(
            self,
            spec=spec,
            source="plan_contract",
            output=accepted,
        )
        self.assertEqual(accept_edge.key, "plan_contract_implement")
        self.assertEqual(accepted["delivery_context"], pre_pr)
        snapshot = json.loads(
            (
                root
                / ".kent"
                / "runtime"
                / "TASK-CONTINUITY"
                / "plan-contract.json"
            ).read_text()
        )
        self.assertNotIn("delivery_context", snapshot)

        verified = run_json_command(
            [str(scripts / "workflow-plan-contract-verify")],
            root=root,
            payload={
                "workspace_path": str(root),
                "review_context": "all writer steps complete",
                "task_short_id": "TASK-CONTINUITY",
                "delivery_context": post_pr,
            },
        )
        verify_edge = assert_edge_projection(
            self,
            spec=spec,
            source="plan_contract_verify",
            output=verified,
        )
        self.assertEqual(verify_edge.key, "plan_contract_checked_verify")
        self.assertEqual(verified["delivery_context"], post_pr)

        dispatched = run_json_command(
            [str(scripts / "workflow-verification-dispatch")],
            root=root,
            payload={
                "workspace_path": str(root),
                "review_context": "verification evidence",
                "delivery_context": post_pr,
            },
        )
        dispatch_edge = assert_edge_projection(
            self,
            spec=spec,
            source="verification_dispatch",
            output=dispatched,
        )
        self.assertEqual(dispatch_edge.key, "dispatch_deterministic_verify")
        self.assertEqual(dispatched["delivery_context"], post_pr)

        verifier = scripts / "workflow-compile-verify"
        verifier.write_text(
            "#!/bin/sh\nprintf '%s\\n' '{\"transition\":\"failed\"}'\n"
        )
        verifier.chmod(0o755)
        with (root / ".gitignore").open("a") as ignored:
            ignored.write("/build/kent-workflow/\n")
        failed_report = run_json_command(
            [sys.executable, str(scripts / "workflow-verify-report")],
            root=root,
            payload={"workspace_path": str(root)},
        )
        report_edge = assert_edge_projection(
            self,
            spec=spec,
            source="deterministic_verify",
            output=failed_report,
        )
        self.assertEqual(report_edge.key, "deterministic_report_join")
        self.assertEqual(
            {parameter.key for parameter in report_edge.parameters},
            {"verification_status", "verification_report"},
        )
        self.assertEqual(failed_report["verification_status"], "needs_changes")
        self.assertNotIn("delivery_context", failed_report)


if __name__ == "__main__":
    unittest.main()
