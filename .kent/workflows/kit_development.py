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

from workflowkit.delivery import build_delivery_workflow
from workflowkit.kent import spec_as_json
from workflowkit.model import SpecError, WorkflowSpec
from workflowkit.profile import ProjectProfile


SPEC_PATH = ".kent/workflows/kit-engineering-delivery-v5.spec.json"
CANDIDATE_SPEC_PATH = ".kent/workflows/kit-engineering-delivery-v6.spec.json"
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
COMMAND_TARGETS = {
    key: f".kent/scripts/{Path(source).name}"
    for key, source in (SCHEMA3_COPIES | PROJECT_COPIES).items()
} | {
    "github_observation": ".kent/scripts/workflow_github_observation.py",
    "runtime_contracts": ".kent/scripts/workflow_runtime_contracts.py",
    "compile_verify": ".kent/scripts/workflow-compile-verify",
    "prepare_cleanup": ".kent/scripts/workflow-prepare-cleanup",
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


def build_workflow(profile: ProjectProfile) -> WorkflowSpec:
    """Build the unchanged v5 default Kit graph."""
    return _add_plan_approval(
        build_delivery_workflow(profile, 5),
        expected_shape=(21, 52, 53),
    )


def build_complexity_candidate_workflow(
    profile: ProjectProfile,
) -> WorkflowSpec:
    """Build the source-only v6 candidate without changing the default profile."""
    if profile.writer_session_policy() != "continuous":
        raise SpecError(
            "the Kit complexity candidate requires continuous writer Sessions"
        )
    candidate_profile = replace(
        profile,
        policies={
            **profile.policies,
            "coder_selection": "complexity",
        },
    )
    return _add_plan_approval(
        build_delivery_workflow(candidate_profile, 6),
        expected_shape=(21, 54, 55),
    )


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
    if profile.schema_version != 3 or profile.commands != COMMAND_TARGETS:
        raise SpecError("bootstrap requires the closed Kit schema-3 command set")
    if profile.adapters:
        raise SpecError("Kit bootstrap does not materialize platform adapters")
    plans = {}
    for key, source_path in (SCHEMA3_COPIES | PROJECT_COPIES).items():
        source = sync.project_target(ROOT, source_path)
        target = sync.project_target(root, profile.command(key))
        plans[key] = sync.plan_synchronization(
            source, target, key=key, update=False,
        )
    for key in ("compile_verify", "prepare_cleanup"):
        target = sync.project_target(root, profile.command(key))
        sync.validate_project_owned_target(target, kind=key)
    # Schema 3 deliberately skips the five v2-named commands. Materializing
    # these unchanged sources here does not declare runtime-v2 adoption.
    sync.synchronize_schema3(profile, root, update=False)
    for key in PROJECT_COPIES:
        sync.apply_synchronization(plans[key])
    verify_command_closure(root)


def verify_command_closure(root: Path = ROOT) -> None:
    sync = load_synchronizer()
    profile = profile_at(root)
    if profile.commands != COMMAND_TARGETS:
        raise SpecError("verification requires the closed Kit command paths")
    for key, configured_path in profile.commands.items():
        target = sync.project_target(root, configured_path)
        sync.validate_project_owned_target(target, kind=key)
        mode = target.stat().st_mode
        unsafe = stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX
        if not mode & stat.S_IXUSR or mode & unsafe:
            raise SpecError(
                f"command must be owner-executable without unsafe permission bits: {target}"
            )
    for key, source_path in (SCHEMA3_COPIES | PROJECT_COPIES).items():
        source = sync.project_target(ROOT, source_path)
        expected = sync.read_template(source, key)
        target = sync.project_target(root, profile.command(key))
        if target.read_bytes() != expected:
            raise SpecError(f"generated command differs from source: {target}")


def rendered_spec(root: Path = ROOT) -> str:
    return json.dumps(
        spec_as_json(build_workflow(profile_at(root))),
        indent=2, ensure_ascii=False,
    ) + "\n"


def rendered_candidate_spec(root: Path = ROOT) -> str:
    return json.dumps(
        spec_as_json(
            build_complexity_candidate_workflow(profile_at(root)),
        ),
        indent=2, ensure_ascii=False,
    ) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", action="store_true",
                        help="Explicitly materialize approved generated copies.")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--write-spec", action="store_true")
    output.add_argument(
        "--write-candidate-spec",
        action="store_true",
        help="Write the source-only complexity-selected v6 candidate.",
    )
    output.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.bootstrap:
        bootstrap()
    sync = load_synchronizer()
    if args.write_candidate_spec:
        rendered = rendered_candidate_spec()
        target = sync.project_target(ROOT, CANDIDATE_SPEC_PATH)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise SpecError(
                f"candidate spec target is not a regular file: {target}"
            )
        target.write_text(rendered)
    elif args.write_spec:
        rendered = rendered_spec()
        target = sync.project_target(ROOT, SPEC_PATH)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise SpecError(f"spec target is not a regular file: {target}")
        target.write_text(rendered)
    elif args.check:
        verify_command_closure()
        target = sync.project_target(ROOT, SPEC_PATH)
        if target.read_text() != rendered_spec():
            raise SpecError("semantic workflow spec is stale")
        candidate_target = sync.project_target(ROOT, CANDIDATE_SPEC_PATH)
        if candidate_target.read_text() != rendered_candidate_spec():
            raise SpecError("source-only complexity candidate spec is stale")
    else:
        sys.stdout.write(rendered_spec())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"Kit development workflow: {error}", file=sys.stderr)
        raise SystemExit(1)
