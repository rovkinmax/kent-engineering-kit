#!/usr/bin/env python3
"""Checkout-local schema-3 bootstrap and semantic Kit development graph."""
from __future__ import annotations

import argparse
from dataclasses import replace
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import json
from pathlib import Path
import stat
import sys


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workflowkit.delivery import (
    UNACTIVATED_SOURCE,
    _PR_TARGET_CARRIER_FIELDS,
    build_delivery_workflow,
)
from workflowkit.kent import spec_as_json
from workflowkit.model import SpecError, WorkflowSpec
from workflowkit.profile import ProjectProfile


CURRENT_SPEC_PATH = ".kent/workflows/kit-engineering-delivery.current.spec.json"
MEMORY_COPIES = {
    "project_memory": "templates/project/workflow-project-memory",
    "project_memory_core": "workflowkit/project_memory.py",
}
PROJECT_COPIES = {
    "evidence": "templates/project/workflow-evidence-ledger",
    "verify": "templates/project/workflow-verify-report",
    "wait_pr": "templates/project/workflow-wait-github-pr",
    "github_observation": "workflowkit/github_observation.py",
    "janitor": "templates/project/workflow-task-janitor",
    "runtime_contracts": "workflowkit/runtime.py",
}
SCHEMA3_COPIES = {
    "checkpoint": "templates/project/workflow-checkpoint",
    "plan_contract": "templates/project/workflow-plan-contract",
    "plan_contract_accept": "templates/project/workflow-plan-contract-accept",
    "plan_contract_continue": "templates/project/workflow-plan-contract-continue",
    "plan_contract_verify": "templates/project/workflow-plan-contract-verify",
    "plan_contract_fix_continue": (
        "templates/project/workflow-plan-contract-fix-continue"
    ),
    "dispatch": "templates/project/workflow-verification-dispatch",
}
CURRENT_COMMAND_TARGETS = {
    key: f".kent/scripts/{Path(source).name}"
    for key, source in (SCHEMA3_COPIES | PROJECT_COPIES).items()
} | {
    "github_observation": ".kent/scripts/workflow_github_observation.py",
    "runtime_contracts": ".kent/scripts/workflow_runtime_contracts.py",
    "compile_verify": ".kent/scripts/workflow-compile-verify",
    "prepare_cleanup": ".kent/scripts/workflow-prepare-cleanup",
    "project_memory": ".kent/scripts/workflow-project-memory",
    "project_memory_core": ".kent/scripts/workflow_project_memory.py",
}
PLAN_REVIEW_GUIDANCE = """
Read `.kent/commands/plan.md` for the project preview-review procedure.
Verify the first independent read-only PASS receipt: it must identify a
different reviewer Session and bind the exact preview SHA-256 now reviewed.
Perform the second independent review of that same preview hash. Revalidation
requires a refreshed first review and this separate second review.
Only after both PASS may plan_review_accept request human approval.
Include a concise Russian approval summary naming scope and effects; do not
request a third routine preview review or claim the graph hashes receipts.
"""


def _add_plan_approval(
    base: WorkflowSpec,
    *,
    expected_shape: tuple[int, int, int],
) -> WorkflowSpec:
    accepts = [edge for edge in base.edges if edge.key == "plan_review_accept"]
    if len(accepts) != 1 or accepts[0].requires_approval:
        raise SpecError("expected exactly one unapproved plan_review_accept edge")
    if (accepts[0].source, accepts[0].target) != ("plan_review", "plan_contract"):
        raise SpecError("unexpected preview approval boundary")
    edges = tuple(
        replace(
            edge,
            requires_approval=True,
        ) if edge.key == "plan_review_accept" else replace(
            edge, prompt=(edge.prompt or "") + PLAN_REVIEW_GUIDANCE,
        ) if edge.target == "plan_review" else edge
        for edge in base.edges
    )
    spec = replace(base, edges=edges)
    spec.validate()
    shape = (
        len(spec.nodes),
        len({(edge.source, edge.transition) for edge in spec.edges}),
        len(spec.edges),
    )
    if shape != expected_shape:
        raise SpecError(f"unexpected Kit lite graph shape: {shape}")
    return spec


