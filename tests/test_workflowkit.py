from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

from workflowkit.delivery import (
    build_canary_workflow,
    build_delivery_workflow,
    build_smoke_lab_workflow,
    context_instruction,
    cleanup_prompt,
    implement_prompt,
    janitor_recovery_prompt,
    plan_prompt,
    published_cleanup_prompt,
)
from workflowkit.kent import (
    KentClient,
    KentCommandError,
    canonical_workflow_selector,
    execution_target_from_policy,
)
from workflowkit.graph import graph_matches_spec, plan_workflow_graph
from workflowkit.model import (
    EdgeSpec,
    NodeSpec,
    ParameterSpec,
    SpecError,
    WorkflowSpec,
    validate_execution_target,
)
from workflowkit.profile import ProjectProfile, ReleaseProfile


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PROFILE = REPO_ROOT / "contracts" / "project-profile.example.toml"
VERIFY_REPORT = REPO_ROOT / "templates" / "project" / "workflow-verify-report"
VERIFY_DISPATCH = (
    REPO_ROOT / "templates" / "project" / "workflow-verification-dispatch"
)
PLAN_CONTRACT = REPO_ROOT / "templates" / "project" / "workflow-plan-contract"
PLAN_CONTRACT_ACCEPT = (
    REPO_ROOT / "templates" / "project" / "workflow-plan-contract-accept"
)
PLAN_CONTRACT_CONTINUE = (
    REPO_ROOT / "templates" / "project" / "workflow-plan-contract-continue"
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


def load_generator():
    loader = SourceFileLoader(
        "workflow_generator_test", str(REPO_ROOT / "scripts/generate-workflow")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def create_work_kind_procedures(root: Path) -> None:
    for configured_path in WORK_KIND_PROCEDURES + CONTEXT_MANIFESTS:
        path = root / configured_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Test context\n")


def schema4_profile_contents(
    *,
    topology_kind: str = "appsome-release-publication",
    adoption_mode: str = "managed-in-place",
    builder_path: str = ".kent/release/build.sh",
) -> str:
    contents = EXAMPLE_PROFILE.read_text()
    contents = contents.replace(
        "schema_version = 3\n",
        (
            "schema_version = 4\n"
            'kit_managed_commands = ["dispatch"]\n\n'
        ),
    )
    contents = contents.replace('release_topology = "none"\n', "")
    contents += (
        "\n[command_versions]\n"
        'dispatch = "1.0.0"\n'
        "\n[release]\n"
        f'topology_kind = "{topology_kind}"\n'
        f'adoption_mode = "{adoption_mode}"\n'
        'spec_path = ".kent/release/spec.toml"\n'
        f'builder_path = "{builder_path}"\n'
        'snapshot_path = ".kent/release/snapshot.toml"\n'
    )
    return contents


def schema4_with_managed_adapter(contents: str) -> str:
    return (
        contents.replace(
            "required_adapters = []",
            'required_adapters = ["mobile_resource_lock"]',
        ).replace(
            "kit_managed_adapters = []",
            'kit_managed_adapters = ["mobile_resource_lock"]',
        )
        + (
            "\n[adapters]\n"
            'mobile_resource_lock = '
            '".kent/adapters/mobile/emulator-resource-lock.sh"\n'
        )
    )


class WorkflowKitTest(unittest.TestCase):
    def test_script_source_ingress_public_api_delegates_exactly(self) -> None:
        import workflowkit.delivery as delivery

        native_context = {"task_id": "native-task", "node_id": "source-script"}
        expected = object()
        with mock.patch(
            "workflowkit.operations.admit_script_source_ingress",
            return_value=expected,
        ) as admit:
            result = delivery.admit_script_source_ingress(
                "/workspace",
                native_context,
                kent_path="/bin/kent",
            )
        self.assertIs(result, expected)
        admit.assert_called_once_with(
            "/workspace",
            native_context,
            kent_path="/bin/kent",
        )

    def test_test_classes_do_not_shadow_methods(self) -> None:
        duplicates = []
        for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
            for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                if not isinstance(node, ast.ClassDef):
                    continue
                seen = set()
                for method in node.body:
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if method.name in seen:
                            duplicates.append(f"{path.name}:{node.name}.{method.name}")
                        seen.add(method.name)
        self.assertEqual(duplicates, [])

    def test_review_stage_obligations_survive_optional_delivery_nodes(self) -> None:
        baseline = self.load_profile()
        for compliance in (False, True):
            for pull_requests in (False, True):
                with self.subTest(compliance=compliance, pull_requests=pull_requests):
                    profile = replace(
                        baseline,
                        capabilities={
                            **baseline.capabilities,
                            "compliance_review": compliance,
                            "pull_requests": pull_requests,
                            "ci_monitoring": pull_requests,
                        },
                    )
                    spec = build_delivery_workflow(profile, 1)
                    nodes = {node.key for node in spec.nodes}
                    self.assertEqual("compliance" in nodes, compliance and pull_requests)
                    self.assertEqual("prepare_pr" in nodes, pull_requests)
                    for target in ("spec_review", "verification_gate"):
                        prompt = " ".join(next(
                            edge.prompt for edge in spec.edges if edge.target == target
                        ).split())
                        self.assertIn("current authorized stage", prompt)
                        self.assertIn("owner and due milestone", prompt)
                        self.assertIn("review-ready PR", prompt)
                        self.assertIn("full business", prompt)
                    gate = " ".join(next(
                        edge.prompt for edge in spec.edges
                        if edge.target == "verification_gate"
                    ).split())
                    self.assertIn("every enabled status is `passed`", gate)
                    self.assertIn("fresh review", gate)
                    self.assertIn("previous `blocked`", gate)

    def test_completion_fields_match_actual_graph_across_profile_capabilities(self) -> None:
        baseline = self.load_profile()
        runtime_v2 = self.load_schema4_profile(
            lambda _: self.source_ci_profile_contents()
        )
        current = ProjectProfile.from_toml(
            REPO_ROOT, (REPO_ROOT / ".kent/workflow-profile.toml").read_text(),
            check_files=False,
        )
        profiles = (
            baseline,
            replace(baseline, policies={**baseline.policies, "writer_sessions": "continuous"}),
            runtime_v2,
            replace(runtime_v2, capabilities={**runtime_v2.capabilities, "ci_monitoring": False}),
            current,
            replace(current, policies={**current.policies, "coder_selection": "complexity"}),
        )
        header = "Completion fields from the selected graph contract "
        for index, profile in enumerate(profiles):
            spec = build_delivery_workflow(profile, 1)
            agents = {node.key for node in spec.nodes if node.kind == "agent"}
            for edge in spec.edges:
                if edge.target not in agents:
                    continue
                with self.subTest(profile=index, edge=edge.key):
                    prompt = edge.prompt or ""
                    self.assertEqual(prompt.count(header), 1)
                    footer = prompt.split(header, 1)[1].splitlines()[1:]
                    expected = []
                    for outgoing in spec.edges:
                        if outgoing.source == edge.target:
                            fields = ", ".join(
                                f"`{parameter.key}`" for parameter in outgoing.parameters
                            ) or "(none)"
                            expected.append(f"- `{outgoing.transition}`: {fields}.")
                    self.assertEqual(footer, expected)

    def test_source_admission_gate_binds_exact_typed_inputs(self) -> None:
        from workflowkit.delivery import (
            SOURCE_ADMISSION_BEGIN, SOURCE_ADMISSION_END, source_admission_gate,
        )
        admission = {
            "schema": "delivery-source-admission-v1",
            "receipt_ref": {"path": "/private/receipt.json", "sha256": "a" * 64},
            "installation_binding_sha256": "b" * 64,
            "primary_workspace_id": "workspace-native",
            "primary_root": "/project", "kit_root": "/kit",
            "source_ref": "refs/remotes/origin/main", "kit_ref": "refs/remotes/origin/main",
            "ci_helper": "/home/.kent/bin/kent-prepare-github-ci",
            "helper_bindings": [{"link": "/home/.kent/bin/kent-project-memory",
                                 "target": "/kit/.kent/scripts/workflow-project-memory"}],
        }
        arguments = dict(project_id="project-native", workflow_id="workflow-native",
                         project_root="/project")
        gate = source_admission_gate(admission, **arguments)
        self.assertEqual(gate, source_admission_gate(dict(reversed(list(admission.items()))),
                                                   **arguments))
        lines = gate.splitlines()
        self.assertEqual(lines[0], SOURCE_ADMISSION_BEGIN)
        self.assertEqual(lines[-1], SOURCE_ADMISSION_END)
        canonical = json.dumps(admission, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":")).encode()
        self.assertEqual(json.loads(lines[1]), {
            "receipt_sha256": "a" * 64,
            "source_admission_sha256": hashlib.sha256(canonical).hexdigest()})
        altered = {**admission, "installation_binding_sha256": "c" * 64}
        self.assertNotEqual(gate, source_admission_gate(altered, **arguments))
        self.assertIn("--receipt-sha256 " + "a" * 64, gate)

    def test_source_admission_v2_gate_carries_exact_full_ingress_frame(self) -> None:
        from workflowkit.delivery import (
            SOURCE_ADMISSION_BEGIN, SOURCE_ADMISSION_END, source_admission_gate,
        )

        admission = {
            "schema": "delivery-source-admission-v2",
            "receipt_ref": {"path": "/private/receipt.json", "sha256": "a" * 64},
            "primary_workspace_id": "workspace-native",
            "primary_root": "/project",
            "kit_root": "/kit",
            "source_ref": "refs/remotes/origin/main",
            "kit_ref": "refs/remotes/origin/main",
            "ci_helper": "/home/.kent/bin/kent-prepare-github-ci",
            "helper_bindings": [{
                "link": "/home/.kent/bin/kent-project-memory",
                "target": "/kit/.kent/scripts/workflow-project-memory",
            }],
            "binding": {"kind": "plan-publication"},
            "ingress": {"kind": "script-edge", "edge_id": "edge-1"},
        }
        arguments = {
            "project_id": "project-native",
            "workflow_id": "workflow-native",
            "project_root": "/project",
        }
        gate = source_admission_gate(admission, **arguments)
        lines = gate.splitlines()
        self.assertEqual(lines[0], SOURCE_ADMISSION_BEGIN)
        self.assertEqual(lines[-1], SOURCE_ADMISSION_END)
        expected_frame = {
            "project_id": arguments["project_id"],
            "workflow_id": arguments["workflow_id"],
            "source_admission": admission,
        }
        self.assertEqual(json.loads(lines[1]), expected_frame)
        self.assertEqual(
            lines[1],
            json.dumps(
                expected_frame, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ),
        )
        self.assertNotIn("source_admission_sha256", lines[1])
        self.assertIn("Do not initialize an R2 Task", gate)
        self.assertIn("--receipt-sha256 " + "a" * 64, gate)

        r2_admission = {
            **admission,
            "binding": {
                "kind": "r2-installed",
                "installation_binding_sha256": "b" * 64,
            },
            "ingress": {"kind": "agent-edge", "edge_id": "edge-2"},
        }
        r2_gate = source_admission_gate(r2_admission, **arguments)
        self.assertEqual(
            json.loads(r2_gate.splitlines()[1])["source_admission"],
            r2_admission,
        )
        self.assertIn("initialize-task", r2_gate)
        self.assertNotEqual(gate, r2_gate)


    def cleanup_profile(self, *, managed: bool, published: bool) -> ProjectProfile:
        profile = self.load_profile()
        return replace(
            profile,
            capabilities={**profile.capabilities, "managed_worktrees": managed},
            release_topology=(
                "manual-package-publish-after-main" if published else "none"
            ),
            procedures={
                **profile.procedures,
                "publish": ".kent/commands/feature-start.md" if published else "",
            },
        )


    def test_opt_in_cleanup_variants_have_one_project_owned_event(self) -> None:
        for managed in (False, True):
            for published in (False, True):
                with self.subTest(managed=managed, published=published):
                    baseline_profile = self.cleanup_profile(managed=managed, published=published)
                    profile = replace(baseline_profile, commands={
                        **baseline_profile.commands,
                        "prepare_cleanup": ".kent/scripts/project-terminal-preparation",
                    })
                    baseline = build_delivery_workflow(baseline_profile, 1)
                    spec = build_delivery_workflow(profile, 1)
                    self.assertEqual(spec.nodes, baseline.nodes)
                    for previous, current in zip(baseline.edges, spec.edges, strict=True):
                        if current.target != "cleanup":
                            self.assertEqual(current, previous)
                            continue
                        self.assertEqual(replace(current, prompt=previous.prompt), previous)
                        self.assertIn("project-terminal-preparation", current.prompt)
                        self.assertNotIn("workflow-evidence-ledger append", current.prompt)
                        for requirement in (
                            "unmodified `KENT_SESSION_ID`, `KENT_RUN_ID` and `KENT_STEP_ID`",
                            "merely to report a blocker", "retained Task records",
                            "Never generate, export, substitute or replace identities",
                            "conflicting or fabricated", "frozen-request/report recovery",
                            "before leaving", "relative project adapters",
                        ):
                            self.assertIn(requirement, current.prompt)
                    for mode in ({}, {"merged": True}, {"no_pr": True}, {"closed": True}):
                        prompt = cleanup_prompt(profile, **mode)
                        self.assertNotIn("workflow-evidence-ledger append", prompt)
                        self.assertIn("project-terminal-preparation", prompt)
                    self.assertIn(
                        "project-terminal-preparation", published_cleanup_prompt(profile),
                    )
                    self.assertEqual(
                        context_instruction(profile, "implement", "implement", "implementation"),
                        context_instruction(baseline_profile, "implement", "implement", "implementation"),
                    )

    def test_cleanup_without_terminal_capability_preserves_legacy_completion(self) -> None:
        legacy = self.cleanup_profile(managed=False, published=False)
        self.assertEqual(legacy.schema_version, 3)
        self.assertFalse(legacy.runtime_contracts_v2())
        self.assertEqual(legacy.command("runtime_contracts"), "")
        self.assertFalse((legacy.project_root / ".kent/scripts").exists())
        for helper in (None, ""):
            with self.subTest(prepare_cleanup=helper):
                profile = legacy if helper is None else replace(
                    legacy,
                    commands={**legacy.commands, "prepare_cleanup": helper},
                )
                prompt = context_instruction(
                    profile, "delivery", "cleanup", "delivery",
                )
                # No terminal support was installed or adopted. Preserve the
                # ordinary successful Cleanup append/completion contract.
                expected = context_instruction(
                    profile, "delivery", "implement", "delivery",
                ).replace("`implement`", "`cleanup`")
                self.assertEqual(prompt, expected)
                self.assertIn("append --task {{.TaskShortId}} --workspace", prompt)
                for unsupported in (
                    " seal --task", "terminal marker", "validate_terminal_chain",
                    "validate_terminal_seal_request", "validate_cleanup_report",
                    "configured runtime-contract support module",
                ):
                    self.assertNotIn(unsupported, prompt)

    def test_cleanup_without_preparation_helper_guides_sealed_recovery(self) -> None:
        missing = self.cleanup_profile(managed=False, published=False)
        missing = replace(
            missing,
            commands={
                **missing.commands,
                "runtime_contracts": ".kent/scripts/workflow_runtime_contracts.py",
            },
        )
        empty = replace(
            missing,
            commands={**missing.commands, "prepare_cleanup": ""},
        )
        self.assertEqual(
            context_instruction(missing, "delivery", "cleanup", "delivery"),
            context_instruction(empty, "delivery", "cleanup", "delivery"),
        )
        for name, profile in (("missing", missing), ("empty", empty)):
            with self.subTest(opt_in=name):
                prompt = context_instruction(
                    profile,
                    "delivery",
                    "cleanup",
                    "delivery",
                )
                for requirement in (
                    "original successful request/report/marker",
                    "without another append or seal",
                    "existing read-only validation path",
                    "validate_terminal_chain",
                    "validate_terminal_seal_request",
                    "validate_cleanup_report",
                    "actual Kent Session/Run/Step records",
                    "KENT_SESSION_ID",
                    "exact returned terminal marker as the final",
                    "Missing, malformed, conflicting, unreferenced or unverifiable history",
                    "genuinely new successful Cleanup with a validated unsealed ledger",
                    "no earlier Cleanup final event or partial seal attempt",
                    "append --task {{.TaskShortId}} --workspace <workspace>",
                    "seal --task {{.TaskShortId}} --workspace <workspace>",
                ):
                    with self.subTest(requirement=requirement):
                        self.assertIn(requirement, prompt)
                self.assertIn(
                    "workflow-evidence-ledger",
                    prompt,
                )

        enabled = replace(
            missing,
            commands={
                **missing.commands,
                "prepare_cleanup": ".kent/scripts/project-terminal-preparation",
            },
        )
        enabled_prompt = context_instruction(
            enabled,
            "delivery",
            "cleanup",
            "delivery",
        )
        self.assertIn("project-terminal-preparation", enabled_prompt)
        self.assertNotIn(
            "This profile has no non-empty `prepare_cleanup` opt-in",
            enabled_prompt,
        )
        self.assertNotIn(
            "genuinely new successful Cleanup with a validated unsealed ledger",
            enabled_prompt,
        )

    def load_profile(
        self,
        transform=lambda value: value,
        *,
        branch_identity_script: bool = False,
    ) -> ProjectProfile:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        profile_directory = root / ".kent"
        profile_directory.mkdir()
        contents = transform(EXAMPLE_PROFILE.read_text())
        (profile_directory / "workflow-profile.toml").write_text(contents)
        if branch_identity_script:
            scripts = profile_directory / "scripts"
            scripts.mkdir()
            target = scripts / "workflow-branch-identity"
            shutil.copyfile(
                REPO_ROOT / "templates/project/workflow-branch-identity",
                target,
            )
            target.chmod(0o755)
        create_work_kind_procedures(root)
        return ProjectProfile.load(root)

    def create_plan_contract_fixture(
        self,
        *,
        coder_selection: str | None = "complexity",
    ) -> tuple[Path, Path, object]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        subprocess.run(
            ["git", "init", "-q", str(root)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        (root / ".gitignore").write_text("/.kent/runtime/\n")
        kent = root / ".kent"
        kent.mkdir()
        scripts = kent / "scripts"
        scripts.mkdir()
        for source in (
            PLAN_CONTRACT,
            PLAN_CONTRACT_ACCEPT,
            PLAN_CONTRACT_CONTINUE,
        ):
            target = scripts / source.name
            shutil.copyfile(source, target)
            target.chmod(0o755)
        session_response = kent / "task-sessions.json"
        fake_kent = kent / "test-kent"
        fake_kent.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "task" ] && [ "$2" = "sessions" ]; then\n'
            '  exec /bin/cat "$KENT_SESSION_RESPONSE"\n'
            "fi\n"
            "exit 2\n"
        )
        fake_kent.chmod(0o755)
        profile = (
            "[policies]\n"
            'writer_sessions = "continuous"\n'
        )
        if coder_selection is not None:
            profile += f'coder_selection = "{coder_selection}"\n'
        (kent / "workflow-profile.toml").write_text(profile)
        plan = root / "plan.md"
        plan.write_text(
            "# Accepted plan\n\n"
            "- [ ] Implement the reviewed behavior.\n\n"
            "Complexity decision:\n\n"
            "```json\n"
            "{\n"
            '  "schema": "coder-selection-v1",\n'
            '  "task_short_id": "KEN-18",\n'
            '  "complexity": "complex",\n'
            '  "rationale": "The change affects workflow contracts."\n'
            "}\n"
            "```\n"
        )
        loader = SourceFileLoader(
            "ken18_workflow_plan_contract_test",
            str(PLAN_CONTRACT),
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.assertIsNotNone(spec)
        if spec is None:
            raise AssertionError("could not load the plan contract template")
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return root, plan, module

    def run_plan_contract(
        self,
        root: Path,
        *,
        mode: str,
        payload: dict[str, object],
        route: str = "continue",
        session_rows: list[dict[str, str]] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if mode not in {"accept", "check"}:
            raise ValueError(f"unsupported test mode: {mode}")
        if route != "continue":
            raise ValueError(f"unsupported test route: {route}")
        wrapper = root / ".kent" / "scripts" / (
            "workflow-plan-contract-accept"
            if mode == "accept"
            else "workflow-plan-contract-continue"
        )
        snapshot_path = root / ".kent" / "runtime" / "KEN-18" / "plan-contract.json"
        sessions = session_rows
        if sessions is None:
            sessions = []
            if snapshot_path.is_file():
                snapshot = json.loads(snapshot_path.read_text())
                selected = snapshot.get("coder_selection")
                if isinstance(selected, dict):
                    role = {
                        "simple": "implementation-simple",
                        "complex": "implementation-complex",
                    }.get(selected.get("complexity"))
                    if role:
                        sessions = [
                            {
                                "session_id": "retained-implementation-session",
                                "agent_role": role,
                                "status": "idle",
                                "node_name": "Implement",
                            }
                        ]
        session_response = root / ".kent" / "task-sessions.json"
        session_response.write_text(
            json.dumps({"task_id": "task-uuid", "items": sessions})
        )
        execution_payload = dict(payload)
        execution_payload.setdefault("_kent", {"task_id": "task-uuid"})
        environment = os.environ.copy()
        environment.update(
            {
                "KENT_BIN": str(root / ".kent" / "test-kent"),
                "KENT_SESSION_RESPONSE": str(session_response),
            }
        )
        return subprocess.run(
            [str(wrapper)],
            cwd=root,
            input=json.dumps(execution_payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )


    def test_coder_selection_policy_is_opt_in_and_continuous_only(self) -> None:
        profile = self.load_profile()
        self.assertEqual(profile.coder_selection_policy(), "disabled")

        enabled = self.load_profile(
            lambda contents: contents.replace(
                'writer_sessions = "fresh_per_slice"',
                'writer_sessions = "continuous"\ncoder_selection = "complexity"',
            )
        )
        self.assertEqual(enabled.coder_selection_policy(), "complexity")

        with self.assertRaisesRegex(SpecError, "unsupported policies.coder_selection"):
            self.load_profile(
                lambda contents: contents.replace(
                    'writer_sessions = "fresh_per_slice"',
                    'writer_sessions = "continuous"\ncoder_selection = "automatic"',
                )
            )
        with self.assertRaisesRegex(
            SpecError,
            "requires policies.writer_sessions = 'continuous'",
        ):
            self.load_profile(
                lambda contents: contents.replace(
                    'writer_sessions = "fresh_per_slice"',
                    'writer_sessions = "fresh_per_slice"\ncoder_selection = "complexity"',
                )
            )

    def test_plan_contract_accepts_only_reviewed_task_bound_selection(self) -> None:
        root, plan, contract = self.create_plan_contract_fixture()
        normalized = contract.normalized_plan(plan)
        reviewed_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        acceptance = {
            "workspace_path": str(root),
            "task_short_id": "KEN-18",
            "plan_path": "plan.md",
            "work_kind": "feature",
            "plan_route": "start",
            "plan_route_context": "not-applicable",
            "review_context": "Independent review and human approval completed.",
            "accepted_plan_sha256": "not-applicable",
            "reviewed_normalized_sha256": reviewed_digest,
        }

        accepted = self.run_plan_contract(
            root,
            mode="accept",
            payload=acceptance,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(
            json.loads(accepted.stdout)["transition"],
            "plan_contract_start",
        )
        accepted_route = json.loads(accepted.stdout)
        self.assertEqual(accepted_route["accepted_plan_sha256"], reviewed_digest)
        self.assertEqual(accepted_route["task_short_id"], "KEN-18")
        self.assertEqual(accepted_route["target_assignee"], "implementation-complex")
        self.assertEqual(accepted_route["target_thinking"], "medium")
        snapshot_path = root / ".kent/runtime/KEN-18/plan-contract.json"
        snapshot = json.loads(snapshot_path.read_text())
        self.assertEqual(snapshot["task_short_id"], "KEN-18")
        self.assertEqual(snapshot["normalized_sha256"], reviewed_digest)
        self.assertEqual(snapshot["coder_selection_sha256"], reviewed_digest)
        self.assertEqual(
            snapshot["coder_selection"],
            {
                "schema": "coder-selection-v1",
                "task_short_id": "KEN-18",
                "complexity": "complex",
                "rationale": "The change affects workflow contracts.",
            },
        )

        # Checkbox-only implementation progress remains outside the digest.
        plan.write_text(plan.read_text().replace("- [ ]", "- [x]"))
        stable = self.run_plan_contract(
            root,
            mode="check",
            payload={
                "workspace_path": str(root),
                "task_short_id": "KEN-18",
                "review_context": "Continue the accepted writer session.",
                "accepted_plan_sha256": reviewed_digest,
            },
        )
        self.assertEqual(stable.returncode, 0, stable.stderr)
        self.assertEqual(
            json.loads(stable.stdout)["transition"],
            "plan_contract_continue_stable",
        )

        # A changed choice or stale reviewer digest goes back to Plan and
        # cannot overwrite the prior accepted snapshot.
        changed = plan.read_text().replace('"complexity": "complex"', '"complexity": "simple"')
        plan.write_text(changed)
        changed_digest = hashlib.sha256(
            contract.normalized_plan(plan).encode("utf-8")
        ).hexdigest()
        stale = self.run_plan_contract(
            root,
            mode="accept",
            payload={
                **acceptance,
                "accepted_plan_sha256": reviewed_digest,
                "reviewed_normalized_sha256": reviewed_digest,
            },
        )
        self.assertEqual(stale.returncode, 0, stale.stderr)
        stale_result = json.loads(stale.stdout)
        self.assertEqual(stale_result["transition"], "plan_contract_revalidate")
        self.assertIn("does not match current plan", stale_result["plan_change_report"])
        self.assertEqual(
            json.loads(snapshot_path.read_text())["normalized_sha256"],
            reviewed_digest,
        )

        current = self.run_plan_contract(
            root,
            mode="check",
            payload={
                "workspace_path": str(root),
                "task_short_id": "KEN-18",
                "review_context": "The accepted selection changed.",
                "accepted_plan_sha256": reviewed_digest,
            },
        )
        self.assertEqual(current.returncode, 0, current.stderr)
        self.assertEqual(
            json.loads(current.stdout)["transition"],
            "plan_contract_continue_changed",
        )
        self.assertEqual(
            hashlib.sha256(
                json.loads(snapshot_path.read_text())["normalized_plan"].encode()
            ).hexdigest(),
            reviewed_digest,
        )
        self.assertNotEqual(changed_digest, reviewed_digest)

    def test_plan_contract_rejects_preaccept_drift_and_tampered_selection_digest(
        self,
    ) -> None:
        root, plan, contract = self.create_plan_contract_fixture()
        original = plan.read_text()
        reviewed_digest = hashlib.sha256(
            contract.normalized_plan(plan).encode("utf-8")
        ).hexdigest()
        plan.write_text(
            original.replace(
                "The change affects workflow contracts.",
                "The plan changed after its independent review.",
            )
        )
        current_digest = hashlib.sha256(
            contract.normalized_plan(plan).encode("utf-8")
        ).hexdigest()
        stale_accept = self.run_plan_contract(
            root,
            mode="accept",
            payload={
                "workspace_path": str(root),
                "task_short_id": "KEN-18",
                "plan_path": "plan.md",
                "work_kind": "feature",
                "plan_route": "start",
                "plan_route_context": "not-applicable",
                "review_context": "The digest belongs to the earlier review.",
                "accepted_plan_sha256": "not-applicable",
                "reviewed_normalized_sha256": reviewed_digest,
            },
        )
        self.assertEqual(stale_accept.returncode, 0, stale_accept.stderr)
        stale_payload = json.loads(stale_accept.stdout)
        self.assertEqual(stale_payload["transition"], "plan_contract_revalidate")
        self.assertIn(
            "does not match current plan",
            stale_payload["plan_change_report"],
        )
        self.assertEqual(stale_payload["accepted_plan_sha256"], "not-applicable")
        self.assertNotEqual(current_digest, reviewed_digest)
        snapshot_path = root / ".kent/runtime/KEN-18/plan-contract.json"
        self.assertFalse(snapshot_path.exists())

        plan.write_text(original)
        accepted = self.run_plan_contract(
            root,
            mode="accept",
            payload={
                "workspace_path": str(root),
                "task_short_id": "KEN-18",
                "plan_path": "plan.md",
                "work_kind": "feature",
                "plan_route": "start",
                "plan_route_context": "not-applicable",
                "review_context": "Reviewed and approved.",
                "accepted_plan_sha256": "not-applicable",
                "reviewed_normalized_sha256": reviewed_digest,
            },
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        original_snapshot = json.loads(snapshot_path.read_text())

        rationale_tamper = dict(original_snapshot)
        rationale_tamper["coder_selection"] = {
            **original_snapshot["coder_selection"],
            "rationale": "Tampered after acceptance.",
        }
        snapshot_path.write_text(json.dumps(rationale_tamper))
        stale_selection = self.run_plan_contract(
            root,
            mode="check",
            payload={
                "workspace_path": str(root),
                "task_short_id": "KEN-18",
                "review_context": "Check the accepted choice.",
                "accepted_plan_sha256": reviewed_digest,
            },
        )
        self.assertEqual(stale_selection.returncode, 0, stale_selection.stderr)
        stale_selection_payload = json.loads(stale_selection.stdout)
        self.assertEqual(
            stale_selection_payload["transition"],
            "plan_contract_continue_changed",
        )
        self.assertIn(
            "Coder selection requires revalidation",
            stale_selection_payload["plan_change_report"],
        )
        self.assertNotIn("target_assignee", stale_selection_payload)

        digest_tamper = dict(original_snapshot)
        digest_tamper["coder_selection_sha256"] = "0" * 64
        snapshot_path.write_text(json.dumps(digest_tamper))
        rejected_digest = self.run_plan_contract(
            root,
            mode="check",
            payload={
                "workspace_path": str(root),
                "task_short_id": "KEN-18",
                "review_context": "Check the accepted choice.",
                "accepted_plan_sha256": reviewed_digest,
            },
        )
        self.assertNotEqual(rejected_digest.returncode, 0)
        self.assertIn(
            "not bound to the normalized plan digest",
            rejected_digest.stderr,
        )

    def test_plan_contract_maps_simple_selection_to_protected_target_values(self) -> None:
        root, plan, contract = self.create_plan_contract_fixture()
        plan.write_text(
            plan.read_text().replace(
                '"complexity": "complex"',
                '"complexity": "simple"',
            )
        )
        digest = hashlib.sha256(
            contract.normalized_plan(plan).encode("utf-8")
        ).hexdigest()
        accepted = self.run_plan_contract(
            root,
            mode="accept",
            payload={
                "workspace_path": str(root),
                "task_short_id": "KEN-18",
                "plan_path": "plan.md",
                "work_kind": "feature",
                "plan_route": "start",
                "plan_route_context": "not-applicable",
                "review_context": "Reviewed simple selection.",
                "accepted_plan_sha256": "not-applicable",
                "reviewed_normalized_sha256": digest,
            },
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        output = json.loads(accepted.stdout)
        self.assertEqual(output["transition"], "plan_contract_start")
        self.assertEqual(output["target_assignee"], "implementation-simple")
        self.assertEqual(output["target_thinking"], "xhigh")

    def test_plan_contract_rejects_existing_or_missing_retained_writer(self) -> None:
        root, plan, contract = self.create_plan_contract_fixture()
        digest = hashlib.sha256(
            contract.normalized_plan(plan).encode("utf-8")
        ).hexdigest()
        acceptance = {
            "workspace_path": str(root),
            "task_short_id": "KEN-18",
            "plan_path": "plan.md",
            "work_kind": "feature",
            "plan_route": "start",
            "plan_route_context": "not-applicable",
            "review_context": "Reviewed plan.",
            "accepted_plan_sha256": "not-applicable",
            "reviewed_normalized_sha256": digest,
        }
        existing_writer = self.run_plan_contract(
            root,
            mode="accept",
            payload=acceptance,
            session_rows=[
                {
                    "session_id": "unexpected-writer",
                    "agent_role": "implementation-complex",
                }
            ],
        )
        self.assertEqual(existing_writer.returncode, 0, existing_writer.stderr)
        self.assertEqual(
            json.loads(existing_writer.stdout)["transition"],
            "plan_contract_revalidate",
        )
        self.assertFalse(
            (root / ".kent/runtime/KEN-18/plan-contract.json").exists()
        )

        accepted = self.run_plan_contract(
            root,
            mode="accept",
            payload=acceptance,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        snapshot = root / ".kent/runtime/KEN-18/plan-contract.json"
        self.assertTrue(snapshot.is_file())
        continue_payload = {
            "workspace_path": str(root),
            "task_short_id": "KEN-18",
            "review_context": "Continue retained writer.",
            "accepted_plan_sha256": digest,
        }
        missing_writer = self.run_plan_contract(
            root,
            mode="check",
            payload=continue_payload,
            session_rows=[],
        )
        self.assertEqual(missing_writer.returncode, 0, missing_writer.stderr)
        missing_result = json.loads(missing_writer.stdout)
        self.assertEqual(
            missing_result["transition"],
            "plan_contract_continue_changed",
        )
        self.assertIn("exactly one retained", missing_result["plan_change_report"])

        wrong_writer = self.run_plan_contract(
            root,
            mode="check",
            payload=continue_payload,
            session_rows=[
                {
                    "session_id": "wrong-writer",
                    "agent_role": "implementation-simple",
                }
            ],
        )
        self.assertEqual(wrong_writer.returncode, 0, wrong_writer.stderr)
        wrong_result = json.loads(wrong_writer.stdout)
        self.assertEqual(
            wrong_result["transition"],
            "plan_contract_continue_changed",
        )
        self.assertIn("differs from the accepted", wrong_result["plan_change_report"])

    def test_plan_contract_preserves_disabled_selection_and_checks_task_identity(
        self,
    ) -> None:
        root, plan, _ = self.create_plan_contract_fixture(
            coder_selection=None,
        )
        acceptance = {
            "workspace_path": str(root),
            "task_short_id": "KEN-18",
            "plan_path": "plan.md",
            "work_kind": "feature",
            "plan_route": "start",
            "plan_route_context": "not-applicable",
            "review_context": "Legacy disabled selection path.",
        }
        accepted = self.run_plan_contract(
            root,
            mode="accept",
            payload=acceptance,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        snapshot_path = root / ".kent/runtime/KEN-18/plan-contract.json"
        snapshot = json.loads(snapshot_path.read_text())
        self.assertNotIn("coder_selection", snapshot)
        snapshot["task_short_id"] = "KEN-19"
        snapshot_path.write_text(json.dumps(snapshot))
        checked = self.run_plan_contract(
            root,
            mode="check",
            payload={
                "workspace_path": str(root),
                "task_short_id": "KEN-18",
                "review_context": "Continue.",
            },
        )
        self.assertNotEqual(checked.returncode, 0)
        self.assertIn(
            "task_short_id does not match the current task",
            checked.stderr,
        )
        self.assertTrue(plan.is_file())

    def test_plan_contract_routes_invalid_selection_to_revalidation(self) -> None:
        root, plan, contract = self.create_plan_contract_fixture()
        original = plan.read_text()
        snapshot_path = root / ".kent/runtime/KEN-18/plan-contract.json"
        initial_digest = hashlib.sha256(
            contract.normalized_plan(plan).encode("utf-8")
        ).hexdigest()
        plan.write_text(
            original.replace('"complexity": "complex"', '"complexity": "unknown"')
        )
        invalid_digest = hashlib.sha256(
            contract.normalized_plan(plan).encode("utf-8")
        ).hexdigest()
        rejected_first_accept = self.run_plan_contract(
            root,
            mode="accept",
            payload={
                "workspace_path": str(root),
                "task_short_id": "KEN-18",
                "plan_path": "plan.md",
                "work_kind": "feature",
                "plan_route": "start",
                "plan_route_context": "not-applicable",
                "review_context": "Invalid first accepted selection fixture.",
                "accepted_plan_sha256": "not-applicable",
                "reviewed_normalized_sha256": invalid_digest,
            },
        )
        self.assertEqual(
            rejected_first_accept.returncode,
            0,
            rejected_first_accept.stderr,
        )
        rejected_result = json.loads(rejected_first_accept.stdout)
        self.assertEqual(
            rejected_result["transition"],
            "plan_contract_revalidate",
        )
        self.assertEqual(
            rejected_result["accepted_plan_sha256"],
            "not-applicable",
        )
        self.assertFalse(snapshot_path.exists())
        plan.write_text(original)

        accepted = self.run_plan_contract(
            root,
            mode="accept",
            payload={
                "workspace_path": str(root),
                "task_short_id": "KEN-18",
                "plan_path": "plan.md",
                "work_kind": "feature",
                "plan_route": "start",
                "plan_route_context": "not-applicable",
                "review_context": "Reviewed and approved.",
                "accepted_plan_sha256": "not-applicable",
                "reviewed_normalized_sha256": initial_digest,
            },
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        invalid_plans = {
            "wrong_task": original.replace(
                '"task_short_id": "KEN-18"',
                '"task_short_id": "KEN-19"',
            ),
            "unknown_field": original.replace(
                '"complexity": "complex",',
                '"complexity": "complex",\n  "model": "gpt-6.1-sol",',
            ),
            "missing_selection": original.replace(
                "Complexity decision:\n\n"
                "```json\n"
                "{\n"
                '  "schema": "coder-selection-v1",\n'
                '  "task_short_id": "KEN-18",\n'
                '  "complexity": "complex",\n'
                '  "rationale": "The change affects workflow contracts."\n'
                "}\n"
                "```\n",
                "No typed complexity decision.\n",
            ),
            "duplicate_field": original.replace(
                '"complexity": "complex",',
                '"complexity": "complex",\n  "complexity": "simple",',
            ),
            "malformed_json": original.replace(
                '"complexity": "complex",',
                '"complexity": complex,',
            ),
            "duplicate_selection": original
            + "\n```json\n"
            + original.split("```json\n", 1)[1].split("```", 1)[0]
            + "```\n",
        }
        expected_errors = {
            "wrong_task": "does not match the current task",
            "unknown_field": "unknown",
            "missing_selection": "found 0",
            "duplicate_field": "duplicate JSON object key",
            "malformed_json": "coder selection JSON is malformed",
            "duplicate_selection": "found 2",
        }
        for case, contents in invalid_plans.items():
            with self.subTest(case=case):
                plan.write_text(contents)
                digest = hashlib.sha256(
                    contract.normalized_plan(plan).encode("utf-8")
                ).hexdigest()
                result = self.run_plan_contract(
                    root,
                    mode="accept",
                    payload={
                        "workspace_path": str(root),
                        "task_short_id": "KEN-18",
                        "plan_path": "plan.md",
                        "work_kind": "feature",
                        "plan_route": "start",
                        "plan_route_context": "not-applicable",
                        "review_context": "Invalid accepted selection fixture.",
                        "accepted_plan_sha256": initial_digest,
                        "reviewed_normalized_sha256": digest,
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output["transition"], "plan_contract_revalidate")
                self.assertIn(expected_errors[case], output["plan_change_report"])
                self.assertEqual(
                    json.loads(snapshot_path.read_text())["normalized_sha256"],
                    initial_digest,
                )
        plan.write_text(original)

    def load_schema4_profile(
        self,
        transform=lambda value: value,
    ) -> ProjectProfile:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        profile_directory = root / ".kent"
        profile_directory.mkdir()
        (profile_directory / "workflow-profile.toml").write_text(
            transform(schema4_profile_contents())
        )
        create_work_kind_procedures(root)
        for configured_path in (
            ".kent/release/spec.toml",
            ".kent/release/build.sh",
            ".kent/release/snapshot.toml",
        ):
            path = root / configured_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Test release artifact\n")
        return ProjectProfile.load(root)

    def project_memory_profile_contents(
        self,
        root: Path,
        *,
        schema: int = 3,
    ) -> str:
        if schema == 3:
            contents = EXAMPLE_PROFILE.read_text().replace(
                "[commands]\n",
                (
                    "[commands]\n"
                    'project_memory = ".kent/scripts/workflow-project-memory"\n'
                    'project_memory_core = '
                    '".kent/scripts/workflow_project_memory.py"\n'
                ),
            )
        else:
            contents = schema4_profile_contents()
            contents = contents.replace(
                'kit_managed_commands = ["dispatch"]',
                (
                    'kit_managed_commands = ["dispatch", "project_memory", '
                    '"project_memory_core"]'
                ),
            ).replace(
                'dispatch = ".kent/scripts/workflow-verification-dispatch"\n',
                (
                    'dispatch = ".kent/scripts/workflow-verification-dispatch"\n'
                    'project_memory = '
                    '".kent/scripts/workflow-project-memory"\n'
                    'project_memory_core = '
                    '".kent/scripts/workflow_project_memory.py"\n'
                ),
            ).replace(
                'dispatch = "1.0.0"\n',
                (
                    'dispatch = "1.0.0"\n'
                    'project_memory = "1.0.0"\n'
                    'project_memory_core = "1.0.0"\n'
                ),
            )
        return (
            contents
            + "\n[project_memory]\n"
            + f'root = "{root}"\n'
            + 'project_key = "appsome"\n'
            + 'project_uid = "project-a"\n'
            + 'mode = "r2"\n'
        )

    def create_schema4_sync_project(
        self,
        transform=lambda value: value,
    ) -> tuple[Path, ProjectProfile]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        profile_directory = root / ".kent"
        profile_directory.mkdir()
        contents = transform(schema4_profile_contents())
        profile_path = profile_directory / "workflow-profile.toml"
        profile_path.write_text(contents)
        create_work_kind_procedures(root)
        profile = ProjectProfile.from_toml(
            root,
            contents,
            check_files=False,
        )
        for configured_path in profile.commands.values():
            if configured_path:
                path = root / configured_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# Project-owned command\n")
                path.chmod(0o755)
        for configured_path in profile.adapters.values():
            if configured_path:
                path = root / configured_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# Project-owned adapter\n")
                path.chmod(0o755)
        for key in profile.kit_managed_commands:
            path = root / profile.command(key)
            path.unlink(missing_ok=True)
        for key in profile.kit_managed_adapters:
            path = root / profile.adapter(key)
            path.unlink(missing_ok=True)
        return root, profile

    def schema4_runtime_v2_contents(self) -> str:
        contents = schema4_profile_contents()
        contents = contents.replace(
            'kit_managed_commands = ["dispatch"]',
            (
                'kit_managed_commands = ["runtime_contracts", "verify", '
                '"evidence", "janitor", "wait_pr", "wait_ci", '
                '"github_observation"]'
            ),
        )
        contents = contents.replace(
            'dispatch = ".kent/scripts/workflow-verification-dispatch"\n',
            (
                'dispatch = ".kent/scripts/workflow-verification-dispatch"\n'
                'runtime_contracts = ".kent/scripts/workflow_runtime_contracts.py"\n'
            ),
        )
        contents = contents.replace(
            '[command_versions]\n'
            'dispatch = "1.0.0"\n',
            (
                '[command_versions]\n'
                'runtime_contracts = "2.0.0"\n'
                'verify = "2.0.0"\n'
                'evidence = "2.0.0"\n'
                'janitor = "2.0.0"\n'
                'wait_pr = "3.0.0"\n'
                'wait_ci = "3.0.0"\n'
                'github_observation = "1.0.0"\n'
            ),
        )
        return contents

    def source_ci_profile_contents(self) -> str:
        return (
            self.schema4_runtime_v2_contents()
            .replace('"github_observation"]', '"github_observation", "prepare_ci"]')
            .replace('wait_ci = "3.0.0"\n', 'wait_ci = "3.0.0"\nprepare_ci = "2.0.0"\n')
            .replace(
                'wait_ci = ".kent/scripts/workflow-wait-github-ci"',
                'wait_ci = ".kent/scripts/workflow-wait-github-ci"\n'
                'prepare_ci = ".kent/scripts/workflow-prepare-github-ci"',
            )
        )

    def run_sync_project_adapters(
        self,
        root: Path,
        *,
        update: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            str(REPO_ROOT / "scripts" / "sync-project-adapters"),
            "--project",
            str(root),
        ]
        if update:
            command.append("--update")
        return subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_subagent_configuration_preserves_operating_settings(self) -> None:
        config = tomllib.loads(
            (REPO_ROOT / "config" / "subagents.toml").read_text()
        )
        # reasoning, agent callability, workflow callability, shell, patch
        expected_roles = {
            "default": ("low", None, None, None, None),
            "defaultExtra": ("low", None, None, None, None),
            "fast": ("high", None, None, None, None),
            "compliance_reviewer": ("high", False, False, True, False),
            "researcher": ("high", True, True, True, False),
            "standards-reviewer": ("medium", False, False, True, False),
            "spec-reviewer": ("medium", False, False, True, False),
            "architecture-designer": ("high", True, True, True, False),
            "grill": ("high", True, True, True, False),
            "task-supervisor": ("high", True, False, True, False),
            "implementation-worker": ("xhigh", True, True, True, True),
            "implementation-simple": ("xhigh", True, True, True, True),
            "implementation-complex": ("medium", True, True, True, True),
            "complexity-planner": ("medium", True, True, None, None),
            "fix-worker": ("medium", True, True, True, True),
            "build-doctor": ("high", True, True, True, True),
            "workflow-gate": ("high", False, False, True, False),
            "runtime-smoke-tester": ("high", True, False, True, False),
            "release-manager": ("medium", False, False, True, True),
            "delivery-operator": ("high", False, False, True, False),
            "ci-monitor": ("low", True, False, True, False),
            "release-decision": ("low", False, False, False, False),
        }
        roles = config["subagents"]
        self.assertEqual(set(roles), set(expected_roles))
        selectors = {"root": config, "reviewer": config["reviewer"], **roles}
        expected_models = {
            "root": "gpt-6.1-sol",
            "reviewer": "gpt-6-luna",
            "default": "gpt-6.1-sol",
            "defaultExtra": "gpt-6-astra",
            "fast": "gpt-6-luna",
            "compliance_reviewer": "gpt-6-luna",
            "researcher": "gpt-6.1-sol",
            "standards-reviewer": "gpt-6-astra",
            "spec-reviewer": "gpt-6-astra",
            "architecture-designer": "gpt-6-astra",
            "grill": "gpt-6-astra",
            "task-supervisor": "gpt-6.1-sol",
            "implementation-worker": "gpt-6-luna",
            "implementation-simple": "gpt-5.6-luna",
            "implementation-complex": "gpt-6.1-sol",
            "complexity-planner": "gpt-6-astra",
            "fix-worker": "gpt-6.1-sol",
            "build-doctor": "gpt-6.1-sol",
            "workflow-gate": "gpt-6-luna",
            "runtime-smoke-tester": "gpt-6-luna",
            "release-manager": "gpt-6-astra",
            "delivery-operator": "gpt-6-luna",
            "ci-monitor": "gpt-6-luna",
            "release-decision": "gpt-6-luna",
        }
        self.assertEqual(
            {name: selector["model"] for name, selector in selectors.items()},
            expected_models,
        )
        luna_selectors = {
            name for name, model in expected_models.items() if model == "gpt-6-luna"
        }
        luna_roles = luna_selectors.intersection(roles)
        expected_reasoning = {
            "root": "high",
            "reviewer": "xhigh",
            **{name: policy[0] for name, policy in expected_roles.items()},
        }
        self.assertEqual(
            {name: selector["thinking_level"] for name, selector in selectors.items()},
            expected_reasoning,
        )
        for name, selector in selectors.items():
            with self.subTest(selector=name):
                self.assertEqual(selector["model_verbosity"], "low")
        self.assertEqual(
            {
                name: selector["model_context_window"]
                for name, selector in selectors.items()
                if "model_context_window" in selector
            },
            {
                "root": 400000,
                **{name: 372000 for name in luna_selectors},
                "implementation-simple": 372000,
                "implementation-complex": 400000,
                "complexity-planner": 400000,
            },
        )
        self.assertEqual(
            {
                name: selector["priority_request_mode"]
                for name, selector in selectors.items()
                if "priority_request_mode" in selector
            },
            {"fast": False, "ci-monitor": False},
        )
        for name, (_, callable_, workflow_callable, shell, patch) in expected_roles.items():
            role = roles[name]
            with self.subTest(role=name):
                if name in {"default", "defaultExtra"}:
                    self.assertEqual(
                        role,
                        {
                            "model": expected_models[name],
                            "thinking_level": "low",
                            "model_verbosity": "low",
                        },
                    )
                    effective_default = {**config, **role}
                    self.assertEqual(effective_default["model"], expected_models[name])
                    self.assertEqual(effective_default["thinking_level"], "low")
                    self.assertEqual(effective_default["model_context_window"], 400000)
                elif name == "fast":
                    for key in ("agent_callable", "workflow_subagent", "tools"):
                        self.assertNotIn(key, role)
                elif name in {
                    "implementation-simple",
                    "implementation-complex",
                }:
                    self.assertIs(role["agent_callable"], callable_)
                    self.assertIs(role["workflow_subagent"], workflow_callable)
                    self.assertEqual(
                        role["tools"],
                        roles["implementation-worker"]["tools"],
                    )
                    self.assertEqual(
                        role["system_prompt_file"],
                        roles["implementation-worker"]["system_prompt_file"],
                    )
                elif name == "complexity-planner":
                    self.assertIs(role["agent_callable"], callable_)
                    self.assertIs(role["workflow_subagent"], workflow_callable)
                    self.assertNotIn("system_prompt_file", role)
                    self.assertNotIn("tools", role)
                else:
                    self.assertIs(role["agent_callable"], callable_)
                    self.assertIs(role["workflow_subagent"], workflow_callable)
                    self.assertEqual(
                        role["tools"],
                        {"shell": shell, "patch": patch, "edit": False},
                    )
                    self.assertEqual(role["system_prompt_file"], f"agents/{name}.md")
        self.assertEqual(roles["compliance_reviewer"]["skills"], {"planning": False})
        self.assertEqual(config["reviewer"]["frequency"], "edits")
        self.assertEqual(config["workflow"], {"subagents": True, "concurrency": 4})
        self.assertEqual(config["max_subagent_depth"], 1)

        compaction_settings = {}

        def collect_compaction_keys(value, path=()) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    child_path = (*path, key)
                    if "compact" in key.lower():
                        compaction_settings[child_path] = child
                    collect_compaction_keys(child, child_path)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    collect_compaction_keys(child, (*path, index))

        collect_compaction_keys(config)
        self.assertEqual(
            compaction_settings,
            {
                ("context_compaction_threshold_tokens",): 360000,
                **{
                    ("subagents", name, "context_compaction_threshold_tokens"): 353400
                    for name in luna_roles
                },
                (
                    "subagents",
                    "implementation-simple",
                    "context_compaction_threshold_tokens",
                ): 353400,
                **{
                    ("subagents", name, "context_compaction_threshold_tokens"): 360000
                    for name in (
                        "implementation-complex",
                        "complexity-planner",
                    )
                },
            },
        )


    def test_current_complexity_lifecycle_for_both_selected_coders(self) -> None:
        profile = ProjectProfile.load(REPO_ROOT)
        selected_profile = replace(
            profile,
            policies={
                **profile.policies,
                "writer_sessions": "continuous",
                "coder_selection": "complexity",
            },
        )
        builder_loader = SourceFileLoader(
            "ken18_kit_development_complete_cycle",
            str(REPO_ROOT / ".kent/workflows/kit_development.py"),
        )
        builder_spec = importlib.util.spec_from_loader(
            builder_loader.name,
            builder_loader,
        )
        self.assertIsNotNone(builder_spec)
        if builder_spec is None:
            return
        builder = importlib.util.module_from_spec(builder_spec)
        sys.modules[builder_spec.name] = builder
        builder_loader.exec_module(builder)
        workflow = builder.build_current_workflow(selected_profile)
        nodes = {node.key: node for node in workflow.nodes}
        edges = {edge.key: edge for edge in workflow.edges}
        roles = tomllib.loads(
            (REPO_ROOT / "config" / "subagents.toml").read_text()
        )["subagents"]
        expected = {
            "simple": ("implementation-simple", "gpt-5.6-luna", "xhigh", 372000, 353400),
            "complex": ("implementation-complex", "gpt-6.1-sol", "medium", 400000, 360000),
        }

        self.assertEqual(
            (nodes["plan"].agent, nodes["plan_revalidation"].agent),
            ("complexity-planner", "complexity-planner"),
        )
        planner = roles["complexity-planner"]
        self.assertEqual(
            (
                planner["model"],
                planner["thinking_level"],
                planner["model_context_window"],
                planner["context_compaction_threshold_tokens"],
            ),
            ("gpt-6-astra", "medium", 400000, 360000),
        )
        first_entry = edges["plan_contract_implement"]
        revalidation = edges["implement_revalidate"]
        resume = edges["plan_contract_checked_continue"]
        recovery = edges["implement_needs_user_action"]
        self.assertEqual(
            (
                first_entry.context,
                first_entry.context_source,
                first_entry.assignee_selection,
                first_entry.thinking_selection,
            ),
            ("new_session", "immediate_source", "previous_node", "previous_node"),
        )
        self.assertEqual(
            (revalidation.context, revalidation.context_source),
            ("continue_session", "node:plan"),
        )
        self.assertEqual(
            (resume.context, resume.context_source),
            ("continue_session", "previous_target_or_new"),
        )
        self.assertEqual(
            (recovery.context, recovery.context_source, recovery.requires_approval),
            ("continue_session", "immediate_source", True),
        )
        recovery_prompt = " ".join((recovery.prompt or "").split())
        self.assertIn(
            "Continue in this same retained Implement Session",
            recovery_prompt,
        )
        self.assertNotIn("Use the retained compacted context", recovery_prompt)
        self.assertIn(
            "effective model, reasoning level, context window, and identity",
            recovery_prompt,
        )
        self.assertTrue(
            {"task_short_id", "accepted_plan_sha256"}
            <= {parameter.key for parameter in recovery.parameters}
        )
        self.assertTrue(edges["plan_review_accept"].requires_approval)

        selected_edges = {
            edge.key
            for edge in workflow.edges
            if any(
                parameter.purpose in {"target_assignee", "target_thinking"}
                for parameter in edge.parameters
            )
        }
        self.assertEqual(selected_edges, {"plan_contract_implement"})
        self.assertEqual(nodes["fix"].agent, "fix-worker")
        self.assertEqual(
            (
                edges["fix_needs_user_action"].context,
                edges["fix_needs_user_action"].context_source,
            ),
            ("compact_and_continue_session", "immediate_source"),
        )
        self.assertFalse(
            {
                parameter.purpose
                for parameter in edges["implement_verify"].parameters
            }
            & {"target_assignee", "target_thinking"}
        )
        for edge in workflow.edges:
            if edge.source in {"verification_dispatch", "deterministic_verify"}:
                with self.subTest(fanout_edge=edge.key):
                    self.assertFalse(
                        {
                            parameter.purpose for parameter in edge.parameters
                        }
                        & {"target_assignee", "target_thinking"}
                    )

        plan_prompt_text = " ".join(
            plan_prompt(selected_profile, coder_selection=True).split()
        )
        self.assertIn("every other case or uncertainty is complex", plan_prompt_text)
        writer_prompt = " ".join(
            implement_prompt(
                selected_profile,
                coder_selection=True,
                first_selected_session=True,
            ).split()
        )
        for requirement in (
            "identity differs from the retained Astra Plan Session",
            "do not use continuation or recovery to create another writer",
        ):
            with self.subTest(prompt=requirement):
                self.assertIn(requirement, writer_prompt)

        for complexity, (
            expected_role,
            expected_model,
            expected_effort,
            expected_window,
            expected_compaction,
        ) in expected.items():
            with self.subTest(complexity=complexity, phase="initial entry"):
                root, plan, contract = self.create_plan_contract_fixture()
                if complexity == "simple":
                    plan.write_text(
                        plan.read_text().replace(
                            '"complexity": "complex"',
                            '"complexity": "simple"',
                        )
                    )
                initial_digest = hashlib.sha256(
                    contract.normalized_plan(plan).encode("utf-8")
                ).hexdigest()
                plan_session_id = f"plan-session-{complexity}"
                writer_session_id = f"writer-session-{complexity}"
                plan_sessions = [
                    {
                        "session_id": plan_session_id,
                        "agent_role": "complexity-planner",
                    }
                ]
                start = self.run_plan_contract(
                    root,
                    mode="accept",
                    payload={
                        "workspace_path": str(root),
                        "task_short_id": "KEN-18",
                        "plan_path": "plan.md",
                        "work_kind": "feature",
                        "plan_route": "start",
                        "plan_route_context": "not-applicable",
                        "review_context": "Reviewed selection and human approval.",
                        "accepted_plan_sha256": "not-applicable",
                        "reviewed_normalized_sha256": initial_digest,
                    },
                    session_rows=plan_sessions,
                )
                self.assertEqual(start.returncode, 0, start.stderr)
                started = json.loads(start.stdout)
                self.assertEqual(
                    (
                        started["transition"],
                        started["target_assignee"],
                        started["target_thinking"],
                        started["accepted_plan_sha256"],
                    ),
                    (
                        "plan_contract_start",
                        expected_role,
                        expected_effort,
                        initial_digest,
                    ),
                )
                coder = roles[expected_role]
                self.assertEqual(
                    (
                        coder["model"],
                        coder["thinking_level"],
                        coder["model_context_window"],
                        coder["context_compaction_threshold_tokens"],
                        coder["agent_callable"],
                        coder["workflow_subagent"],
                    ),
                    (
                        expected_model,
                        expected_effort,
                        expected_window,
                        expected_compaction,
                        True,
                        True,
                    ),
                )
                self.assertNotEqual(plan_session_id, writer_session_id)

                writer_sessions = plan_sessions + [
                    {
                        "session_id": writer_session_id,
                        "agent_role": expected_role,
                    }
                ]
                # A material, same-choice plan edit requires Astra revalidation
                # and re-review, then resumes the same retained writer.
                plan.write_text(
                    plan.read_text().replace(
                        "The change affects workflow contracts.",
                        "The reviewed implementation rationale remains valid.",
                    )
                )
                revised_digest = hashlib.sha256(
                    contract.normalized_plan(plan).encode("utf-8")
                ).hexdigest()
                changed = self.run_plan_contract(
                    root,
                    mode="check",
                    payload={
                        "workspace_path": str(root),
                        "task_short_id": "KEN-18",
                        "review_context": "Plan drift requires revalidation.",
                        "accepted_plan_sha256": initial_digest,
                    },
                    session_rows=writer_sessions,
                )
                self.assertEqual(changed.returncode, 0, changed.stderr)
                changed_payload = json.loads(changed.stdout)
                self.assertEqual(
                    changed_payload["transition"],
                    "plan_contract_continue_changed",
                )
                self.assertEqual(
                    changed_payload["accepted_plan_sha256"],
                    initial_digest,
                )
                self.assertNotIn("target_assignee", changed_payload)
                self.assertEqual(writer_sessions[1]["session_id"], writer_session_id)

                reaccepted = self.run_plan_contract(
                    root,
                    mode="accept",
                    payload={
                        "workspace_path": str(root),
                        "task_short_id": "KEN-18",
                        "plan_path": "plan.md",
                        "work_kind": "feature",
                        "plan_route": "continue",
                        "plan_route_context": "not-applicable",
                        "review_context": "Same choice re-reviewed and approved.",
                        "accepted_plan_sha256": initial_digest,
                        "reviewed_normalized_sha256": revised_digest,
                    },
                    session_rows=writer_sessions,
                )
                self.assertEqual(reaccepted.returncode, 0, reaccepted.stderr)
                resumed = json.loads(reaccepted.stdout)
                self.assertEqual(resumed["transition"], "plan_contract_continue")
                self.assertEqual(resumed["accepted_plan_sha256"], revised_digest)
                self.assertNotIn("target_assignee", resumed)
                self.assertNotIn("target_thinking", resumed)
                stable = self.run_plan_contract(
                    root,
                    mode="check",
                    payload={
                        "workspace_path": str(root),
                        "task_short_id": "KEN-18",
                        "review_context": "Resume the accepted writer.",
                        "accepted_plan_sha256": revised_digest,
                    },
                    session_rows=writer_sessions,
                )
                self.assertEqual(stable.returncode, 0, stable.stderr)
                self.assertEqual(
                    json.loads(stable.stdout)["transition"],
                    "plan_contract_continue_stable",
                )

                # Reclassification is reviewed, but cannot replace this writer.
                replacement_complexity = (
                    "complex" if complexity == "simple" else "simple"
                )
                plan.write_text(
                    plan.read_text().replace(
                        f'"complexity": "{complexity}"',
                        f'"complexity": "{replacement_complexity}"',
                    )
                )
                replacement_digest = hashlib.sha256(
                    contract.normalized_plan(plan).encode("utf-8")
                ).hexdigest()
                replacement = self.run_plan_contract(
                    root,
                    mode="accept",
                    payload={
                        "workspace_path": str(root),
                        "task_short_id": "KEN-18",
                        "plan_path": "plan.md",
                        "work_kind": "feature",
                        "plan_route": "continue",
                        "plan_route_context": "not-applicable",
                        "review_context": "Changed choice was re-reviewed.",
                        "accepted_plan_sha256": revised_digest,
                        "reviewed_normalized_sha256": replacement_digest,
                    },
                    session_rows=writer_sessions,
                )
                self.assertEqual(replacement.returncode, 0, replacement.stderr)
                replacement_payload = json.loads(replacement.stdout)
                self.assertEqual(
                    replacement_payload["transition"],
                    "plan_contract_revalidate",
                )
                self.assertIn(
                    "role differs from the accepted coder selection",
                    replacement_payload["plan_change_report"],
                )
                self.assertNotIn("target_assignee", replacement_payload)
                self.assertEqual(
                    json.loads(
                        (root / ".kent/runtime/KEN-18/plan-contract.json").read_text()
                    )["normalized_sha256"],
                    revised_digest,
                )

    def test_session_naming_configuration(self) -> None:
        config = tomllib.loads((REPO_ROOT / "config" / "subagents.toml").read_text())
        self.assertEqual(
            config["nano"],
            {
                "model": "gpt-6-luna",
                "thinking_level": "low",
                "model_verbosity": "low",
                "features": {"session_naming": True},
            },
        )
        self.assertNotIn("connection", config["nano"])
        self.assertNotIn("nano", config["subagents"])

    def test_selected_owner_replan_preserves_digest_through_repeated_acceptance(self) -> None:
        from workflowkit.delivery import owner_replan_instruction

        delivery = '{"schema":"workflow-delivery-context-v1","phase":"pre_pr"}'
        for compliance in (False, True):
            for complexity in ("simple", "complex"):
                with self.subTest(compliance=compliance, complexity=complexity):
                    profile = self.load_profile()
                    profile = replace(
                        profile,
                        delivery_profile="lite",
                        policies={**profile.policies, "writer_sessions": "continuous",
                                  "coder_selection": "complexity"},
                        capabilities={**profile.capabilities, "compliance_review": compliance},
                    )
                    spec = build_delivery_workflow(profile, 1)
                    spec.validate()
                    edges = {edge.key: edge for edge in spec.edges}
                    self.assertEqual("compliance_replan" in edges, compliance)
                    instruction = owner_replan_instruction(profile)
                    for source in (("fix", "compliance") if compliance else ("fix",)):
                        self.assertIn(
                            "accepted_plan_sha256",
                            {p.key for p in edges[f"{source}_replan"].parameters},
                        )
                    self.assertIn("accepted_plan_sha256", instruction)
                    self.assertIn("normalized_sha256", instruction)
                    root, plan, contract = self.create_plan_contract_fixture()
                    shutil.copyfile(
                        REPO_ROOT / "workflowkit/runtime.py",
                        root / ".kent/scripts/workflow_runtime_contracts.py",
                    )
                    plan.write_text(plan.read_text().replace(
                        '"complexity": "complex"', f'"complexity": "{complexity}"',
                    ))
                    digest = hashlib.sha256(contract.normalized_plan(plan).encode()).hexdigest()
                    accepted = self.run_plan_contract(root, mode="accept", session_rows=[], payload={
                        "workspace_path": str(root), "task_short_id": "KEN-18",
                        "plan_path": "plan.md", "work_kind": "feature",
                        "plan_route": "start", "plan_route_context": "not-applicable",
                        "review_context": "Reviewed and approved.",
                        "accepted_plan_sha256": "not-applicable",
                        "reviewed_normalized_sha256": digest, "delivery_context": delivery,
                    })
                    self.assertEqual(accepted.returncode, 0, accepted.stderr)
                    selected = json.loads(accepted.stdout)
                    sessions = [
                        {"session_id": "retained-plan", "agent_role": "complexity-planner"},
                        {"session_id": "retained-writer", "agent_role": selected["target_assignee"]},
                        {"session_id": "retained-fix", "agent_role": "fix-worker"},
                    ]
                    for source in (("fix", "compliance") if compliance else ("fix",)):
                        edge = edges[f"{source}_replan"]
                        self.assertEqual((edge.context, edge.context_source),
                                         ("continue_session", "node:plan"))
                        self.assertIn("Previous accepted plan digest:", edge.prompt)
                        self.assertIn("same-choice", edge.prompt)
                        # Traverse actual generated edge carriers; missing required
                        # fields fail here before the real Accept wrapper is invoked.
                        for repetition in range(2):
                            packet = {
                                "workspace_path": str(root), "plan_path": "plan.md",
                                "work_kind": "feature", "review_context": "Owner finding reconciled.",
                                "plan_change_report": "Within accepted selection.",
                                "delivery_context": delivery, "accepted_plan_sha256": digest,
                            }
                            for key in (f"{source}_replan", "plan_revalidation_review",
                                        "plan_review_accept"):
                                packet = {p.key: packet[p.key] for p in edges[key].parameters}
                                self.assertEqual(packet["accepted_plan_sha256"], digest)
                                self.assertEqual(packet["delivery_context"], delivery)
                                if key == f"{source}_replan":
                                    # Owner prompt fixes the reviewed route, not a
                                    # new writer start or Fix-specific continuation.
                                    packet.update(plan_route="continue",
                                                  plan_route_context="not-applicable",
                                                  task_short_id="KEN-18")
                                elif key == "plan_revalidation_review":
                                    packet["reviewed_normalized_sha256"] = digest
                                    packet["plan_review_report"] = "Independent review PASS."
                            result = self.run_plan_contract(
                                root, mode="accept", payload=packet, session_rows=sessions,
                            )
                            self.assertEqual(result.returncode, 0, result.stderr)
                            resumed = json.loads(result.stdout)
                            self.assertEqual(resumed["transition"], "plan_contract_continue")
                            self.assertEqual(resumed["accepted_plan_sha256"], digest)
                            self.assertEqual(resumed["delivery_context"], delivery)
                            self.assertNotIn("target_assignee", resumed)
                            self.assertNotIn("target_thinking", resumed)
                            target = edges["plan_contract_continue_implement"]
                            self.assertEqual(target.target, "implement")
                            self.assertEqual((target.context, target.context_source),
                                             ("continue_session", "previous_target_or_new"))
                            snapshot = json.loads(
                                (root / ".kent/runtime/KEN-18/plan-contract.json").read_text()
                            )
                            self.assertEqual(snapshot["coder_selection"]["complexity"], complexity)
                            self.assertEqual(snapshot["coder_selection_sha256"], digest)
                            self.assertEqual(sessions[1]["session_id"], "retained-writer")

    def test_replan_routes_preserve_review_and_owner_context(self) -> None:
        profile = self.load_profile()
        spec = build_delivery_workflow(profile, 99)
        edges = {edge.key: edge for edge in spec.edges}
        for source in ("fix", "compliance"):
            with self.subTest(source=source):
                edge = edges[f"{source}_replan"]
                self.assertEqual(edge.transition, f"{source}_replan")
                self.assertEqual(edge.target, "plan_revalidation")
                self.assertEqual(edge.context, "continue_session")
                self.assertEqual(edge.context_source, "node:plan")
                keys = {parameter.key for parameter in edge.parameters}
                self.assertTrue({
                    "workspace_path", "plan_path", "work_kind",
                    "review_context", "plan_change_report",
                }.issubset(keys))
                self.assertNotIn("plan_route_context", keys)
                self.assertIn("continue", edge.prompt)
                self.assertIn("remaining actionable findings", edge.prompt)
        self.assertEqual(edges["plan_revalidation_review"].target, "plan_review")
        self.assertEqual(edges["plan_review_accept"].target, "plan_contract")
        spec.validate()

    def test_plan_contract_revalidation_exists_for_every_selection_and_context_mode(
        self,
    ) -> None:
        from workflowkit.delivery import _delivery_context_enabled

        def v1(contents: str) -> str:
            return contents.replace(
                "[policies]\n",
                '[policies]\ndelivery_reliability = "v1"\n',
            )

        def complexity(contents: str) -> str:
            return v1(contents).replace(
                'writer_sessions = "fresh_per_slice"\n',
                'writer_sessions = "continuous"\n'
                'coder_selection = "complexity"\n',
            )

        runtime_v2 = self.load_schema4_profile(
            lambda _: self.source_ci_profile_contents()
        )
        current = ProjectProfile.from_toml(
            REPO_ROOT,
            (REPO_ROOT / ".kent/workflow-profile.toml").read_text(),
            check_files=False,
        )
        cases = (
            ("legacy-disabled", self.load_profile()),
            ("v1-disabled-no-context", self.load_profile(v1)),
            ("v1-complexity-no-context", self.load_profile(complexity)),
            ("runtime-v2-disabled", runtime_v2),
            ("kit-v1-disabled", current),
            (
                "kit-v1-complexity",
                replace(
                    current,
                    policies={
                        **current.policies,
                        "coder_selection": "complexity",
                    },
                ),
            ),
        )
        common = (
            "workspace_path",
            "plan_path",
            "work_kind",
            "plan_route",
            "plan_route_context",
            "plan_change_report",
            "review_context",
            "task_short_id",
        )
        for name, profile in cases:
            spec = build_delivery_workflow(profile, 1)
            matches = [
                edge for edge in spec.edges
                if edge.key == "plan_contract_revalidate"
            ]
            with self.subTest(profile=name):
                self.assertEqual(len(matches), 1)
                edge = matches[0]
                self.assertEqual(
                    (edge.source, edge.transition, edge.target),
                    (
                        "plan_contract",
                        "plan_contract_revalidate",
                        "plan_revalidation",
                    ),
                )
                self.assertEqual(
                    (edge.context, edge.context_source, edge.requires_approval),
                    ("continue_session", "node:plan", False),
                )
                expected = common
                if profile.coder_selection_policy() == "complexity":
                    expected += ("accepted_plan_sha256",)
                if _delivery_context_enabled(profile):
                    expected += ("delivery_context",)
                self.assertEqual(
                    tuple(parameter.key for parameter in edge.parameters),
                    expected,
                )
                self.assertIn("accepted plan contract", edge.transition_description)
                self.assertNotIn("coder choice", edge.transition_description)
                self.assertIn("retained planning context", edge.prompt)

    def test_gate_retry_is_a_fresh_unapproved_dispatch_for_every_review_matrix(
        self,
    ) -> None:
        from workflowkit.delivery import (
            _delivery_context_enabled,
            verification_gate_prompt,
        )

        base = self.load_profile()
        profiles = []
        for standards in (False, True):
            for specification in (False, True):
                profiles.append(
                    replace(
                        base,
                        capabilities={
                            **base.capabilities,
                            "standards_review": standards,
                            "spec_review": specification,
                        },
                    )
                )
        current = ProjectProfile.from_toml(
            REPO_ROOT,
            (REPO_ROOT / ".kent/workflow-profile.toml").read_text(),
            check_files=False,
        )
        profiles.append(current)

        for index, profile in enumerate(profiles):
            spec = build_delivery_workflow(profile, 1)
            edges = {edge.key: edge for edge in spec.edges}
            with self.subTest(profile=index):
                edge = edges["gate_retry_verification"]
                self.assertEqual(
                    (edge.source, edge.transition, edge.target),
                    (
                        "verification_gate",
                        "verification_gate_retry_verification",
                        "verification_dispatch",
                    ),
                )
                self.assertEqual(
                    (edge.context, edge.context_source, edge.requires_approval),
                    ("new_session", "immediate_source", False),
                )
                expected = ("workspace_path", "review_context")
                if _delivery_context_enabled(profile):
                    expected += ("delivery_context",)
                self.assertEqual(
                    tuple(parameter.key for parameter in edge.parameters),
                    expected,
                )
                self.assertIsNone(edge.prompt)
                prompt = verification_gate_prompt(profile)
                compact = " ".join(prompt.split())
                self.assertIn(
                    "only when every enabled status is `passed` for the current "
                    "authorized stage",
                    compact,
                )
                self.assertIn("A verification retry is not delivery readiness.", compact)
                self.assertIn(
                    "changed prerequisites, and settlement of prior owned "
                    "processes/effects",
                    compact,
                )
                self.assertIn(
                    "every enabled verification and review branch fresh",
                    compact,
                )
                self.assertIn(
                    "rewrite opaque producer packets",
                    compact,
                )
                self.assertNotIn(
                    "Continue only when every enabled branch passes",
                    compact,
                )

    def test_smoke_cancellation_requires_approval_and_settlement(self) -> None:
        from workflowkit.delivery import _delivery_context_enabled

        current = ProjectProfile.from_toml(
            REPO_ROOT,
            (REPO_ROOT / ".kent/workflow-profile.toml").read_text(),
            check_files=False,
        )
        base = self.load_profile()
        for policy in ("disabled", "conditional", "required"):
            for pull_requests in (False, True):
                for compliance in (False, True):
                    profile = replace(
                        base,
                        policies={**base.policies, "smoke": policy},
                        capabilities={
                            **base.capabilities,
                            "pull_requests": pull_requests,
                            "ci_monitoring": pull_requests,
                            "compliance_review": compliance,
                        },
                    )
                    spec = build_delivery_workflow(profile, 1)
                    edges = {edge.key: edge for edge in spec.edges}
                    with self.subTest(
                        policy=policy,
                        pull_requests=pull_requests,
                        compliance=compliance,
                    ):
                        self.assertEqual(
                            "smoke" in {n.key for n in spec.nodes},
                            policy != "disabled",
                        )
                        self.assertEqual(
                            "smoke_wont_do" in edges,
                            policy != "disabled",
                        )
                        self.assertEqual(
                            "gate_delivery_ready" in edges,
                            policy != "required",
                        )
                        if policy == "disabled":
                            continue
                        cancel = edges["smoke_wont_do"]
                        self.assertEqual(
                            (cancel.source, cancel.transition, cancel.target),
                            ("smoke", "smoke_wont_do", "wont_do"),
                        )
                        self.assertTrue(cancel.requires_approval)
                        self.assertEqual(
                            tuple(parameter.key for parameter in cancel.parameters),
                            ("closure_reason",),
                        )
                        self.assertFalse(
                            _delivery_context_enabled(profile)
                            and "delivery_context"
                            in {parameter.key for parameter in cancel.parameters}
                        )
                        initial = " ".join(
                            edges["gate_smoke_required"].prompt.split()
                        )
                        for requirement in (
                            "settling every owned runner",
                            "restore changed state and verify resource release",
                            "Safe retention is allowed only when",
                            "owner, status, safe-state proof, and next action",
                            "Unknown cleanup blocks cancellation",
                            "Do not route through Janitor",
                            "not that approval",
                            "references in `closure_reason` without secrets",
                        ):
                            self.assertIn(requirement, initial)
                        recovery = " ".join(
                            edges["smoke_needs_user_action"].prompt.split()
                        )
                        for requirement in (
                            "does not authorize replay",
                            "safety-only continuation",
                            "Cancellation permits safety settlement only",
                            "infer a TTL release",
                            "borrow a lease",
                        ):
                            self.assertIn(requirement, recovery)

        current_with_smoke_role = replace(
            current,
            roles={**current.roles, "qa": "runtime-smoke-tester"},
            policies={**current.policies, "smoke": "conditional"},
        )
        kit_spec = build_delivery_workflow(current_with_smoke_role, 1)
        kit_edges = {edge.key: edge for edge in kit_spec.edges}
        self.assertTrue(_delivery_context_enabled(current_with_smoke_role))
        self.assertEqual(
            tuple(parameter.key for parameter in kit_edges["smoke_wont_do"].parameters),
            ("closure_reason",),
        )


    def test_invalid_workspace_dispatch_keeps_policy_and_context_independent(
        self,
    ) -> None:
        from workflowkit.delivery import (
            _delivery_context_enabled,
            workspace_path_fix_prompt,
        )

        def with_v1(contents: str) -> str:
            return contents.replace(
                "[policies]\n",
                '[policies]\ndelivery_reliability = "v1"\n',
            )

        runtime_v2 = self.load_schema4_profile(
            lambda _: self.source_ci_profile_contents()
        )
        kit_profile = ProjectProfile.from_toml(
            REPO_ROOT,
            (REPO_ROOT / ".kent/workflow-profile.toml").read_text(),
            check_files=False,
        )
        profiles = (
            ("legacy-without-context", self.load_profile()),
            ("legacy-with-context", runtime_v2),
            ("v1-without-context", self.load_profile(with_v1)),
            ("v1-with-context", kit_profile),
        )
        for name, profile in profiles:
            spec = build_delivery_workflow(profile, 1)
            edge = next(
                edge for edge in spec.edges
                if edge.key == "dispatch_invalid_workspace"
            )
            with self.subTest(profile=name):
                expected = (
                    ("workspace_path",)
                    if profile.delivery_reliability_policy() == "v1"
                    else ()
                ) + ("reported_workspace_path", "fix_context")
                if _delivery_context_enabled(profile):
                    expected += ("delivery_context",)
                self.assertEqual(
                    tuple(parameter.key for parameter in edge.parameters),
                    expected,
                )
                self.assertEqual(
                    (edge.source, edge.transition, edge.target),
                    (
                        "verification_dispatch",
                        "verification_dispatch_invalid_workspace",
                        "fix",
                    ),
                )
                self.assertEqual(
                    (edge.context, edge.context_source, edge.requires_approval),
                    ("new_session", "immediate_source", False),
                )
                prompt = workspace_path_fix_prompt(profile)
                self.assertIn("reported_workspace_path", prompt)
                self.assertIn(
                    "metadata correction only; do not create or update a Fix checkpoint",
                    prompt,
                )
                self.assertIn("preserving its required envelope", prompt)
                if profile.delivery_reliability_policy() == "v1":
                    self.assertIn(
                        "canonical, native-qualified `workspace_path`",
                        prompt,
                    )
                    self.assertIn("Use that exact value unchanged", prompt)
                    self.assertNotIn("git rev-parse", prompt)
                else:
                    self.assertIn("Kent's current task execution root", prompt)
                    self.assertIn("git rev-parse --show-toplevel", prompt)

    def test_replan_profile_matrix_and_parameter_closure(self) -> None:
        source_ci = self.schema4_runtime_v2_contents().replace(
            '"github_observation"]', '"github_observation", "prepare_ci"]',
        ).replace(
            'wait_ci = "3.0.0"\n', 'wait_ci = "3.0.0"\nprepare_ci = "2.0.0"\n',
        ).replace(
            'wait_ci = ".kent/scripts/workflow-wait-github-ci"',
            'wait_ci = ".kent/scripts/workflow-wait-github-ci"\n'
            'prepare_ci = ".kent/scripts/workflow-prepare-github-ci"',
        )
        profiles = [
            self.load_profile(),
            self.load_schema4_profile(lambda _: source_ci),
            ProjectProfile.load(REPO_ROOT),
        ]
        for original in profiles:
            for writer in ("continuous", "fresh_per_slice"):
                profile = replace(
                    original,
                    policies={**original.policies, "writer_sessions": writer},
                )
                spec = build_delivery_workflow(profile, 99)
                edges = {edge.key: edge for edge in spec.edges}
                with self.subTest(profile=profile.project_name, writer=writer):
                    spec.validate()
                    self.assertIn("fix_replan", edges)
                    self.assertEqual(
                        "compliance_replan" in edges,
                        any(node.key == "compliance" for node in spec.nodes),
                    )
                    for source in ("fix", "compliance"):
                        if f"{source}_replan" not in edges:
                            continue
                        edge = edges[f"{source}_replan"]
                        expected = {
                            "workspace_path", "plan_path", "work_kind",
                            "review_context", "plan_change_report",
                        }
                        if "{{.Params.delivery_context}}" in edge.prompt:
                            expected.add("delivery_context")
                        self.assertEqual(
                            {parameter.key for parameter in edge.parameters}, expected,
                        )
                        self.assertNotIn("{{.Params.plan_route", edge.prompt)
                        self.assertEqual(edge.context_source, "node:plan")
                    for edge in spec.edges:
                        if edge.target not in {"fix", "compliance"} or not edge.prompt:
                            continue
                        if edge.key == "dispatch_invalid_workspace":
                            self.assertNotIn("Choose `fix_replan`", edge.prompt)
                            continue
                        self.assertIn(f"Choose `{edge.target}_replan`", edge.prompt)
                        for guard in (
                            "schema_version=1", "normalized_plan UTF-8",
                            "task_short_id={{.TaskShortId}}",
                            "regular non-escaping", "work_kind is declared",
                        ):
                            self.assertIn(guard, edge.prompt)
                    self.assertEqual(
                        edges["plan_revalidation_review"].target, "plan_review",
                    )
                    self.assertEqual(edges["plan_review_accept"].target, "plan_contract")
                    # Global generator does not impose Kit's project-only approval.
                    self.assertFalse(edges["plan_review_accept"].requires_approval)


    def test_task_supervisor_packaged_operational_contract(self) -> None:
        config = tomllib.loads((REPO_ROOT / "config" / "subagents.toml").read_text())
        role = config["subagents"]["task-supervisor"]
        self.assertEqual(role["system_prompt_file"], "agents/task-supervisor.md")
        self.assertTrue(role["agent_callable"])
        self.assertFalse(role["workflow_subagent"])
        self.assertEqual(role["tools"], {"shell": True, "patch": False, "edit": False})
        role_path = REPO_ROOT / role["system_prompt_file"]
        prompt = role_path.read_text()
        self.assertFalse(prompt.lstrip().startswith("---"))
        self.assertNotRegex(prompt, r"(?m)^\s*(model|tools)\s*:")

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

    def test_release_decision_role_is_toolless(self) -> None:
        config = tomllib.loads(
            (REPO_ROOT / "config" / "subagents.toml").read_text()
        )
        role = config["subagents"]["release-decision"]
        self.assertEqual(role["system_prompt_file"], "agents/release-decision.md")
        self.assertFalse(role["agent_callable"])
        self.assertFalse(role["workflow_subagent"])
        self.assertEqual(role["tools"], {"shell": False, "patch": False, "edit": False})
        prompt = (REPO_ROOT / "agents" / "release-decision.md").read_text()
        self.assertNotIn("model:", prompt)
        self.assertNotIn("tools:", prompt)

    def test_edge_rejects_malformed_template_placeholders(self) -> None:
        malformed_prompts = (
            "Task {{{{.TaskShortId}}}}",
            "Task {{{.TaskShortId}}}",
            "Task {{.TaskShortId}}}",
            "Task {{.TaskShortId}",
            "Task {.TaskShortId}}",
            "Task {{TaskShortId}}",
            "Task {{.Params.invalid-key}}",
        )

        for prompt in malformed_prompts:
            with self.subTest(prompt=prompt):
                edge = EdgeSpec(
                    key="malformed_prompt",
                    source="backlog",
                    transition="start",
                    target="implement",
                    prompt=prompt,
                    transition_description="Start implementation.",
                )

                with self.assertRaisesRegex(
                    SpecError,
                    "malformed template placeholder",
                ):
                    edge.validate()

    def test_edge_accepts_supported_template_placeholders(self) -> None:
        edge = EdgeSpec(
            key="valid_prompt",
            source="backlog",
            transition="start",
            target="implement",
            prompt=(
                "Task {{.TaskShortId}} in "
                "{{.Params.verification_dispatch_fanout_verify.workspace_path}}"
            ),
            transition_description="Start implementation.",
        )

        edge.validate()


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

    def assert_native_join_parameter_contract(self, spec: WorkflowSpec) -> None:
        node_kinds = {node.key: node.kind for node in spec.nodes}
        join_keys = {
            node.key
            for node in spec.nodes
            if node.kind == "join"
        }
        for edge in spec.edges:
            if edge.source in join_keys:
                self.assertEqual(
                    tuple(parameter.key for parameter in edge.parameters),
                    (),
                    edge.key,
                )

        for join_key in join_keys:
            provided: dict[str, str] = {}
            for edge in spec.edges:
                if edge.target != join_key:
                    continue
                self.assertIn(edge.source, node_kinds)
                for parameter in edge.parameters:
                    self.assertNotIn(
                        parameter.key,
                        provided,
                        f"{join_key}: {parameter.key} from "
                        f"{provided.get(parameter.key)} and {edge.key}",
                    )
                    provided[parameter.key] = edge.key

    def test_join_parameter_contract_matches_native_aggregate_rules(self) -> None:
        source_ci_contents = (
            self.schema4_runtime_v2_contents()
            .replace(
                '"github_observation"]',
                '"github_observation", "prepare_ci"]',
            )
            .replace(
                'wait_ci = "3.0.0"\n',
                'wait_ci = "3.0.0"\nprepare_ci = "2.0.0"\n',
            )
            .replace(
                'wait_ci = ".kent/scripts/workflow-wait-github-ci"',
                'wait_ci = ".kent/scripts/workflow-wait-github-ci"\n'
                'prepare_ci = ".kent/scripts/workflow-prepare-github-ci"',
            )
        )
        disabled_contents = (
            self.schema4_runtime_v2_contents()
            .replace("ci_monitoring = true", "ci_monitoring = false")
            .replace(
                ', "wait_ci", "github_observation"]',
                ', "github_observation"]',
            )
            .replace('wait_ci = "3.0.0"\n', "")
        )
        profiles = (
            ("schema3_ci", self.load_profile(), 1),
            (
                "schema4_ci",
                self.load_schema4_profile(lambda _: source_ci_contents),
                1,
            ),
            (
                "schema4_disabled",
                self.load_schema4_profile(lambda _: disabled_contents),
                1,
            ),
            (
                "kit_schema3_lite",
                ProjectProfile.from_toml(
                    REPO_ROOT,
                    (REPO_ROOT / ".kent" / "workflow-profile.toml").read_text(),
                    check_files=False,
                ),
                3,
            ),
        )
        for name, profile, version in profiles:
            with self.subTest(profile=name):
                self.assert_native_join_parameter_contract(
                    build_delivery_workflow(profile, version)
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
        adapter_path = ".kent/adapters/jira/jira-api.sh"
        self.assertNotIn(adapter_path, without_jira_plan)

        jira_profile = replace(
            profile,
            required_adapters=("jira_api",),
            adapters={"jira_api": adapter_path},
        )
        with_jira = build_delivery_workflow(jira_profile, 1)
        with_jira_plan = next(
            edge.prompt for edge in with_jira.edges if edge.target == "plan"
        )
        self.assertIn(adapter_path, with_jira_plan)
        self.assertIn("normalized `issue_links`", with_jira_plan)
        self.assertIn("only root issues explicitly supplied", with_jira_plan)
        self.assertIn("deferred/out-of-scope issues", with_jira_plan)

    def test_sentry_adapter_adds_bounded_plan_instruction(self) -> None:
        profile = self.load_profile()
        without_sentry = build_delivery_workflow(profile, 1)
        without_sentry_plan = next(
            edge.prompt for edge in without_sentry.edges if edge.target == "plan"
        )
        adapter_path = ".kent/adapters/sentry/sentry-issues.sh"
        self.assertNotIn(adapter_path, without_sentry_plan)

        sentry_profile = replace(
            profile,
            required_adapters=("sentry_issues",),
            adapters={
                "sentry_issues": adapter_path,
            },
        )
        with_sentry = build_delivery_workflow(sentry_profile, 1)
        with_sentry_plan = next(
            edge.prompt for edge in with_sentry.edges if edge.target == "plan"
        )
        self.assertIn(adapter_path, with_sentry_plan)
        self.assertIn("explicitly identifies a Sentry issue", with_sentry_plan)
        normalized = " ".join(with_sentry_plan.split())
        self.assertIn("normalized, bounded evidence", normalized)
        self.assertIn("never raw request", normalized)
        self.assertNotIn("mark-seen --allow-mutate", normalized)
        self.assertIn("grants no mutation permission", normalized)
        self.assertIn("Do not resolve or mute an issue in Plan", normalized)

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

    def test_complexity_profile_uses_task_bound_selectors_and_guards(
        self,
    ) -> None:
        def complexity_policy(contents: str) -> str:
            return contents.replace(
                'writer_sessions = "fresh_per_slice"\n',
                'writer_sessions = "continuous"\n'
                'coder_selection = "complexity"\n',
            )

        profile = self.load_profile(complexity_policy)
        spec = build_delivery_workflow(profile, 1)
        nodes = {node.key: node for node in spec.nodes}
        edges = {edge.key: edge for edge in spec.edges}

        self.assertEqual(nodes["plan"].agent, "complexity-planner")
        self.assertEqual(nodes["plan_revalidation"].agent, "complexity-planner")
        self.assertEqual(nodes["implement"].agent, "implementation-worker")

        first = edges["plan_contract_implement"]
        self.assertEqual(
            (first.context, first.context_source),
            ("new_session", "immediate_source"),
        )
        self.assertEqual(first.assignee_selection, "previous_node")
        self.assertEqual(first.thinking_selection, "previous_node")
        self.assertEqual(
            {parameter.purpose for parameter in first.parameters},
            {"ordinary", "target_assignee", "target_thinking"},
        )
        first_keys = {parameter.key for parameter in first.parameters}
        self.assertTrue(
            {"task_short_id", "accepted_plan_sha256", "target_assignee",
             "target_thinking"} <= first_keys
        )

        acceptance_keys = {
            parameter.key for parameter in edges["plan_review_accept"].parameters
        }
        self.assertTrue(
            {
                "accepted_plan_sha256",
                "reviewed_normalized_sha256",
            } <= acceptance_keys
        )
        self.assertEqual(
            edges["plan_contract_revalidate"].target,
            "plan_revalidation",
        )
        self.assertEqual(
            edges["implement_revalidate"].context_source,
            "node:plan",
        )
        self.assertEqual(
            (
                edges["plan_contract_checked_continue"].context,
                edges["plan_contract_checked_continue"].context_source,
            ),
            ("continue_session", "previous_target_or_new"),
        )
        recovery = edges["implement_needs_user_action"]
        self.assertEqual(
            (recovery.context, recovery.context_source),
            ("continue_session", "immediate_source"),
        )
        self.assertTrue(recovery.requires_approval)
        self.assertEqual(
            edges["fix_needs_user_action"].context,
            "compact_and_continue_session",
        )

        branch_profile = self.load_profile(
            lambda contents: complexity_policy(contents)
            .replace('issue_tracker = "none"', 'issue_tracker = "jira"')
            .replace('branch_identity = "task"', 'branch_identity = "jira"')
            .replace(
                "[commands]\n",
                "[commands]\n"
                'branch_identity = ".kent/scripts/workflow-branch-identity"\n',
            ),
            branch_identity_script=True,
        )
        branch_edges = {
            edge.key: edge
            for edge in build_delivery_workflow(branch_profile, 1).edges
        }
        branch_entry = branch_edges["branch_identity_implement"]
        self.assertEqual(
            (branch_entry.context, branch_entry.context_source),
            ("new_session", "immediate_source"),
        )
        self.assertEqual(branch_entry.assignee_selection, "previous_node")
        self.assertEqual(branch_entry.thinking_selection, "previous_node")
        for key in ("plan_contract_branch_identity", "branch_identity_retry"):
            purposes = {
                parameter.purpose
                for parameter in branch_edges[key].parameters
            }
            self.assertEqual(purposes, {"ordinary"})
            self.assertNotIn(
                "target_assignee",
                {parameter.key for parameter in branch_edges[key].parameters},
            )
        self.assertEqual(
            branch_edges["branch_identity_revalidate"].context_source,
            "node:plan",
        )
        self.assertEqual(
            branch_edges["branch_identity_resolution_revalidate"].target,
            "plan_revalidation",
        )


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

    def test_profile_requires_one_plan_review_role_fallback(self) -> None:
        fallback_keys = ("plan_review", "spec_review", "researcher")

        def with_only_fallback(selected):
            def transform(contents):
                contents = contents.replace("spec_review = true\n", "spec_review = false\n")
                contents = re.sub(
                    r'(?m)^(?:plan_review|spec_review|researcher) = "[^"]*"\n',
                    "",
                    contents,
                )
                if selected is not None:
                    key, role = selected
                    contents = contents.replace(
                        "[roles]\n",
                        f'[roles]\n{key} = "{role}"\n',
                        1,
                    )
                return contents

            return transform

        with self.assertRaisesRegex(
            SpecError,
            "requires at least one Plan Review role",
        ):
            self.load_profile(with_only_fallback(None))

        expected_roles = {
            "plan_review": "primary-reviewer",
            "spec_review": "spec-reviewer",
            "researcher": "researcher",
        }
        for key in fallback_keys:
            with self.subTest(fallback=key):
                profile = self.load_profile(
                    with_only_fallback((key, expected_roles[key]))
                )
                spec = build_delivery_workflow(profile, 1)
                plan_review = next(
                    node for node in spec.nodes if node.key == "plan_review"
                )
                self.assertEqual(plan_review.agent, expected_roles[key])

    def test_skill_first_entry_is_generated_for_review_and_delivery_nodes(self) -> None:
        from workflowkit.delivery import owner_replan_instruction, plan_review_prompt

        selected = self.load_profile(
            lambda contents: contents.replace(
                'writer_sessions = "fresh_per_slice"\n',
                'writer_sessions = "continuous"\n'
                'coder_selection = "complexity"\n',
            )
        )
        profiles = (
            self.load_profile(),
            selected,
            ProjectProfile.from_toml(
                REPO_ROOT,
                (REPO_ROOT / ".kent/workflow-profile.toml").read_text(),
                check_files=False,
            ),
        )
        for profile in profiles:
            for manifest, node_key, evidence_type in (
                ("review", "plan_review", "review"),
                ("review", "standards_review", "review"),
                ("review", "spec_review", "review"),
                ("review", "verification_gate", "review"),
                ("review", "compliance", "review"),
                ("delivery", "prepare_pr", "delivery"),
                ("delivery", "ci_monitor", "delivery"),
                ("delivery", "waiting_pr", "delivery"),
                ("delivery", "cleanup", "delivery"),
                ("delivery", "publish_package", "delivery"),
            ):
                with self.subTest(
                    profile=profile.project_name,
                    manifest=manifest,
                    node=node_key,
                ):
                    prompt = context_instruction(
                        profile, manifest, node_key, evidence_type,
                    )
                    compact_prompt = " ".join(prompt.split())
                    self.assertIn(
                        "first read the installed `kent-engineering-kit` skill's `SKILL.md`",
                        compact_prompt,
                    )
                    self.assertIn("block only the dependent action", compact_prompt)
                    self.assertNotIn("resolve-task", compact_prompt)
                    self.assertNotIn("project-memory helper", compact_prompt)
                    if manifest == "review":
                        evidence_owner = (
                            "append only the Gate node's own required evidence"
                            if node_key == "verification_gate"
                            else "append only this node's own required evidence"
                        )
                        self.assertIn(evidence_owner, compact_prompt)
                        self.assertIn(
                            "genuine current Task/Node/Session/Step identity",
                            compact_prompt,
                        )
                        self.assertIn("Do not edit sources or checkpoints", compact_prompt)
                        self.assertIn("append for another producer", compact_prompt)
                        self.assertIn("seal or recover", compact_prompt)

        review = plan_review_prompt(selected, coder_selection=True)
        review = " ".join(review.split())
        self.assertIn(
            "first read the installed `kent-engineering-kit` skill's `SKILL.md`",
            review,
        )
        self.assertIn("block only the dependent action", review)
        replan = owner_replan_instruction(selected)
        replan = " ".join(replan.split())
        self.assertIn(
            "first read the installed `kent-engineering-kit` skill's `SKILL.md`",
            replan,
        )
        self.assertIn("block only the dependent action", replan)

    def test_fix_stage_contracts_keep_checkpoint_ownership_narrow(self) -> None:
        from workflowkit.delivery import (
            evidence_repair_prompt,
            fix_prompt,
            workspace_path_fix_prompt,
        )

        profile = self.load_profile()
        source_fix = fix_prompt(profile)
        source_fix = " ".join(source_fix.split())
        metadata_fix = " ".join(workspace_path_fix_prompt(profile).split())
        evidence_fix = " ".join(evidence_repair_prompt(profile).split())
        self.assertIn("This is Source-Fix", source_fix)
        self.assertIn("Maintain the ignored `fix` checkpoint", source_fix)
        self.assertIn(
            "metadata correction only; do not create or update a Fix checkpoint",
            metadata_fix,
        )
        self.assertIn("retain those exact source locators", metadata_fix)
        self.assertIn(
            "preserving its required envelope and every opaque field byte-for-byte",
            metadata_fix,
        )
        self.assertIn("Do not rewrite, reserialize", metadata_fix)
        self.assertIn("Do not create or update the Fix checkpoint", evidence_fix)

    def test_outcome_guidance_matches_gate_and_plan_review_contracts(self) -> None:
        from workflowkit.delivery import (
            plan_review_prompt,
            verification_gate_prompt,
        )

        for policy in ("disabled", "conditional", "required"):
            baseline = self.load_profile()
            profile = replace(
                baseline,
                policies={**baseline.policies, "smoke": policy},
            )
            gate = verification_gate_prompt(profile)
            compact = " ".join(gate.split())
            self.assertNotIn(
                "Provide `workspace_path`, the resulting `review_context`, "
                "and required Smoke decision fields",
                compact,
            )
            self.assertIn(
                "Return `delivery_context` only when the selected transition "
                "declares it.",
                compact,
            )
            self.assertIn(
                "For `needs_changes`, provide `workspace_path` plus `fix_context`.",
                compact,
            )
            self.assertIn(
                "For `needs_user_action`, provide `workspace_path`, "
                "`review_context`, and `blocker_reason`.",
                compact,
            )
            self.assertIn(
                "For `wont_do`, provide only `closure_reason`.",
                compact,
            )
            self.assertEqual(
                "gate_smoke_required" in {edge.key for edge in build_delivery_workflow(profile, 1).edges},
                policy != "disabled",
            )
            self.assertEqual(
                "gate_delivery_ready" in {edge.key for edge in build_delivery_workflow(profile, 1).edges},
                policy != "required",
            )

        complexity = self.load_profile(
            lambda contents: contents.replace(
                'writer_sessions = "fresh_per_slice"\n',
                'writer_sessions = "continuous"\n'
                'coder_selection = "complexity"\n',
            )
        )
        review = plan_review_prompt(complexity, coder_selection=True)
        review = " ".join(review.split())
        self.assertIn(
            "Only on `accepted`, compute and return `reviewed_normalized_sha256`.",
            review,
        )
        self.assertIn(
            "Do not emit `reviewed_normalized_sha256` or "
            "`accepted_plan_sha256` on any outcome whose selected edge does "
            "not declare that field.",
            review,
        )
        current_profile = ProjectProfile.load(REPO_ROOT)
        delivery_profile = replace(
            current_profile,
            policies={
                **current_profile.policies,
                "coder_selection": "complexity",
            },
        )
        self.assertIn(
            "Preserve `delivery_context` only when the selected transition declares it.",
            " ".join(implement_prompt(delivery_profile, coder_selection=True).split()),
        )

    def test_complexity_storage_guidance_tracks_project_memory_adoption(self) -> None:
        from workflowkit.delivery import (
            owner_replan_instruction,
            plan_revalidation_prompt,
        )

        def selected(contents):
            return contents.replace(
                'writer_sessions = "fresh_per_slice"\n',
                'writer_sessions = "continuous"\n'
                'coder_selection = "complexity"\n'
                'delivery_reliability = "v1"\n',
            )

        unadopted = self.load_profile(selected)
        # This profile-only fixture must stay external even with an in-root TMPDIR.
        root = REPO_ROOT.parent / "kent-plan-guidance-r2-fixture"
        r2_contents = self.project_memory_profile_contents(root).replace(
            'writer_sessions = "fresh_per_slice"\n',
            'writer_sessions = "continuous"\n'
            'coder_selection = "complexity"\n'
            'delivery_reliability = "v1"\n',
        )
        adopted = ProjectProfile.from_toml(
            REPO_ROOT, r2_contents, check_files=False,
        )
        for name, profile in (("unadopted", unadopted), ("R2", adopted)):
            with self.subTest(profile=name):
                implementation = implement_prompt(profile, coder_selection=True)
                revalidation = plan_revalidation_prompt(
                    profile, from_review=False, coder_selection=True,
                )
                owner_replan = owner_replan_instruction(profile)
                guidance = "\n".join((implementation, revalidation, owner_replan))
                guidance = " ".join(guidance.split())
                if name == "R2":
                    self.assertIn("external immutable accepted snapshot", guidance)
                    self.assertIn("local `plan-contract.json` is a disposable cache", guidance)
                    self.assertIn("do not require or recreate it", guidance)
                    self.assertIn("never fall back to legacy storage", guidance)
                    self.assertIn("check-plan", guidance)
                    self.assertIn("resolve-plan", guidance)
                else:
                    self.assertNotIn("workflow-project-memory", guidance)
                    self.assertNotIn("resolve-task", guidance)
                    self.assertIn("only for a Task confirmed as inventoried legacy", guidance)
                    self.assertIn("If the Task is R2 or its mode cannot be proven", guidance)
                    self.assertIn("do not assume legacy mode", guidance)

    def test_current_recovery_uses_guard_owned_work_kind(self) -> None:
        from workflowkit.delivery import (
            work_kind_implement_instruction,
            work_kind_recovery_instruction,
        )

        legacy = self.load_profile()
        current = ProjectProfile.load(REPO_ROOT)
        self.assertIn("{{.Params.work_kind}}", work_kind_recovery_instruction(legacy))
        for instruction in (
            work_kind_implement_instruction(current),
            work_kind_recovery_instruction(current),
        ):
            self.assertIn("accepted snapshot", instruction)
            self.assertIn("Do not emit `work_kind`", instruction)
        spec = build_delivery_workflow(current, 1)
        edges = {edge.key: edge for edge in spec.edges}
        for key in ("implement_continue", "implement_verify"):
            self.assertNotIn(
                "work_kind", {parameter.key for parameter in edges[key].parameters}
            )
        self.assertIn(
            "work_kind",
            {parameter.key for parameter in edges["implement_needs_user_action"].parameters},
        )

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
        self.assertEqual(continuation.target, "plan_contract_continue")
        self.assertEqual(
            tuple(parameter.key for parameter in continuation.parameters),
            (
                "workspace_path",
                "review_context",
                "task_short_id",
            ),
        )
        self.assertEqual(
            implementation_edges["implement_verify"].target,
            "plan_contract_verify",
        )
        self.assertIn(
            "writer-owned plan step",
            by_key["plan_contract_checked_continue"].prompt,
        )
        self.assertIn(
            "when every\nwriter-owned plan step is complete",
            by_key["plan_contract_checked_continue"].prompt,
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
            ("workspace_path", "fix_context", "task_short_id"),
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
            ".kent/scripts/workflow-plan-contract-accept",
        )
        self.assertEqual(
            nodes["plan_contract_continue"].script_path,
            ".kent/scripts/workflow-plan-contract-continue",
        )
        self.assertEqual(
            nodes["plan_contract_verify"].script_path,
            ".kent/scripts/workflow-plan-contract-verify",
        )
        self.assertEqual(
            nodes["plan_contract_fix_continue"].script_path,
            ".kent/scripts/workflow-plan-contract-fix-continue",
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
            by_key["plan_contract_continue_revalidate"].target,
            "plan_revalidation",
        )
        self.assertEqual(
            by_key["plan_contract_checked_verify"].target,
            "verification_dispatch",
        )
        self.assertIn(
            "checkbox state alone",
            by_key["plan_contract_continue_revalidate"].prompt,
        )
        self.assertEqual(
            by_key["fix_verify"].target,
            "plan_contract_verify",
        )
        self.assertEqual(
            tuple(parameter.key for parameter in by_key["fix_verify"].parameters),
            ("workspace_path", "review_context", "task_short_id"),
        )
        self.assertEqual(
            by_key["fix_continue"].target,
            "plan_contract_fix_continue",
        )
        self.assertEqual(
            by_key["plan_contract_fix_revalidate"].target,
            "plan_revalidation",
        )

    def test_plan_review_and_revalidation_resume_only_through_fixed_guards(self) -> None:
        from workflowkit.delivery import (
            plan_revalidation_prompt,
            plan_review_prompt,
        )

        baseline = self.load_profile()
        cases = (
            ("fresh", baseline),
            (
                "continuous",
                replace(
                    baseline,
                    policies={
                        **baseline.policies,
                        "writer_sessions": "continuous",
                    },
                ),
            ),
            (
                "continuous-complexity",
                replace(
                    baseline,
                    policies={
                        **baseline.policies,
                        "writer_sessions": "continuous",
                        "coder_selection": "complexity",
                    },
                ),
            ),
        )
        for name, profile in cases:
            with self.subTest(profile=name):
                spec = build_delivery_workflow(profile, 1)
                edges = {edge.key: edge for edge in spec.edges}
                delivery_fields = (
                    ("delivery_context",)
                    if "delivery_context"
                    in {p.key for p in edges["plan_review"].parameters}
                    else ()
                )
                digest_fields = (
                    ("accepted_plan_sha256",)
                    if profile.coder_selection_policy() == "complexity"
                    else ()
                )
                for source in ("plan_review", "plan_revalidation"):
                    for route, target, fields in (
                        (
                            "continue",
                            "plan_contract_continue",
                            ("workspace_path", "review_context", "task_short_id")
                            + delivery_fields
                            + digest_fields,
                        ),
                        (
                            "verify",
                            "plan_contract_verify",
                            ("workspace_path", "review_context", "task_short_id")
                            + delivery_fields
                            + digest_fields,
                        ),
                    ):
                        key = f"{source}_resume_{route}"
                        edge = edges[key]
                        self.assertEqual(edge.source, source)
                        self.assertEqual(edge.target, target)
                        self.assertEqual(
                            edge.transition,
                            f"{source}_resume_{route}",
                        )
                        self.assertEqual(
                            tuple(parameter.key for parameter in edge.parameters),
                            fields,
                        )
                        self.assertFalse(edge.requires_approval)
                        checked = (
                            "plan_contract_checked_continue"
                            if route == "continue"
                            else "plan_contract_checked_verify"
                        )
                        self.assertEqual(
                            edges[checked].target,
                            "implement" if route == "continue"
                            else "verification_dispatch",
                        )
                        self.assertEqual(
                            edges[f"plan_contract_{route}_revalidate"].target,
                            "plan_revalidation",
                        )
                if profile.writer_session_policy() == "fresh_per_slice":
                    for source in ("plan_review", "plan_revalidation"):
                        edge = edges[f"{source}_resume_fix_continue"]
                        self.assertEqual(edge.source, source)
                        self.assertEqual(
                            edge.target, "plan_contract_fix_continue",
                        )
                        self.assertEqual(
                            tuple(parameter.key for parameter in edge.parameters),
                            ("workspace_path", "fix_context", "task_short_id")
                            + delivery_fields
                            + digest_fields,
                        )
                        self.assertFalse(edge.requires_approval)
                        self.assertEqual(
                            edges["plan_contract_checked_fix"].target,
                            "fix",
                        )
                        self.assertEqual(
                            edges["plan_contract_fix_revalidate"].target,
                            "plan_revalidation",
                        )
                    self.assertIn(
                        "plan_revalidation_resume_fix_continue",
                        " ".join(plan_revalidation_prompt(
                            profile, from_review=False,
                        ).split()),
                    )
                else:
                    self.assertNotIn("plan_contract_fix_continue", {
                        node.key for node in spec.nodes
                    })
                    self.assertNotIn("plan_review_resume_fix_continue", edges)
                    self.assertNotIn(
                        "plan_revalidation_resume_fix_continue", edges,
                    )

                review_prompt = " ".join(plan_review_prompt(profile).split())
                revalidation_prompt = " ".join(
                    plan_revalidation_prompt(
                        profile, from_review=False,
                    ).split()
                )
                for prompt in (review_prompt, revalidation_prompt):
                    self.assertIn("exact previously accepted contract", prompt)
                    self.assertIn("prior independent PASS review", prompt)
                    self.assertIn("no new findings", prompt)
                    self.assertIn("authority/milestone", prompt)
                    self.assertIn("existing Plan Contract guard", prompt)
                    self.assertIn("cannot accept or continue a changed contract", prompt)
                self.assertIn("fresh review result in `review_context`", review_prompt)
                self.assertIn("negative review finding remains unresolved", revalidation_prompt)
                self.assertEqual(edges["plan_review_accept"].target, "plan_contract")


    def test_delivery_preserves_continuous_writer_compatibility(self) -> None:
        profile = self.load_profile(
            lambda contents: contents.replace(
                'writer_sessions = "fresh_per_slice"\n',
                "",
            )
        )
        spec = build_delivery_workflow(profile, 1)
        nodes = {node.key for node in spec.nodes}
        by_key = {edge.key: edge for edge in spec.edges}

        self.assertEqual(profile.writer_session_policy(), "continuous")
        self.assertEqual(
            by_key["plan_contract_implement"].context,
            "compact_and_continue_session",
        )
        self.assertEqual(
            by_key["plan_contract_checked_continue"].context,
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
        self.assertNotIn("plan_contract_fix_continue", nodes)
        self.assertNotIn("plan_contract_fix_revalidate", by_key)
        self.assertNotIn("plan_contract_checked_fix", by_key)
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
        self.assertIn(
            "`task_short_id={{.TaskShortId}}`",
            by_key["gate_fix"].prompt,
        )

    def test_every_fix_entry_prompt_provides_task_identity(self) -> None:
        profiles = (
            self.load_profile(),
            self.load_profile(
                lambda contents: contents.replace(
                    'writer_sessions = "fresh_per_slice"\n',
                    "",
                )
            ),
        )

        for profile in profiles:
            with self.subTest(policy=profile.writer_session_policy()):
                spec = build_delivery_workflow(profile, 1)
                for edge in spec.edges:
                    if (
                        edge.target != "fix"
                        or edge.source == "fix"
                        or edge.prompt is None
                    ):
                        continue
                    self.assertIn("task_short_id", edge.prompt, edge.key)

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


    def test_install_adopts_byte_identical_managed_agent_file(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        kit = root / "kit"
        (kit / "scripts").mkdir(parents=True)
        (kit / "agents").mkdir()
        generated = kit / ".kent" / "scripts"
        generated.mkdir(parents=True)
        installer = kit / "scripts" / "install"
        source = kit / "agents" / "compliance_reviewer.md"
        shutil.copyfile(REPO_ROOT / "scripts" / "install", installer)
        installer.chmod(0o755)
        shutil.copyfile(
            REPO_ROOT / "agents" / "compliance_reviewer.md",
            source,
        )
        adapter = generated / "workflow-project-memory"
        shutil.copyfile(
            REPO_ROOT / "templates" / "project" / "workflow-project-memory",
            adapter,
        )
        adapter.chmod(0o755)
        shutil.copyfile(
            REPO_ROOT / "workflowkit" / "project_memory.py",
            generated / "workflow_project_memory.py",
        )
        kent_root = root / "kent"
        target = kent_root / "agents" / source.name
        target.parent.mkdir(parents=True)
        target.write_bytes(source.read_bytes())

        environment = dict(os.environ)
        environment["KENT_PERSISTENCE_ROOT"] = str(kent_root)
        script = installer
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
        for kind in profile.work_kinds.values():
            self.assertNotIn(kind.plan, prompts)
            self.assertNotIn(kind.implement, prompts)
        self.assertNotIn("gate_smoke_required", by_key)
        self.assertEqual(by_key["gate_delivery_ready"].target, "cleanup")
        self.assertEqual(
            by_key["gate_retry_verification"].transition,
            "verification_gate_retry_verification",
        )
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["gate_retry_verification"].parameters
            ),
            ("workspace_path", "review_context"),
        )

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
            (
                "workspace_path",
                "pr_url",
                "branch_name",
                "merge_strategy",
                "ci_contract",
            ),
        )
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["merge_watch_cleanup"].parameters
            ),
            (
                "workspace_path",
                "pr_url",
                "branch_name",
                "merge_strategy",
                "merge_report",
            ),
        )
        self.assertIsNone(by_key["prepare_pr_ci_watch"].prompt)
        self.assertEqual(by_key["ci_watch_waiting_pr"].target, "waiting_pr")
        self.assertEqual(by_key["ci_watch_state_changed"].target, "waiting_pr")
        self.assertEqual(by_key["ci_watch_diagnose"].target, "ci_monitor")
        self.assertEqual(by_key["ci_watch_merged"].target, "cleanup")
        self.assertEqual(
            tuple(parameter.key for parameter in by_key["prepare_pr_no_pr"].parameters),
            ("pr_report",),
        )
        self.assertTrue(
            all(
                parameter.key != "pr_feedback_cursor"
                for edge in by_key.values()
                for parameter in edge.parameters
            )
        )
        self.assertNotIn(
            "{{.Params.pr_feedback_cursor}}",
            "\n".join(edge.prompt or "" for edge in by_key.values()),
        )

        self.assertFalse(by_key["prepare_pr_fix"].requires_approval)
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
                "ci_contract",
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
                "ci_contract",
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
            "exact terminal watcher report",
            by_key["ci_watch_diagnose"].prompt,
        )
        self.assertIn(
            "Never use `ci_monitor_needs_user_action` merely because CI is still running",
            by_key["ci_watch_diagnose"].prompt,
        )
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
                "ci_contract",
                "ci_report",
            ),
        )
        self.assertEqual(
            tuple(parameter.key for parameter in by_key["waiting_pr_fix"].parameters),
            (
                "workspace_path",
                "merge_strategy",
                "pr_report",
                "ci_contract",
                "ci_report",
            ),
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
                "ci_contract",
                "ci_report",
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
        for expected in (
            "force-with-lease",
            "outcome=needs_user_action",
            "kent-resolve-github-merge-strategy",
        ):
            self.assertIn(expected, generated_prompts)

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


    def test_cleanup_and_janitor_prompts_preserve_workspace_and_branch(self) -> None:
        profile = self.cleanup_profile(managed=True, published=False)
        spec = build_delivery_workflow(profile, 1)
        by_key = {edge.key: edge for edge in spec.edges}
        cleanup_keys = (
            "prepare_pr_no_pr",
            "fix_pr_merged_cleanup",
            "waiting_pr_cleanup",
            "merge_watch_cleanup",
            "waiting_pr_close_without_merge",
        )
        for key in cleanup_keys:
            prompt = by_key[key].prompt
            with self.subTest(edge=key):
                self.assertIn(
                    "`git -C <workspace_path> branch --show-current`",
                    prompt,
                )
                self.assertIn(
                    "including the exact `workspace_path` and `branch_name`",
                    prompt,
                )
                self.assertLess(
                    prompt.index("git -C <workspace_path> branch --show-current"),
                    prompt.index("`kent worktree leave`"),
                )
                self.assertNotIn("`git branch --show-current`", prompt)

        published_profile = self.cleanup_profile(managed=True, published=True)
        published = published_cleanup_prompt(published_profile)
        self.assertLess(
            published.index("git -C <workspace_path> branch --show-current"),
            published.index("`kent worktree leave`"),
        )
        self.assertIn(
            "including the exact `workspace_path` and `branch_name`",
            published,
        )

        recovery = janitor_recovery_prompt(profile)
        self.assertIn(
            "exact `workspace_path` and\n`branch_name` recorded in its `cleanup_report`",
            recovery,
        )
        self.assertIn(
            "`git -C <workspace_path> branch --show-current`",
            recovery,
        )
        self.assertIn(
            "do not infer either from this recovery session's current directory",
            " ".join(recovery.split()),
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
                "`git -C <workspace_path> branch --show-current`",
                by_key[key].prompt,
            )
            self.assertIn(
                "never use `null`, `none`, `not-applicable`",
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
        self.assertTrue(by_key["smoke_wont_do"].requires_approval)
        self.assertEqual(
            tuple(
                parameter.key for parameter in by_key["smoke_wont_do"].parameters
            ),
            ("closure_reason",),
        )
        self.assertEqual(
            by_key["gate_retry_verification"].transition,
            "verification_gate_retry_verification",
        )
        prompts = "\n".join(edge.prompt or "" for edge in spec.edges)
        for kind in profile.work_kinds.values():
            self.assertNotIn(kind.plan, prompts)
            self.assertNotIn(kind.implement, prompts)

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
        snapshot_filename = load_generator().snapshot_filename
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
        snapshot_filename = load_generator().snapshot_filename
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

    def test_profile_accepts_schema_three_extension_tables(self) -> None:
        profile = self.load_profile(
            lambda contents: contents
            + '\n[integrations.example]\nname = "project-owned"\n'
        )

        self.assertEqual(profile.schema_version, 3)
        self.assertEqual(profile.release_topology, "none")
        self.assertIsNone(profile.release)

    def test_schema_three_rejects_only_schema_four_root_keys(self) -> None:
        root_key_values = {
            "kit_managed_commands": 'kit_managed_commands = []\n',
            "command_versions": "[command_versions]\ndispatch = \"1.0.0\"\n",
            "ci_archive_reader_sources": (
                'ci_archive_reader_sources = ["src/evidence_reader.py"]\n'
            ),
            "release": (
                "[release]\n"
                'topology_kind = "appsome-release-publication"\n'
            ),
        }
        for key, fragment in root_key_values.items():
            with self.subTest(key=key):
                def add_root_key(contents: str, fragment: str = fragment) -> str:
                    if fragment.startswith("["):
                        return contents + "\n" + fragment
                    return contents.replace(
                        "schema_version = 3\n",
                        "schema_version = 3\n" + fragment,
                    )

                with self.assertRaisesRegex(
                    SpecError,
                    "schema 3 does not support root keys",
                ):
                    self.load_profile(add_root_key)

    def test_schema_four_ci_archive_reader_sources_are_closed_and_bounded(
        self,
    ) -> None:
        profile = self.load_schema4_profile(
            lambda contents: contents.replace(
                "schema_version = 4\n",
                'schema_version = 4\nci_archive_reader_sources = '
                '["src/evidence_reader.py", "tools/plan_reader.py"]\n',
            )
        )
        self.assertEqual(
            profile.ci_archive_reader_sources,
            ("src/evidence_reader.py", "tools/plan_reader.py"),
        )

        invalid_values = (
            '["src/../reader.py"]',
            '["/absolute/reader.py"]',
            '["src/*.py"]',
            '["src\\\\reader.py"]',
            '["src/reader.py", "src/reader.py"]',
            '[""]',
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaisesRegex(
                SpecError,
                "ci_archive_reader_sources",
            ):
                self.load_schema4_profile(
                    lambda contents, value=value: contents.replace(
                        "schema_version = 4\n",
                        "schema_version = 4\n"
                        f"ci_archive_reader_sources = {value}\n",
                    )
                )

    def test_schema_four_accepts_release_profile_and_disables_legacy_publish(
        self,
    ) -> None:
        profile = self.load_schema4_profile()

        self.assertEqual(profile.schema_version, 4)
        self.assertIsInstance(profile.release, ReleaseProfile)
        self.assertEqual(profile.kit_managed_commands, ("dispatch",))
        self.assertEqual(profile.command_versions, {"dispatch": "1.0.0"})
        self.assertFalse(profile.package_publish_after_main())
        self.assertNotIn(
            "publish_package",
            {node.key for node in build_delivery_workflow(profile, 1).nodes},
        )

    def test_optional_project_memory_profile_is_closed_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            memory_root = root.parent / f"{root.name}-external-memory"
            legacy = ProjectProfile.from_toml(
                root,
                EXAMPLE_PROFILE.read_text(),
                check_files=False,
            )
            profile = ProjectProfile.from_toml(
                root,
                self.project_memory_profile_contents(memory_root),
                check_files=False,
            )

            self.assertIsNone(legacy.project_memory)
            self.assertEqual(legacy.delivery_reliability_policy(), "")
            self.assertEqual(profile.project_memory.root, str(memory_root))
            self.assertEqual(profile.project_memory.project_key, "appsome")
            self.assertEqual(profile.project_memory.project_uid, "project-a")
            self.assertEqual(profile.project_memory.mode, "r2")
            self.assertFalse(memory_root.exists())

            contents = self.project_memory_profile_contents(memory_root).replace(
                "[policies]\n",
                '[policies]\ndelivery_reliability = "v1"\n',
            )
            enabled = ProjectProfile.from_toml(
                root,
                contents,
                check_files=False,
            )
            self.assertEqual(enabled.delivery_reliability_policy(), "v1")

    def test_project_memory_profile_rejects_incomplete_or_unsafe_bindings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            memory_root = root.parent / f"{root.name}-memory"
            valid = self.project_memory_profile_contents(memory_root)
            invalid = (
                (valid.replace('mode = "r2"\n', ""), "exactly"),
                (
                    valid.replace(
                        f'root = "{memory_root}"',
                        'root = "memory"',
                    ),
                    "absolute",
                ),
                (
                    valid.replace(
                        f'root = "{memory_root}"',
                        f'root = "{root}/../R2"',
                    ),
                    "normalized absolute",
                ),
                (
                    self.project_memory_profile_contents(
                        root / "inside-memory"
                    ),
                    "outside the project checkout",
                ),
                (
                    valid.replace(
                        'project_key = "appsome"',
                        'project_key = "../escape"',
                    ),
                    "project_key",
                ),
                (
                    valid.replace(
                        'project_uid = "project-a"',
                        'project_uid = ".."',
                    ),
                    "project_uid",
                ),
                (valid.replace('mode = "r2"', 'mode = "legacy"'), "mode"),
                (
                    valid.replace(
                        'mode = "r2"\n',
                        'mode = "r2"\nunknown = "rejected"\n',
                    ),
                    "exactly",
                ),
            )
            for contents, message in invalid:
                with self.subTest(contents=contents[-80:]):
                    with self.assertRaisesRegex(SpecError, message):
                        ProjectProfile.from_toml(
                            root,
                            contents,
                            check_files=False,
                        )

            unsupported_policy = valid.replace(
                "[policies]\n",
                '[policies]\ndelivery_reliability = "v2"\n',
            )
            with self.assertRaisesRegex(
                SpecError,
                "delivery_reliability",
            ):
                ProjectProfile.from_toml(
                    root,
                    unsupported_policy,
                    check_files=False,
                )

    def test_schema_four_project_memory_requires_versioned_sibling_closure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            contents = self.project_memory_profile_contents(
                root.parent / f"{root.name}-external-memory",
                schema=4,
            )
            profile = ProjectProfile.from_toml(
                root,
                contents,
                check_files=False,
            )
            self.assertEqual(
                profile.command_versions["project_memory"],
                "1.0.0",
            )
            self.assertEqual(
                profile.command_versions["project_memory_core"],
                "1.0.0",
            )
            self.assertEqual(
                Path(profile.command("project_memory")).parent,
                Path(profile.command("project_memory_core")).parent,
            )

            missing_core = contents.replace(
                'project_memory_core = ".kent/scripts/workflow_project_memory.py"\n',
                "",
            )
            with self.assertRaisesRegex(SpecError, "project_memory_core"):
                ProjectProfile.from_toml(
                    root,
                    missing_core,
                    check_files=False,
                )
            wrong_version = contents.replace(
                'project_memory_core = "1.0.0"',
                'project_memory_core = "2.0.0"',
            )
            with self.assertRaisesRegex(SpecError, "project_memory_core"):
                ProjectProfile.from_toml(
                    root,
                    wrong_version,
                    check_files=False,
                )

    def test_schema_four_runtime_v2_requires_prepare_ci_producer_adoption(
        self,
    ) -> None:
        profile = self.load_schema4_profile(
            lambda _: self.schema4_runtime_v2_contents()
        )
        self.assertTrue(profile.runtime_contracts_v2())
        with self.assertRaisesRegex(
            SpecError,
            "requires prepare_ci 2.0.0",
        ):
            build_delivery_workflow(profile, 1)

        coexist = self.schema4_runtime_v2_contents().replace(
            '"github_observation"]',
            '"github_observation", "dispatch"]',
        ).replace(
            'github_observation = "1.0.0"\n',
            'github_observation = "1.0.0"\ndispatch = "1.0.0"\n',
        )
        coexist_profile = self.load_schema4_profile(lambda _: coexist)
        self.assertTrue(coexist_profile.runtime_contracts_v2())

        with self.assertRaisesRegex(SpecError, "exact conditional"):
            self.load_schema4_profile(
                lambda _: self.schema4_runtime_v2_contents().replace(
                    '"wait_pr", "wait_ci", "github_observation"]',
                    '"wait_pr"]',
                ).replace(
                    'wait_ci = "3.0.0"\n',
                    "",
                ).replace(
                    'github_observation = "1.0.0"\n',
                    "",
                )
            )
        with self.assertRaisesRegex(
            SpecError,
            "runtime-contract v2 managed command",
        ):
            self.load_schema4_profile(
                lambda _: self.schema4_runtime_v2_contents().replace(
                    'evidence = "2.0.0"',
                    'evidence = "1.0.0"',
                )
            )

    def test_schema_four_runtime_and_source_ci_previous_family_versions(self) -> None:
        contents = self.source_ci_profile_contents().replace(
            "[policies]\n",
            '[policies]\ndelivery_reliability = "v1"\n',
        )
        for old, new in (
            ('runtime_contracts = "2.0.0"', 'runtime_contracts = "2.1.0"'),
            ('evidence = "2.0.0"', 'evidence = "2.1.0"'),
            ('janitor = "2.0.0"', 'janitor = "3.0.0"'),
            ('wait_pr = "3.0.0"', 'wait_pr = "4.0.0"'),
            ('wait_ci = "3.0.0"', 'wait_ci = "4.0.0"'),
            ('prepare_ci = "2.0.0"', 'prepare_ci = "3.0.0"'),
        ):
            contents = contents.replace(old, new)

        profile = self.load_schema4_profile(lambda _: contents)
        self.assertTrue(profile.runtime_contracts_v2_1())
        self.assertFalse(profile.runtime_contracts_v2_2())
        self.assertTrue(profile.source_ci_contract_v3())
        self.assertEqual(profile.delivery_reliability_policy(), "v1")
        loader = SourceFileLoader(
            "sync_project_adapters_current_family_test",
            str(REPO_ROOT / "scripts/sync-project-adapters"),
        )
        module_spec = importlib.util.spec_from_loader(loader.name, loader)
        self.assertIsNotNone(module_spec)
        if module_spec is None:
            raise AssertionError("could not load the adapter synchronizer")
        synchronizer = importlib.util.module_from_spec(module_spec)
        sys.modules[loader.name] = synchronizer
        loader.exec_module(synchronizer)
        synchronizer.validate_schema4_command_versions(profile)

    def test_schema_four_runtime_and_source_ci_current_family_versions(self) -> None:
        contents = self.source_ci_profile_contents().replace(
            "[policies]\n",
            '[policies]\ndelivery_reliability = "v1"\n',
        )
        for old, new in (
            ('runtime_contracts = "2.0.0"', 'runtime_contracts = "2.2.0"'),
            ('evidence = "2.0.0"', 'evidence = "2.1.0"'),
            ('janitor = "2.0.0"', 'janitor = "3.1.0"'),
            ('wait_pr = "3.0.0"', 'wait_pr = "4.1.0"'),
            ('wait_ci = "3.0.0"', 'wait_ci = "4.1.0"'),
            ('github_observation = "1.0.0"', 'github_observation = "1.1.0"'),
            ('prepare_ci = "2.0.0"', 'prepare_ci = "3.1.0"'),
        ):
            contents = contents.replace(old, new)

        profile = self.load_schema4_profile(lambda _: contents)
        self.assertTrue(profile.runtime_contracts_v2_1())
        self.assertTrue(profile.runtime_contracts_v2_2())
        self.assertTrue(profile.source_ci_contract_v3())
        loader = SourceFileLoader(
            "sync_project_adapters_d15_family_test",
            str(REPO_ROOT / "scripts/sync-project-adapters"),
        )
        module_spec = importlib.util.spec_from_loader(loader.name, loader)
        self.assertIsNotNone(module_spec)
        if module_spec is None:
            raise AssertionError("could not load the adapter synchronizer")
        synchronizer = importlib.util.module_from_spec(module_spec)
        sys.modules[loader.name] = synchronizer
        loader.exec_module(synchronizer)
        synchronizer.validate_schema4_command_versions(profile)

    def test_source_ci_contract_adds_preparation_and_distinct_cycle_edges(
        self,
    ) -> None:
        contents = self.source_ci_profile_contents()
        profile = self.load_schema4_profile(lambda _: contents)
        self.assertTrue(profile.source_ci_contract())
        spec = build_delivery_workflow(profile, 1)
        by_key = {edge.key: edge for edge in spec.edges}
        self.assertIn("ci_prepare", {node.key for node in spec.nodes})
        self.assertEqual(by_key["prepare_pr_ci_prepare"].target, "ci_prepare")
        initial = tuple(
            parameter.key for parameter in by_key["ci_prepare_ready_initial"].parameters
        )
        retry = tuple(
            parameter.key for parameter in by_key["ci_prepare_ready_retry"].parameters
        )
        self.assertNotIn("ci_report", initial)
        self.assertIn("ci_report", retry)
        self.assertNotIn("ci_monitor_waiting_pr", by_key)
        self.assertEqual(by_key["ci_monitor_watch"].target, "ci_prepare")
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["merge_watch_cleanup"].parameters
            ),
            (
                "workspace_path",
                "pr_url",
                "branch_name",
                "merge_strategy",
                "merge_report",
                "pr_feedback_cursor",
            ),
        )
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["ci_watch_waiting_pr"].parameters
            ),
            (
                "workspace_path",
                "pr_url",
                "branch_name",
                "merge_strategy",
                "ci_contract",
                "task_short_id",
                "ci_report",
                "pr_feedback_cursor",
            ),
        )
        cursor_edges = {
            edge.key for edge in spec.edges
            if any(parameter.key == "pr_feedback_cursor" for parameter in edge.parameters)
        }
        self.assertEqual(cursor_edges, {
            "ci_prepare_ready_initial", "ci_prepare_ready_retry",
            "ci_prepare_failed", "ci_watch_waiting_pr", "ci_watch_diagnose",
            "ci_watch_state_changed", "ci_watch_source_changed",
            "ci_monitor_needs_user_action", "waiting_pr_watch_merge",
            "merge_watch_still_waiting", "merge_watch_state_changed",
            "merge_watch_cleanup",
            "waiting_pr_needs_user_action",
        })
        prompts = " ".join((edge.prompt or "") for edge in spec.edges)
        self.assertIn("uninitialized", prompts)
        self.assertIn("{{.Params.pr_feedback_cursor}}", prompts)
        self.assertIn("deterministic PR watcher", prompts)
        self.assertIn("pull_request_feedback_invalid", prompts)
        for key in ("ci_prepare_failed", "ci_monitor_needs_user_action"):
            self.assertTrue({
                "task_short_id", "ci_report", "ci_contract",
            } <= {parameter.key for parameter in by_key[key].parameters}, key)
        self.assertEqual(
            tuple(parameter.key for parameter in by_key["ci_monitor_fix"].parameters),
            ("workspace_path", "fix_context", "delivery_context"),
        )
        self.assertEqual(
            tuple(parameter.key for parameter in by_key["ci_monitor_watch"].parameters),
            ("workspace_path", "task_short_id", "delivery_context"),
        )
        self.assertEqual(
            tuple(parameter.key for parameter in by_key["fix_verify"].parameters),
            ("workspace_path", "review_context", "task_short_id", "delivery_context"),
        )
        self.assertEqual(
            tuple(parameter.key for parameter in by_key["fix_continue"].parameters),
            ("workspace_path", "fix_context", "task_short_id", "delivery_context"),
        )
        self.assertTrue(
            {"delivery_context"}
            <= {parameter.key for parameter in by_key["fix_needs_user_action"].parameters}
        )
        self.assertTrue(
            {"delivery_context"}
            <= {
                parameter.key
                for parameter in by_key["compliance_prepare_pr"].parameters
            }
        )

    def test_ci_disabled_schema_four_keeps_pr_tail_without_ci_packets(self) -> None:
        profile = self.load_schema4_profile(
            lambda _: self.schema4_runtime_v2_contents().replace(
                "ci_monitoring = true", "ci_monitoring = false",
            ).replace(', "wait_ci", "github_observation"]', ', "github_observation"]')
            .replace('wait_ci = "3.0.0"\n', '')
        )
        spec = build_delivery_workflow(profile, 1)
        self.assertNotIn("ci_prepare", {node.key for node in spec.nodes})
        self.assertNotIn("ci_watch", {node.key for node in spec.nodes})
        by_key = {edge.key: edge for edge in spec.edges}
        for edge in spec.edges:
            handwritten = (edge.prompt or "").split(
                "Completion fields from the selected graph contract ", 1
            )[0]
            self.assertNotIn("prepare_pr_monitor_ci", handwritten, edge.key)
            self.assertTrue(
                {"ci_contract", "ci_report"}.isdisjoint(
                    parameter.key for parameter in edge.parameters
                ),
                edge.key,
            )
        merge_watch = next(edge for edge in spec.edges if edge.target == "merge_watch")
        self.assertNotIn("ci_contract", {p.key for p in merge_watch.parameters})
        for key in (
            "waiting_pr_fix",
            "fix_needs_user_action",
            "fix_verify",
            "fix_continue",
            "compliance_prepare_pr",
        ):
            self.assertIn(
                "delivery_context",
                {parameter.key for parameter in by_key[key].parameters},
                key,
            )
            self.assertNotIn(
                "ci_contract",
                {parameter.key for parameter in by_key[key].parameters},
            )
            self.assertNotIn(
                "ci_report",
                {parameter.key for parameter in by_key[key].parameters},
            )

    def test_kit_lite_schema_three_preserves_feedback_cursor_through_fix_loop(
        self,
    ) -> None:
        profile = ProjectProfile.from_toml(
            REPO_ROOT,
            (REPO_ROOT / ".kent" / "workflow-profile.toml").read_text(),
            check_files=False,
        )
        self.assertEqual(profile.schema_version, 3)
        self.assertEqual(profile.delivery_profile, "lite")
        self.assertFalse(profile.runtime_contracts_v2())
        spec = build_delivery_workflow(profile, 3)
        by_key = {edge.key: edge for edge in spec.edges}
        context_edges = (
            "plan_contract_verify",
            "plan_contract_checked_verify",
            "fix_verify",
            "fix_needs_user_action",
            "dispatch_deterministic_verify",
            "dispatch_invalid_workspace",
            "dispatch_standards_review",
            "gate_fix",
            "gate_reverify_after_user_action",
            "gate_delivery_ready",
            "waiting_pr_fix",
        )
        for key in context_edges:
            self.assertIn(
                "delivery_context",
                {parameter.key for parameter in by_key[key].parameters},
                key,
            )
            self.assertNotIn(
                "pr_feedback_cursor",
                {parameter.key for parameter in by_key[key].parameters},
                key,
            )
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["deterministic_report_join"].parameters
            ),
            ("verification_status", "verification_report"),
        )
        flat_observation_edges = (
            "prepare_pr_waiting_pr",
            "waiting_pr_watch_merge",
            "merge_watch_still_waiting",
            "merge_watch_state_changed",
            "waiting_pr_needs_user_action",
        )
        self.assertEqual(
            tuple(
                parameter.key
                for parameter in by_key["merge_watch_cleanup"].parameters
            ),
            (
                "workspace_path",
                "pr_url",
                "branch_name",
                "merge_strategy",
                "merge_report",
                "pr_feedback_cursor",
                "pr_base_branch",
            ),
        )
        for key in flat_observation_edges:
            self.assertIn(
                "pr_feedback_cursor",
                {parameter.key for parameter in by_key[key].parameters},
                key,
            )
            self.assertNotIn(
                "delivery_context",
                {parameter.key for parameter in by_key[key].parameters},
                key,
            )
        for key in (
            *context_edges,
            *flat_observation_edges,
        ):
            self.assertNotIn(
                "ci_contract",
                {parameter.key for parameter in by_key[key].parameters},
            )
            self.assertNotIn(
                "ci_report",
                {parameter.key for parameter in by_key[key].parameters},
            )

    def test_schema_four_variants_remain_valid_without_legacy_publish(
        self,
    ) -> None:
        profile = self.load_schema4_profile()
        variant = replace(profile, release_topology="none")
        variant.validate(check_files=True)
        self.assertFalse(variant.package_publish_after_main())

        canary = build_canary_workflow(variant, 1)
        smoke_lab = build_smoke_lab_workflow(variant)

        canary.validate()
        smoke_lab.validate()
        variant_nodes = {node.key for node in canary.nodes}
        variant_nodes.update(node.key for node in smoke_lab.nodes)
        self.assertNotIn(
            "publish_package",
            variant_nodes,
        )

    def test_schema_four_rejects_legacy_release_topology(self) -> None:
        with self.assertRaisesRegex(
            SpecError,
            "schema 4 does not support release_topology",
        ):
            self.load_schema4_profile(
                lambda contents: contents.replace(
                    "schema_version = 4\n",
                    'schema_version = 4\nrelease_topology = "none"\n',
                )
            )

    def test_schema_four_requires_unique_managed_commands(self) -> None:
        with self.assertRaisesRegex(
            SpecError,
            "kit_managed_commands must not contain duplicates",
        ):
            self.load_schema4_profile(
                lambda contents: contents.replace(
                    'kit_managed_commands = ["dispatch"]',
                    'kit_managed_commands = ["dispatch", "dispatch"]',
                )
            )

        with self.assertRaisesRegex(
            SpecError,
            "must have a non-empty path in commands",
        ):
            self.load_schema4_profile(
                lambda contents: contents.replace(
                    'dispatch = ".kent/scripts/workflow-verification-dispatch"',
                    'dispatch = ""',
                )
            )

        with self.assertRaisesRegex(
            SpecError,
            "must have a non-empty path in commands",
        ):
            self.load_schema4_profile(
                lambda contents: contents.replace(
                    'dispatch = ".kent/scripts/workflow-verification-dispatch"\n',
                    "",
                )
            )

    def test_schema_four_requires_exact_valid_command_versions(self) -> None:
        invalid_versions = {
            "missing": lambda contents: contents.replace(
                'dispatch = "1.0.0"\n',
                "",
            ),
            "extra": lambda contents: contents.replace(
                'dispatch = "1.0.0"\n',
                'dispatch = "1.0.0"\nextra = "1.0.0"\n',
            ),
            "invalid": lambda contents: contents.replace(
                'dispatch = "1.0.0"',
                'dispatch = "1.2"',
            ),
        }
        for name, transform in invalid_versions.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    SpecError,
                    "command_versions",
                ):
                    self.load_schema4_profile(transform)

    def test_schema_four_closes_release_table_and_validates_topology_pairs(
        self,
    ) -> None:
        with self.assertRaisesRegex(SpecError, "release must declare exactly"):
            self.load_schema4_profile(
                lambda contents: contents.replace(
                    'snapshot_path = ".kent/release/snapshot.toml"\n',
                    'snapshot_path = ".kent/release/snapshot.toml"\nextra = "nope"\n',
                )
            )

        for topology_kind in (
            "unknown",
        ):
            with self.subTest(topology_kind=topology_kind):
                with self.assertRaisesRegex(
                    SpecError,
                    "unsupported release.topology_kind|requires adoption_mode",
                ):
                    self.load_schema4_profile(
                        lambda contents, topology_kind=topology_kind: contents.replace(
                            'topology_kind = "appsome-release-publication"',
                            f'topology_kind = "{topology_kind}"',
                        ).replace(
                            'adoption_mode = "managed-in-place"',
                            'adoption_mode = "metadata-only"',
                        )
                    )

        with self.assertRaisesRegex(
            SpecError,
            (
                r"\Arelease\.topology_kind 'appsome-release-publication' "
                r"requires adoption_mode 'managed-in-place'\Z"
            ),
        ):
            self.load_schema4_profile(
                lambda contents: contents.replace(
                    'adoption_mode = "managed-in-place"',
                    'adoption_mode = "metadata-only"',
                )
            )

        with self.assertRaisesRegex(
            SpecError,
            (
                r"\Arelease\.topology_kind "
                r"'sdk-merged-main-publication' requires adoption_mode one of "
                r"\('managed-in-place', 'metadata-only'\)\Z"
            ),
        ):
            self.load_schema4_profile(
                lambda contents: contents.replace(
                    'topology_kind = "appsome-release-publication"',
                    'topology_kind = "sdk-merged-main-publication"',
                ).replace(
                    'adoption_mode = "managed-in-place"',
                    'adoption_mode = "unsupported"',
                )
            )

    def test_schema_four_accepts_all_approved_topology_adoption_pairs(
        self,
    ) -> None:
        pairs = (
            ("appsome-release-publication", "managed-in-place", ".kent/release/build.sh"),
            ("puber-release", "managed-in-place", ".kent/release/build.sh"),
            ("sdk-merged-main-publication", "managed-in-place", ".kent/release/build.sh"),
            ("sdk-merged-main-publication", "metadata-only", ""),
            ("slack-reader-release", "managed-in-place", ".kent/release/build.sh"),
        )
        for topology_kind, adoption_mode, builder_path in pairs:
            with self.subTest(topology_kind=topology_kind):
                def transform(contents: str) -> str:
                    return contents.replace(
                        'topology_kind = "appsome-release-publication"',
                        f'topology_kind = "{topology_kind}"',
                    ).replace(
                        'adoption_mode = "managed-in-place"',
                        f'adoption_mode = "{adoption_mode}"',
                    ).replace(
                        'builder_path = ".kent/release/build.sh"',
                        f'builder_path = "{builder_path}"',
                    )

                profile = self.load_schema4_profile(transform)
                self.assertEqual(profile.release.topology_kind, topology_kind)
                self.assertEqual(profile.release.adoption_mode, adoption_mode)
                self.assertEqual(profile.release.builder_path, builder_path)
                self.assertFalse(profile.package_publish_after_main())

    def test_schema_four_enforces_builder_rules(self) -> None:
        for topology_kind in (
            "appsome-release-publication",
            "sdk-merged-main-publication",
        ):
            with self.subTest(topology_kind=topology_kind):
                with self.assertRaisesRegex(
                    SpecError,
                    "builder_path is required for managed-in-place",
                ):
                    self.load_schema4_profile(
                        lambda contents, topology_kind=topology_kind: contents.replace(
                            'topology_kind = "appsome-release-publication"',
                            f'topology_kind = "{topology_kind}"',
                        ).replace(
                            'builder_path = ".kent/release/build.sh"',
                            'builder_path = ""',
                        )
                )

        profile = self.load_schema4_profile(
            lambda contents: contents.replace(
                'topology_kind = "appsome-release-publication"',
                'topology_kind = "sdk-merged-main-publication"',
            ).replace(
                'adoption_mode = "managed-in-place"',
                'adoption_mode = "metadata-only"',
            ).replace(
                'builder_path = ".kent/release/build.sh"',
                'builder_path = ""',
            )
        )
        self.assertEqual(profile.release.builder_path, "")

        with self.assertRaisesRegex(
            SpecError,
            "builder_path must be empty for metadata-only",
        ):
            self.load_schema4_profile(
                lambda contents: contents.replace(
                    'topology_kind = "appsome-release-publication"',
                    'topology_kind = "sdk-merged-main-publication"',
                ).replace(
                    'adoption_mode = "managed-in-place"',
                    'adoption_mode = "metadata-only"',
                )
            )

    def test_schema_four_rejects_path_escapes_and_target_aliases(self) -> None:
        for field, replacement in (
            (
                "release",
                (
                    'spec_path = ".kent/release/spec.toml"',
                    'spec_path = "../release/spec.toml"',
                ),
            ),
            (
                "command",
                (
                    'dispatch = ".kent/scripts/workflow-verification-dispatch"',
                    'dispatch = "../scripts/dispatch"',
                ),
            ),
            (
                "adapter",
                (
                    "[release]\n",
                    '[adapters]\nlock = "../adapters/lock"\n\n[release]\n',
                ),
            ),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    SpecError,
                    "normalized project-relative path",
                ):
                    self.load_schema4_profile(
                        lambda contents, replacement=replacement: contents.replace(
                            *replacement
                        )
                    )

        with self.assertRaisesRegex(SpecError, "targets must be unique"):
            self.load_schema4_profile(
                lambda contents: contents.replace(
                    "\n[release]\n",
                    (
                        "\n[adapters]\n"
                        'project_adapter = ".kent/scripts/workflow-verification-dispatch"\n'
                        "\n[release]\n"
                    ),
                )
            )

    def test_schema_four_release_files_are_required_and_symlink_safe(self) -> None:
        profile = self.load_schema4_profile()
        spec_path = profile.resolve_project_path(
            profile.release.spec_path,
            "release.spec_path",
        )
        spec_path.unlink()
        with self.assertRaisesRegex(SpecError, "release spec path not found"):
            ProjectProfile.load(profile.project_root)

        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary) / "outside-release-spec.toml"
            outside.write_text("# outside\n")
            spec_path.symlink_to(outside)
            with self.assertRaisesRegex(SpecError, "release.spec_path.*symlinks"):
                ProjectProfile.load(profile.project_root)

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

    def test_sync_project_memory_materializes_schema_three_source_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / ".kent").mkdir()
            memory_root = root.parent / f"{root.name}-R2"
            profile = self.project_memory_profile_contents(memory_root)
            (root / ".kent/workflow-profile.toml").write_text(profile)
            create_work_kind_procedures(root)

            result = self.run_sync_project_adapters(root)

            self.assertEqual(result.returncode, 0, result.stderr)
            adapter = root / ".kent/scripts/workflow-project-memory"
            core = root / ".kent/scripts/workflow_project_memory.py"
            self.assertEqual(
                adapter.read_bytes(),
                (REPO_ROOT / "templates/project/workflow-project-memory").read_bytes(),
            )
            self.assertEqual(
                core.read_bytes(),
                (REPO_ROOT / "workflowkit/project_memory.py").read_bytes(),
            )
            self.assertTrue(adapter.stat().st_mode & 0o111)
            self.assertTrue(core.stat().st_mode & 0o111)
            self.assertFalse(memory_root.exists())
            synchronized = {
                item["command"]
                for item in json.loads(result.stdout)["commands"]
            }
            self.assertTrue(
                {"project_memory", "project_memory_core"} <= synchronized
            )

    def test_schema_three_project_memory_pair_preflights_before_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / ".kent").mkdir()
            (root / ".kent/scripts").mkdir()
            memory_root = root.parent / f"{root.name}-R2"
            profile = self.project_memory_profile_contents(memory_root)
            (root / ".kent/workflow-profile.toml").write_text(profile)
            create_work_kind_procedures(root)
            foreign_core = root / "foreign-core.py"
            foreign_core.write_text("# foreign target\n")
            core_target = root / ".kent/scripts/workflow_project_memory.py"
            core_target.symlink_to(foreign_core)

            result = self.run_sync_project_adapters(root)

            self.assertEqual(result.returncode, 1)
            self.assertIn("symlink", result.stderr)
            self.assertFalse(
                (root / ".kent/scripts/workflow-project-memory").exists()
            )
            self.assertEqual(core_target.resolve(), foreign_core)
            self.assertFalse(memory_root.exists())

    def test_sync_project_memory_materializes_schema_four_versioned_pair(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            profile_contents = self.project_memory_profile_contents(
                root.parent / f"{root.name}-R2",
                schema=4,
            )
            project, profile = self.create_schema4_sync_project(
                lambda _: profile_contents,
            )

            result = self.run_sync_project_adapters(project)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                profile.command_versions,
                {
                    "dispatch": "1.0.0",
                    "project_memory": "1.0.0",
                    "project_memory_core": "1.0.0",
                },
            )
            for relative, source in (
                (
                    ".kent/scripts/workflow-project-memory",
                    REPO_ROOT / "templates/project/workflow-project-memory",
                ),
                (
                    ".kent/scripts/workflow_project_memory.py",
                    REPO_ROOT / "workflowkit/project_memory.py",
                ),
            ):
                target = project / relative
                self.assertEqual(target.read_bytes(), source.read_bytes())
                self.assertTrue(target.stat().st_mode & 0o111)
            self.assertFalse(
                (root.parent / f"{root.name}-R2/appsome").exists()
            )

    def test_installer_links_the_qualified_project_memory_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            kit = root / "kit"
            (kit / "scripts").mkdir(parents=True)
            generated = kit / ".kent/scripts"
            generated.mkdir(parents=True)
            installer = kit / "scripts/install"
            shutil.copyfile(REPO_ROOT / "scripts/install", installer)
            adapter = generated / "workflow-project-memory"
            core = generated / "workflow_project_memory.py"
            adapter.write_text("#!/usr/bin/env python3\n")
            adapter.chmod(0o755)
            core.write_text("# qualified sibling core\n")

            persistence = root / "kent"
            result = subprocess.run(
                ["bash", str(installer)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    **os.environ,
                    "HOME": str(root / "home"),
                    "KENT_PERSISTENCE_ROOT": str(persistence),
                },
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            link = persistence / "bin/kent-project-memory"
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), str(adapter))

            incomplete = root / "incomplete"
            (incomplete / "scripts").mkdir(parents=True)
            incomplete_generated = incomplete / ".kent/scripts"
            incomplete_generated.mkdir(parents=True)
            incomplete_installer = incomplete / "scripts/install"
            shutil.copyfile(REPO_ROOT / "scripts/install", incomplete_installer)
            invalid_adapter = incomplete_generated / "workflow-project-memory"
            invalid_adapter.write_text("#!/usr/bin/env python3\n")
            invalid_adapter.chmod(0o755)
            blocked_root = root / "blocked-kent"
            blocked = subprocess.run(
                ["bash", str(incomplete_installer)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    **os.environ,
                    "HOME": str(root / "home"),
                    "KENT_PERSISTENCE_ROOT": str(blocked_root),
                },
                check=False,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("sibling", blocked.stderr)
            self.assertFalse(blocked_root.exists())

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

    def test_sync_schema4_managed_create_update_and_exact_registry_version(
        self,
    ) -> None:
        root, profile = self.create_schema4_sync_project(
            schema4_with_managed_adapter
        )
        result = self.run_sync_project_adapters(root)

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        dispatch = root / profile.command("dispatch")
        adapter = root / profile.adapter("mobile_resource_lock")
        self.assertTrue(dispatch.is_file())
        self.assertTrue(adapter.is_file())
        self.assertEqual(profile.command_versions["dispatch"], "1.0.0")
        self.assertEqual(
            next(
                item
                for item in payload["commands"]
                if item["command"] == "dispatch"
            )["status"],
            "created",
        )
        self.assertEqual(
            next(
                item
                for item in payload["commands"]
                if item["command"] == "verify"
            )["status"],
            "project-owned",
        )

        dispatch.write_text("foreign command\n")
        adapter.write_text("foreign adapter\n")
        updated = self.run_sync_project_adapters(root, update=True)

        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertEqual(
            dispatch.read_bytes(),
            (
                REPO_ROOT
                / "templates"
                / "project"
                / "workflow-verification-dispatch"
            ).read_bytes(),
        )
        self.assertEqual(
            adapter.read_bytes(),
            (
                REPO_ROOT
                / "templates"
                / "project"
                / "emulator-resource-lock.sh"
            ).read_bytes(),
        )


    def test_sync_schema4_runtime_v2_copies_byte_identical_support_module(
        self,
    ) -> None:
        contents = self.schema4_runtime_v2_contents()
        for old, new in (
            ('runtime_contracts = "2.0.0"', 'runtime_contracts = "2.2.0"'),
            ('evidence = "2.0.0"', 'evidence = "2.1.0"'),
            ('janitor = "2.0.0"', 'janitor = "3.1.0"'),
            ('wait_pr = "3.0.0"', 'wait_pr = "4.1.0"'),
            ('wait_ci = "3.0.0"', 'wait_ci = "4.1.0"'),
            ('github_observation = "1.0.0"', 'github_observation = "1.1.0"'),
        ):
            contents = contents.replace(old, new)
        root, profile = self.create_schema4_sync_project(
            lambda _: contents
        )
        result = self.run_sync_project_adapters(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        support = root / profile.command("runtime_contracts")
        self.assertEqual(
            support.read_bytes(),
            (REPO_ROOT / "workflowkit" / "runtime.py").read_bytes(),
        )
        self.assertEqual(support.stat().st_mode & 0o777, 0o755)
        payload = json.loads(result.stdout)
        self.assertEqual(
            {
                item["command"] for item in payload["commands"]
                if item["status"] == "created"
            },
            set(profile.kit_managed_commands),
        )

    def test_sync_schema4_runtime_v2_1_preserves_previous_command_sources(
        self,
    ) -> None:
        contents = self.schema4_runtime_v2_contents()
        for old, new in (
            ('runtime_contracts = "2.0.0"', 'runtime_contracts = "2.1.0"'),
            ('evidence = "2.0.0"', 'evidence = "2.1.0"'),
            ('janitor = "2.0.0"', 'janitor = "3.0.0"'),
            ('wait_pr = "3.0.0"', 'wait_pr = "4.0.0"'),
            ('wait_ci = "3.0.0"', 'wait_ci = "4.0.0"'),
        ):
            contents = contents.replace(old, new)
        root, profile = self.create_schema4_sync_project(
            lambda _: contents,
        )
        expected = {}
        for key in profile.kit_managed_commands:
            target = root / profile.command(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"pinned runtime 2.1 source: {key}\n")
            target.chmod(0o755)
            expected[key] = target.read_bytes()

        result = self.run_sync_project_adapters(root, update=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            {
                item["command"] for item in payload["commands"]
                if item["status"] == "project-owned"
            } & set(profile.kit_managed_commands),
            set(profile.kit_managed_commands),
        )
        for key, content in expected.items():
            self.assertEqual((root / profile.command(key)).read_bytes(), content)

    def test_sync_schema4_version_mismatch_does_not_write(self) -> None:
        root, profile = self.create_schema4_sync_project(
            lambda contents: contents.replace(
                'dispatch = "1.0.0"',
                'dispatch = "1.1.0"',
            )
        )
        result = self.run_sync_project_adapters(root, update=True)

        self.assertEqual(result.returncode, 1)
        self.assertIn("expected toolkit template version", result.stderr)
        self.assertFalse((root / profile.command("dispatch")).exists())

    def test_sync_schema4_unknown_managed_template_does_not_write(self) -> None:
        def transform(contents: str) -> str:
            return contents.replace(
                'kit_managed_commands = ["dispatch"]',
                'kit_managed_commands = ["unknown"]',
            ).replace(
                'dispatch = "1.0.0"',
                'unknown = "1.0.0"',
            ).replace(
                "[commands]\n",
                '[commands]\nunknown = ".kent/scripts/unknown"\n',
            )

        root, profile = self.create_schema4_sync_project(transform)
        result = self.run_sync_project_adapters(root, update=True)

        self.assertEqual(result.returncode, 1)
        self.assertIn("no toolkit template exists", result.stderr)
        self.assertFalse((root / profile.command("unknown")).exists())

    def test_sync_schema4_preserves_known_template_project_owned_command(
        self,
    ) -> None:
        root, profile = self.create_schema4_sync_project()
        initial = self.run_sync_project_adapters(root)
        self.assertEqual(initial.returncode, 0, initial.stderr)
        verify = root / profile.command("verify")
        verify.write_text("project-owned verify\n")
        verify_before = verify.read_bytes()
        dispatch = root / profile.command("dispatch")
        dispatch.write_text("foreign dispatch\n")

        result = self.run_sync_project_adapters(root, update=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(verify.read_bytes(), verify_before)
        payload = json.loads(result.stdout)
        self.assertEqual(
            next(
                item
                for item in payload["commands"]
                if item["command"] == "verify"
            )["status"],
            "project-owned",
        )

    def test_sync_schema4_rejects_symlink_and_non_executable_project_command(
        self,
    ) -> None:
        for mode in ("symlink", "non-executable"):
            with self.subTest(mode=mode):
                root, profile = self.create_schema4_sync_project()
                verify = root / profile.command("verify")
                if mode == "symlink":
                    outside = root.parent / f"{root.name}-outside-command"
                    outside.write_text("outside\n")
                    outside.chmod(0o755)
                    self.addCleanup(outside.unlink)
                    verify.unlink()
                    verify.symlink_to(outside)
                else:
                    verify.chmod(0o644)

                result = self.run_sync_project_adapters(root, update=True)

                self.assertEqual(result.returncode, 1)
                if mode == "symlink":
                    self.assertIn("symlink", result.stderr)
                else:
                    self.assertIn("project-owned command", result.stderr)
                self.assertFalse((root / profile.command("dispatch")).exists())

    def test_sync_schema4_late_preflight_failure_writes_nothing(self) -> None:
        def transform(contents: str) -> str:
            return schema4_with_managed_adapter(
                contents.replace(
                    "[commands]\n",
                    (
                        "[commands]\n"
                        'zz_project_owned = ".kent/scripts/zz-project-owned"\n'
                    ),
                )
            )

        root, profile = self.create_schema4_sync_project(transform)
        late_target = root / profile.command("zz_project_owned")
        late_target.chmod(0o644)
        result = self.run_sync_project_adapters(root, update=True)

        self.assertEqual(result.returncode, 1)
        self.assertIn("project-owned command", result.stderr)
        self.assertFalse((root / profile.command("dispatch")).exists())

    def test_sync_schema3_late_adapter_failure_writes_nothing(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / ".kent").mkdir()
                contents = schema4_with_managed_adapter(EXAMPLE_PROFILE.read_text())
                contents += 'zz_project_owned = ".kent/adapters/zz-owned"\n'
                (root / ".kent/workflow-profile.toml").write_text(contents)
                create_work_kind_procedures(root)
                managed = root / ".kent/adapters/mobile/emulator-resource-lock.sh"
                if existing:
                    managed.parent.mkdir(parents=True)
                    managed.write_bytes(b"existing managed adapter\n")
                    managed.chmod(0o755)
                before = managed.read_bytes() if existing else None
                foreign = root / ".kent/adapters/zz-owned"
                foreign.parent.mkdir(parents=True, exist_ok=True)
                foreign.write_bytes(b"non-executable project adapter\n")
                foreign.chmod(0o644)

                result = self.run_sync_project_adapters(root, update=True)

                self.assertEqual(result.returncode, 1)
                self.assertIn("project-owned adapter is not executable", result.stderr)
                self.assertEqual(managed.exists(), existing)
                if existing:
                    self.assertEqual(managed.read_bytes(), before)
                self.assertEqual(foreign.read_bytes(), b"non-executable project adapter\n")
                self.assertFalse((root / ".kent/scripts").exists())

    def test_sync_schema4_managed_ancestor_failure_writes_nothing(self) -> None:
        def transform(contents: str) -> str:
            return schema4_with_managed_adapter(
                contents.replace(
                    'kit_managed_commands = ["dispatch"]',
                    'kit_managed_commands = ["dispatch", "wait_pr"]',
                )
                .replace(
                    'dispatch = "1.0.0"\n',
                    'dispatch = "1.0.0"\nwait_pr = "4.1.0"\n',
                )
                .replace(
                    'wait_pr = ".kent/scripts/workflow-wait-github-pr"',
                    'wait_pr = ".kent/scripts/late/wait-pr"',
                )
            )

        root, profile = self.create_schema4_sync_project(transform)
        dispatch = root / profile.command("dispatch")
        dispatch.write_text("foreign dispatch\n")
        dispatch_before = dispatch.read_bytes()
        late_parent = root / ".kent/scripts/late"
        late_parent.rmdir()
        late_parent.write_text("not a directory\n")

        result = self.run_sync_project_adapters(root, update=True)

        self.assertEqual(result.returncode, 1)
        self.assertIn("non-directory component", result.stderr)
        self.assertEqual(dispatch.read_bytes(), dispatch_before)
        self.assertFalse((root / profile.command("wait_pr")).exists())
        self.assertFalse((root / profile.adapter("mobile_resource_lock")).exists())

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
                'minimum_kent_version = "2.6.1"',
                'minimum_kent_version = "2.7.0"',
            )
        )
        self.assertEqual(profile.minimum_version_tuple(), (2, 7, 0))

    def test_profile_rejects_older_minimum_kent_version(self) -> None:
        with self.assertRaisesRegex(SpecError, "2.6.1 or newer"):
            self.load_profile(
                lambda contents: contents.replace(
                    'minimum_kent_version = "2.6.1"',
                    'minimum_kent_version = "2.6.0"',
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


    def test_apply_rejects_graph_mutation_for_linked_taskless_workflow(self) -> None:
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
                "id": "11111111-1111-4111-8111-111111111111",
                "name": "Minimal v1",
                "description": "Minimal workflow.",
                "execution_target_policy": {"mode": "head"},
            },
        }
        client = KentClient(Path(temporary.name))
        client.require_version = lambda *version: None
        client.inspect = lambda workflow: definition
        client.inspect_graph = lambda workflow: {
            "workflow_id": definition["workflow"]["id"],
            "expected_version": 1,
            "graph": {
                "node_groups": [],
                "nodes": [
                    {
                        "id": "node-backlog",
                        "key": "backlog",
                        "kind": "start",
                        "display_name": "Wrong",
                        "group_id": None,
                    },
                    {
                        "id": "node-done",
                        "key": "done",
                        "kind": "terminal",
                        "display_name": "Done",
                        "group_id": None,
                    },
                ],
                "transition_groups": [],
                "edges": [],
            },
        }
        client.workflow_has_tasks = lambda current: False
        client.workflow_is_linked = lambda current: True

        with self.assertRaisesRegex(
            SpecError,
            "linked to a project; this client does not support linked semantic updates"
            r"\. Use a separately approved lifecycle operation",
        ):
            client.apply(spec)

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
        client.inspect_graph = lambda workflow: {
            "workflow_id": definition["workflow"]["id"],
            "expected_version": 1,
            "graph": {
                "node_groups": [],
                "nodes": definition["nodes"],
                "transition_groups": [],
                "edges": [],
            },
        }
        client.workflow_has_tasks = lambda current: True
        client.run_json = lambda args: commands.append(args) or {}

        with self.assertRaisesRegex(
            SpecError,
            "this client does not support task-referenced semantic updates"
            r"\. Use a separately approved lifecycle operation",
        ):
            client.apply(spec)
        self.assertEqual(commands, [])

    def test_native_edge_prompt_projection_matches_go_whitespace_before_diff(self) -> None:
        whitespace = (
            "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
            "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
        )
        spec = WorkflowSpec(
            name="Native whitespace", description="Projection fixture.", execution_target="head",
            nodes=(NodeSpec("start", "start", "Start"), NodeSpec("end", "terminal", "End")),
            edges=(EdgeSpec("finish", "start", "finish", "end",
                            prompt=whitespace + "\x1c interior \n whitespace \x1f" + whitespace),),
        )
        inspected = {
            "workflow_id": "11111111-1111-4111-8111-111111111111", "expected_version": 1,
            "graph": {key: [] for key in ("nodes", "edges", "node_groups", "transition_groups")},
        }
        plan = plan_workflow_graph(spec, inspected, metadata_changed=False)
        self.assertEqual(
            plan.document["graph"]["edges"][0]["prompt_template"],
            "\x1c interior \n whitespace \x1f",
        )
        second = plan_workflow_graph(spec, plan.document, metadata_changed=False)
        self.assertFalse(second.graph_changed)

    def test_shared_native_projection_is_narrow_nonmutating_and_idempotent(self) -> None:
        from workflowkit.graph import native_graph_projection

        graph = {
            "version": 17, "description": " Keep metadata.\n",
            "nodes": [{"id": "node-1", "prompt_template": " Keep node instructions.\n"}],
            "transition_groups": [{"description": " Keep transition description.\n"}],
            "edges": [
                {"id": "edge-1", "prompt_template": "\nPrompt with\n internal \t whitespace.\n",
                 "future": {"prompt_template": " Keep unknown data.\n"}},
                {"id": "edge-2", "prompt_template": None},
                {"id": "edge-3", "prompt": " Keep other field.\n"},
                {"id": "edge-4", "prompt_template": " \t\n"},
            ],
        }
        original = deepcopy(graph)
        projected = native_graph_projection(graph)
        self.assertEqual(graph, original)
        expected = deepcopy(graph)
        expected["edges"][0]["prompt_template"] = "Prompt with\n internal \t whitespace."
        expected["edges"][3]["prompt_template"] = ""
        self.assertEqual(projected, expected)
        self.assertEqual(native_graph_projection(projected), projected)
        projected["edges"][0]["future"]["prompt_template"] = "Changed copy only"
        self.assertEqual(graph, original)
        for character in "\x1c\x1d\x1e\x1f\u180e\u200b\ufeff":
            text = character + " content\n " + character
            self.assertEqual(
                native_graph_projection({"edges": [{"prompt_template": text}]})["edges"][0]["prompt_template"],
                text,
            )

    def test_graph_projection_preserves_ids_order_and_unknown_fields(self) -> None:
        spec = WorkflowSpec(
            name="Minimal v1",
            description="Minimal.",
            execution_target="head",
            nodes=(
                NodeSpec("backlog", "start", "Backlog"),
                NodeSpec(
                    "implement",
                    "agent",
                    "Implement",
                    agent="default",
                    completion_mode="shell_command",
                ),
                NodeSpec("done", "terminal", "Done"),
            ),
            edges=(
                EdgeSpec(
                    "start_implement",
                    "backlog",
                    "start",
                    "implement",
                    prompt="Implement.",
                    transition_description="Start.",
                ),
                EdgeSpec(
                    "implement_done",
                    "implement",
                    "finish",
                    "done",
                    transition_description="Finish.",
                ),
            ),
        )
        graph = {
            "workflow_id": "11111111-1111-4111-8111-111111111111",
            "expected_version": 7,
            "future_top_level": "keep",
            "graph": {
                "node_groups": [],
                "future_graph": {"keep": True},
                "nodes": [
                    {
                        "id": "node-backlog",
                        "key": "backlog",
                        "kind": "start",
                        "display_name": "Backlog",
                        "group_id": None,
                        "future_node": "keep",
                    },
                    {
                        "id": "node-done",
                        "key": "done",
                        "kind": "terminal",
                        "display_name": "Done",
                        "group_id": None,
                    },
                ],
                "transition_groups": [],
                "edges": [],
            },
        }

        plan = plan_workflow_graph(spec, graph, metadata_changed=False)

        self.assertTrue(plan.graph_changed)
        self.assertEqual(
            [node["key"] for node in plan.document["graph"]["nodes"]],
            ["backlog", "done", "implement"],
        )
        self.assertEqual(plan.document["graph"]["nodes"][0]["id"], "node-backlog")
        self.assertEqual(
            plan.document["graph"]["nodes"][0]["future_node"],
            "keep",
        )
        self.assertEqual(plan.document["future_top_level"], "keep")
        self.assertEqual(plan.document["graph"]["future_graph"], {"keep": True})

    def test_native_graph_match_rejects_semantic_drift_and_extra_nodes(self) -> None:
        spec = WorkflowSpec(
            name="Graph equality",
            description="Native graph comparison.",
            execution_target="head",
            nodes=(
                NodeSpec("backlog", "start", "Backlog"),
                NodeSpec("done", "terminal", "Done"),
            ),
            edges=(
                EdgeSpec("finish", "backlog", "finish", "done", prompt="Finish."),
            ),
        )
        empty = {
            "workflow_id": "11111111-1111-4111-8111-111111111111",
            "expected_version": 1,
            "graph": {key: [] for key in ("nodes", "edges", "transition_groups", "node_groups")},
        }
        native = plan_workflow_graph(spec, empty, metadata_changed=False).document
        self.assertTrue(graph_matches_spec(spec, native))
        for drift in ("node_name", "edge_prompt", "extra_node"):
            with self.subTest(drift=drift):
                changed = deepcopy(native)
                graph = changed["graph"]
                if drift == "node_name":
                    graph["nodes"][0]["display_name"] = "Wrong"
                elif drift == "edge_prompt":
                    graph["edges"][0]["prompt_template"] = "Different instruction."
                else:
                    graph["nodes"].append({
                        "id": "foreign-node", "key": "foreign",
                        "kind": "terminal", "display_name": "Foreign",
                    })
                self.assertFalse(graph_matches_spec(spec, changed))
                repair = plan_workflow_graph(spec, changed, metadata_changed=False)
                self.assertTrue(repair.graph_changed)
                self.assertTrue(graph_matches_spec(spec, repair.document))

    def test_graph_projection_round_trips_selection_parameter_purposes(self) -> None:
        edge = EdgeSpec(
            "start_implement",
            "backlog",
            "start",
            "implement",
            prompt="Implement.",
            transition_description="Start.",
            parameters=(
                ParameterSpec("scope", "Scope."),
                ParameterSpec(
                    "target_role",
                    "Selected role.",
                    purpose="target_assignee",
                ),
                ParameterSpec(
                    "target_thinking",
                    "Selected thinking.",
                    purpose="target_thinking",
                ),
            ),
            assignee_selection="previous_node",
            thinking_selection="previous_node",
        )
        spec = WorkflowSpec(
            name="Selection v1",
            description="Selection.",
            execution_target="head",
            nodes=(
                NodeSpec("backlog", "start", "Backlog"),
                NodeSpec(
                    "implement",
                    "agent",
                    "Implement",
                    agent="default",
                    completion_mode="shell_command",
                ),
                NodeSpec("done", "terminal", "Done"),
            ),
            edges=(edge,),
        )
        graph = {
            "workflow_id": "11111111-1111-4111-8111-111111111111",
            "expected_version": 1,
            "graph": {
                "node_groups": [],
                "nodes": [],
                "transition_groups": [],
                "edges": [],
            },
        }

        projected = plan_workflow_graph(
            spec,
            graph,
            metadata_changed=False,
        ).document
        self.assertTrue(graph_matches_spec(spec, projected))
        projected_edge = projected["graph"]["edges"][0]
        self.assertEqual(projected_edge["assignee_selection"], "previous_node")
        self.assertEqual(projected_edge["thinking_selection"], "previous_node")
        self.assertEqual(
            [parameter["purpose"] for parameter in projected_edge["parameters"]],
            ["ordinary", "target_assignee", "target_thinking"],
        )

    def test_graph_projection_omits_empty_parameter_collection(self) -> None:
        spec = WorkflowSpec(
            name="Minimal v1",
            description="Minimal.",
            execution_target="head",
            nodes=(
                NodeSpec("backlog", "start", "Backlog"),
                NodeSpec("done", "terminal", "Done"),
            ),
            edges=(
                EdgeSpec(
                    "finish",
                    "backlog",
                    "finish",
                    "done",
                    transition_description="Finish.",
                ),
            ),
        )
        graph = {
            "workflow_id": "11111111-1111-4111-8111-111111111111",
            "expected_version": 1,
            "graph": {
                "node_groups": [],
                "nodes": [],
                "transition_groups": [],
                "edges": [],
            },
        }
        projected = plan_workflow_graph(
            spec,
            graph,
            metadata_changed=False,
        ).document
        self.assertNotIn("parameters", projected["graph"]["edges"][0])
        self.assertTrue(graph_matches_spec(spec, projected))

    def test_edge_selection_requires_exact_protected_parameters(self) -> None:
        with self.assertRaisesRegex(SpecError, "target_assignee parameter"):
            EdgeSpec(
                "select",
                "backlog",
                "start",
                "done",
                transition_description="Select.",
                assignee_selection="previous_node",
            ).validate()
        with self.assertRaisesRegex(SpecError, "repeats parameter purpose"):
            EdgeSpec(
                "select",
                "backlog",
                "start",
                "done",
                transition_description="Select.",
                parameters=(
                    ParameterSpec("one", "One.", "target_assignee"),
                    ParameterSpec("two", "Two.", "target_assignee"),
                ),
                assignee_selection="previous_node",
            ).validate()

    def test_graph_projection_rejects_node_groups(self) -> None:
        spec = WorkflowSpec(
            name="Minimal v1",
            description="Minimal.",
            execution_target="head",
            nodes=(
                NodeSpec("backlog", "start", "Backlog"),
                NodeSpec("done", "terminal", "Done"),
            ),
            edges=(),
        )
        graph = {
            "workflow_id": "11111111-1111-4111-8111-111111111111",
            "expected_version": 1,
            "graph": {
                "node_groups": [{"id": "group"}],
                "nodes": [],
                "transition_groups": [],
                "edges": [],
            },
        }
        with self.assertRaisesRegex(SpecError, "node_groups"):
            plan_workflow_graph(spec, graph, metadata_changed=False)

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

    def test_apply_delegates_explicit_typed_compatibility_plan_without_legacy_effects(self) -> None:
        import workflowkit.kent as kent_module

        plan_type = getattr(kent_module, "CanonicalWorkflowCompatibilityPlan", None)
        self.assertIsNotNone(plan_type)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            plan_path = root / "compatibility-plan.json"
            workflow_id = "11111111-1111-4111-8111-111111111111"
            digest = "a" * 64
            compatibility_plan = plan_type(
                path=plan_path,
                expected_sha256=digest,
                confirmation_sha256=digest,
            )
            spec = WorkflowSpec(
                name="Current delivery",
                description="Current compatible delivery.",
                execution_target="default-branch",
                nodes=(
                    NodeSpec("backlog", "start", "Backlog"),
                    NodeSpec("done", "terminal", "Done"),
                ),
                edges=(),
            )
            loaded = type(
                "LoadedCompatibilityPlan",
                (),
                {
                    "schema": "canonical-workflow-reconcile-plan-v2",
                    "sha256": digest,
                    "value": {
                        "schema": "canonical-workflow-reconcile-plan-v2",
                        "project_root": str(root),
                        "kent": {"path": sys.executable},
                        "workflows": [{"workflow_id": workflow_id}],
                    },
                },
            )()
            # Verifier TMPDIR may be inside another Git worktree; this fixture
            # explicitly models its own project, not the ancestor repository.
            client = KentClient(root, binary=sys.executable, project_workspace=root)
            client.inspect = lambda *args, **kwargs: self.fail(
                "compatibility delegation must not enter legacy KentClient apply"
            )
            client.run_json = lambda *args, **kwargs: self.fail(
                "compatibility delegation must not create, link, or default"
            )
            with (
                mock.patch(
                    "workflowkit.operations.load_plan",
                    return_value=loaded,
                ) as load_plan_mock,
                mock.patch(
                    "workflowkit.operations.reconcile_canonical_workflows",
                    side_effect=[
                        {"phase": "prepared"},
                        {"phase": "complete"},
                    ],
                ) as reconcile_mock,
            ):
                result = client.apply(
                    spec,
                    workflow_selector=workflow_id,
                    compatibility_plan=compatibility_plan,
                )
            self.assertEqual(result["phase"], "complete")
            load_plan_mock.assert_called_once()
            self.assertEqual(
                [call.kwargs["mode"] for call in reconcile_mock.call_args_list],
                ["prepare", "apply"],
            )
            self.assertTrue(
                all(
                    call.kwargs["expected_workflow_id"] == workflow_id
                    and call.kwargs["expected_spec"] is spec
                    for call in reconcile_mock.call_args_list
                )
            )

    def test_generate_workflow_exposes_exact_hash_compatibility_confirmation(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "generate-workflow"),
                "--help",
            ],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--compatibility-plan", result.stdout)
        self.assertIn("--compatibility-plan-sha256", result.stdout)
        self.assertIn("--confirm-compatibility-plan", result.stdout)

    def test_graph_apply_retries_only_confirmation_required_outcome(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        calls: list[tuple[list[str], str | None]] = []
        outcomes = iter(
            (
                subprocess.CompletedProcess(
                    [],
                    1,
                    stdout=json.dumps(
                        {
                            "outcome": "confirmation_required",
                            "impact": {"removed_node_count": 1},
                        }
                    ),
                    stderr="",
                ),
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps({"outcome": "saved"}),
                    stderr="",
                ),
            )
        )
        client = KentClient(Path(temporary.name))

        def run(
            args: list[str],
            *,
            check: bool = True,
            input_text: str | None = None,
        ) -> subprocess.CompletedProcess[str]:
            calls.append((args, input_text))
            result = next(outcomes)
            result.args = args
            return result

        client.run = run
        document = {
            "workflow_id": "11111111-1111-4111-8111-111111111111",
            "expected_version": 2,
            "graph": {
                "node_groups": [],
                "nodes": [],
                "transition_groups": [],
                "edges": [],
            },
        }

        outcome = client.apply_graph_document(
            document,
            confirm_destructive=True,
        )

        self.assertEqual(outcome["outcome"], "saved")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--confirm", calls[0][0])
        self.assertIn("--confirm", calls[1][0])
        self.assertEqual(calls[0][1], calls[1][1])

    def test_graph_apply_stale_version_fails_without_retry(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        calls: list[list[str]] = []
        client = KentClient(Path(temporary.name))

        def run(
            args: list[str],
            *,
            check: bool = True,
            input_text: str | None = None,
        ) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(
                args,
                1,
                stdout=json.dumps(
                    {
                        "outcome": "blocked",
                        "message": "expected version 2, current version 3",
                    }
                ),
                stderr="",
            )

        client.run = run
        with self.assertRaisesRegex(KentCommandError, "current version 3"):
            client.apply_graph_document(
                {
                    "workflow_id": "11111111-1111-4111-8111-111111111111",
                    "expected_version": 2,
                    "graph": {
                        "node_groups": [],
                        "nodes": [],
                        "transition_groups": [],
                        "edges": [],
                    },
                },
                confirm_destructive=True,
            )
        self.assertEqual(len(calls), 1)

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
        workflow_input: str | None = None,
    ) -> dict[str, object]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        scripts = root / ".kent" / "scripts"
        scripts.mkdir(parents=True)
        verifier = scripts / "workflow-compile-verify"
        verifier.write_text("#!/bin/sh\n" + verifier_body)
        verifier.chmod(0o755)
        shutil.copyfile(
            REPO_ROOT / "workflowkit" / "runtime.py",
            scripts / "workflow_runtime_contracts.py",
        )
        wrapper = scripts / "workflow-verify-report"
        shutil.copyfile(VERIFY_REPORT, wrapper)
        wrapper.chmod(0o755)
        (root / ".gitignore").write_text("/build/\nbuild/kent-workflow/*\n")
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "Kent Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "kent@example.invalid"],
            check=True,
        )
        (root / "tracked.txt").write_text("ready\n")
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-q", "-m", "Initial"],
            check=True,
        )
        effective_input = (
            workflow_input
            if workflow_input is not None
            else json.dumps({"workspace_path": str(root)})
        )
        result = subprocess.run(
            [str(wrapper)],
            cwd=root,
            env=os.environ.copy(),
            input=effective_input,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def parse_verification_report(
        self,
        payload: dict[str, object],
    ) -> dict[str, object]:
        report = json.loads(str(payload["verification_report"]))
        self.assertEqual(
            set(report),
            {"schema", "code", "log_path", "log_sha256", "exit_code"},
        )
        return report

    def test_passed_verifier_reports_passed_even_with_stderr_diagnostics(self) -> None:
        result = self.run_report(
            """echo "diagnostic" >&2
printf '%s\n' '{"transition":"passed"}'
"""
        )
        self.assertEqual(
            result["transition"],
            "deterministic_verify_reported",
        )
        self.assertEqual(result["verification_status"], "passed")
        self.assertEqual(
            self.parse_verification_report(result)["code"],
            "passed",
        )

    def test_code_failure_reports_needs_changes(self) -> None:
        result = self.run_report(
            """printf '%s\n' \
'{"transition":"failed"}'
"""
        )
        self.assertEqual(result["verification_status"], "needs_changes")
        self.assertEqual(
            self.parse_verification_report(result)["code"],
            "verification_failed",
        )

    def test_environment_failure_reports_blocked(self) -> None:
        result = self.run_report(
            """printf '%s\n' \
'{"transition":"blocked"}'
""",
        )
        self.assertEqual(result["verification_status"], "blocked")
        self.assertEqual(
            self.parse_verification_report(result)["code"],
            "verification_blocked",
        )

    def test_stderr_environment_failure_is_case_insensitive(self) -> None:
        result = self.run_report(
            """echo "permission denied while opening toolchain" >&2
printf '%s\n' \
'{"transition":"blocked"}'
"""
        )
        self.assertEqual(result["verification_status"], "blocked")
        self.assertEqual(
            self.parse_verification_report(result)["code"],
            "verification_blocked",
        )

    def test_nonzero_verifier_reports_needs_changes(self) -> None:
        result = self.run_report('echo "boom" >&2\nexit 2\n')
        self.assertEqual(
            result["transition"],
            "deterministic_verify_reported",
        )
        self.assertEqual(result["verification_status"], "needs_changes")
        self.assertEqual(
            self.parse_verification_report(result)["code"],
            "child_exit_nonzero",
        )

    def test_malformed_verifier_output_still_reports_blocked(self) -> None:
        result = self.run_report("echo not-json\n")
        self.assertEqual(result["verification_status"], "blocked")
        self.assertEqual(
            self.parse_verification_report(result)["code"],
            "child_output_invalid",
        )

    def test_invalid_workflow_input_still_reports_blocked(self) -> None:
        result = self.run_report("exit 99\n", workflow_input="not-json")
        self.assertEqual(result["verification_status"], "blocked")
        self.assertEqual(
            self.parse_verification_report(result)["code"],
            "input_invalid",
        )


if __name__ == "__main__":
    unittest.main()