def build_current_workflow(profile: ProjectProfile) -> WorkflowSpec:
    """Build the current Kit graph using the profile's coder-selection policy."""
    if profile.delivery_reliability_policy() != "v1":
        raise SpecError("the current Kit graph requires delivery_reliability = 'v1'")
    if profile.writer_session_policy() != "continuous":
        raise SpecError("the current Kit graph requires continuous writer Sessions")
    if profile.commands != CURRENT_COMMAND_TARGETS:
        raise SpecError("the current Kit graph requires the complete command set")
    complexity = profile.coder_selection_policy() == "complexity"
    delivery = build_delivery_workflow(profile, 6 if complexity else 5)
    target_carriers = [
        edge for edge in delivery.edges
        if edge.key != "prepare_pr_no_pr"
        and any(
            parameter.key in _PR_TARGET_CARRIER_FIELDS
            for parameter in edge.parameters
        )
    ]
    if not target_carriers or any(
        not any(parameter.key == "pr_base_branch" for parameter in edge.parameters)
        for edge in target_carriers
    ):
        raise SpecError("the current Kit graph requires exact PR target carriers")
    no_pr = next(
        (edge for edge in delivery.edges if edge.key == "prepare_pr_no_pr"),
        None,
    )
    if no_pr is None or any(
        parameter.key == "pr_base_branch" for parameter in no_pr.parameters
    ):
        raise SpecError("the current Kit no-PR route must omit PR target evidence")
    if any(
        edge.key in {
            "ci_monitor_retry_preparation",
            "ci_monitor_preparation_needs_user_action",
        }
        for edge in delivery.edges
    ):
        raise SpecError("the current Kit CI-off graph cannot declare source-CI recovery")
    return _add_plan_approval(
        delivery,
        expected_shape=(21, 60, 61) if complexity else (21, 59, 60),
    )


def generated_copies(profile: ProjectProfile) -> dict[str, str]:
    if profile.commands != CURRENT_COMMAND_TARGETS:
        raise SpecError("bootstrap requires the complete closed Kit command set")
    return SCHEMA3_COPIES | PROJECT_COPIES | MEMORY_COPIES


def build_published_current_workflow(profile: ProjectProfile, admission) -> WorkflowSpec:
    """Render from a trusted Kit caller's verified admission; perform no effects."""
    from workflowkit.operations import VerifiedSourceAdmission

    if not isinstance(admission, VerifiedSourceAdmission):
        raise SpecError("published rendering requires VerifiedSourceAdmission")
    if admission.value.get("schema") == "delivery-source-admission-v2" and (
        admission.value.get("binding", {}).get("kind") != "r2-installed"
    ):
        raise SpecError("the R2-required Kit final graph needs r2-installed admission")
    if (Path(admission.project_root) != profile.project_root.resolve()
            or admission.project_id != "project-135413f9-2454-4d04-969e-3831b1527f18"
            or admission.workflow_id != "4e7cea43-54f5-4662-8d5d-c8db3d816c74"
            or Path(admission.kit_root) != ROOT.resolve()):
        raise SpecError("verified admission belongs to a different Kit deployment")
    spec = build_current_workflow(profile)
    ingress = [edge for edge in spec.edges if edge.source == "backlog" and edge.target == "plan"]
    if len(ingress) != 1:
        raise SpecError("published Kit graph requires one Backlog to Plan ingress")
    return replace(spec, edges=tuple(
        replace(edge, prompt=admission.gate + "\n\n" + (edge.prompt or ""))
        if edge is ingress[0] else edge for edge in spec.edges
    ))


def load_synchronizer():
    loader = SourceFileLoader(
        "kit_project_adapter_sync", str(ROOT / "scripts/sync-project-adapters"),
    )
    spec = spec_from_loader(loader.name, loader)
    if spec is None:
        raise SpecError("cannot load checkout-local synchronizer")
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


def profile_at(root: Path, *, check_files: bool = True) -> ProjectProfile:
    return ProjectProfile.from_toml(
        root, (root / ".kent/workflow-profile.toml").read_text(),
        check_files=check_files,
    )


def bootstrap(root: Path = ROOT) -> None:
    """Preflight the complete copy set before any generated target mutation."""
    sync = load_synchronizer()
    profile = profile_at(root)
    if profile.schema_version != 3:
        raise SpecError("bootstrap requires the closed Kit schema-3 command set")
    copies = generated_copies(profile)
    if profile.adapters:
        raise SpecError("Kit bootstrap does not materialize platform adapters")
    plans = {}
    for key, source_path in copies.items():
        source = sync.project_target(ROOT, source_path)
        target = sync.project_target(root, profile.command(key))
        plans[key] = sync.plan_synchronization(
            source, target, key=key, update=False,
        )
    for key in ("compile_verify", "prepare_cleanup"):
        target = sync.project_target(root, profile.command(key))
        sync.validate_project_owned_target(target, kind=key)
    for plan in plans.values():
        sync.apply_synchronization(plan)
    verify_command_closure(root)


def verify_command_closure(root: Path = ROOT) -> None:
    sync = load_synchronizer()
    profile = profile_at(root)
    copies = generated_copies(profile)
    for key, configured_path in profile.commands.items():
        target = sync.project_target(root, configured_path)
        sync.validate_project_owned_target(target, kind=key)
        mode = target.stat().st_mode
        unsafe = stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX
        if not mode & stat.S_IXUSR or mode & unsafe:
            raise SpecError(
                f"command must be owner-executable without unsafe permission bits: {target}"
            )
    for key, source_path in copies.items():
        source = sync.project_target(ROOT, source_path)
        expected = sync.read_template(source, key)
        target = sync.project_target(root, profile.command(key))
        if target.read_bytes() != expected:
            raise SpecError(f"generated command differs from source: {target}")


def rendered_current_spec(root: Path = ROOT) -> str:
    spec = build_current_workflow(profile_at(root))
    spec = replace(spec, edges=tuple(
        replace(edge, prompt=UNACTIVATED_SOURCE + "\n\n" + (edge.prompt or ""))
        if edge.source == "backlog" and edge.target == "plan" else edge
        for edge in spec.edges
    ))
    return json.dumps(
        spec_as_json(spec),
        indent=2, ensure_ascii=False,
    ) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", action="store_true",
                        help="Explicitly materialize approved generated copies.")
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--write-spec", "--write-current-spec", dest="write_spec", action="store_true",
        help="Write the current source graph without changing a live Workflow.",
    )
    output.add_argument(
        "--check", "--check-current", dest="check", action="store_true",
        help="Read-only current graph and generated command parity check.",
    )
    args = parser.parse_args()
    if args.check and args.bootstrap:
        raise SpecError("--check cannot be combined with --bootstrap")
    if args.bootstrap:
        bootstrap()
    sync = load_synchronizer()
    if args.check:
        verify_command_closure()
        target = sync.project_target(ROOT, CURRENT_SPEC_PATH)
        if target.is_symlink() or not target.is_file():
            raise SpecError(f"current spec target is not a regular file: {target}")
        if target.read_bytes() != rendered_current_spec().encode("utf-8"):
            raise SpecError("current Kit workflow spec is stale")
    elif args.write_spec:
        rendered = rendered_current_spec()
        target = sync.project_target(ROOT, CURRENT_SPEC_PATH)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise SpecError(f"current spec target is not a regular file: {target}")
        target.write_text(rendered)
    else:
        sys.stdout.write(rendered_current_spec())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"Kit development workflow: {error}", file=sys.stderr)
        raise SystemExit(1)
