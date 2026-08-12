from __future__ import annotations

from dataclasses import replace

from .model import EdgeSpec, NodeSpec, ParameterSpec, SpecError, WorkflowSpec
from .profile import ProjectProfile


WORKSPACE = ParameterSpec(
    "workspace_path",
    "Repository or managed-worktree root; never an artifact directory.",
)
REPORTED_WORKSPACE = ParameterSpec(
    "reported_workspace_path",
    "Rejected workspace value emitted by an upstream writer.",
)
PLAN = ParameterSpec(
    "plan_path",
    "Path to the approved implementation plan, or the literal not-applicable.",
)
PLAN_ROUTE = ParameterSpec(
    "plan_route",
    "Post-review route: start, continue, verify, or fix_continue.",
)
PLAN_ROUTE_CONTEXT = ParameterSpec(
    "plan_route_context",
    "Route-specific context; use not-applicable unless continuing a Fix bundle.",
)
PLAN_REVIEW_REPORT = ParameterSpec(
    "plan_review_report",
    "Independent read-only review of plan authority, scope, evidence, and ordering.",
)
PLAN_CHANGE_REPORT = ParameterSpec(
    "plan_change_report",
    "Deterministic summary of material plan-contract changes.",
)
WORK_KIND = ParameterSpec(
    "work_kind",
    "Stable project-profile work kind selected during Plan.",
)
REVIEW_CONTEXT = ParameterSpec(
    "review_context",
    "Implementation, fix, report, and artifact context required by verification.",
)
SMOKE_RATIONALE = ParameterSpec(
    "smoke_rationale",
    "Evidence-based reason that runtime smoke is required or may be skipped.",
)
SMOKE_SCOPE = ParameterSpec(
    "smoke_scope",
    "Focused runtime scenarios and surfaces that smoke testing must exercise.",
)
BLOCKER = ParameterSpec(
    "blocker_reason",
    "Exact blocker and the next user or external action required.",
)
FIX_CONTEXT = ParameterSpec(
    "fix_context",
    "Deduplicated, dependency-ordered task-scoped repair bundle for the single writer.",
)
VERIFICATION_STATUS = ParameterSpec(
    "verification_status",
    "Deterministic verification status: passed, needs_changes, or blocked.",
)
VERIFICATION_REPORT = ParameterSpec(
    "verification_report",
    "Deterministic verification command, result, and report or log location.",
)
STANDARDS_STATUS = ParameterSpec(
    "standards_status",
    "Repository standards status: passed, needs_changes, or blocked.",
)
STANDARDS_REPORT = ParameterSpec(
    "standards_report",
    "Read-only repository standards, architecture, and engineering report.",
)
COMPLIANCE_REPORT = ParameterSpec(
    "compliance_report",
    "Final delivery compliance attestation and any blocking findings.",
)
SPEC_STATUS = ParameterSpec(
    "spec_status",
    "Specification fidelity status: passed, needs_changes, or blocked.",
)
REVIEW_REPORT = ParameterSpec(
    "review_report",
    "Read-only specification and acceptance-criteria review report.",
)
PR_URL = ParameterSpec("pr_url", "Canonical pull request URL.")
BRANCH_NAME = ParameterSpec(
    "branch_name",
    "Exact non-empty current Git branch from git branch --show-current; "
    "required even when no pull request exists.",
)
MERGE_STRATEGY = ParameterSpec(
    "merge_strategy",
    "Resolved pull-request merge strategy: merge, squash, or rebase.",
)
PR_REPORT = ParameterSpec(
    "pr_report",
    "Pull request creation, review, conflict, or post-CI status report.",
)
CI_REPORT = ParameterSpec("ci_report", "CI checks and status summary.")
MERGE_REPORT = ParameterSpec(
    "merge_report",
    "Proof that the pull request is merged or equivalent delivery is complete.",
)
PUBLICATION_REPORT = ParameterSpec(
    "publication_report",
    "Exact package, version, merged source, publish command shape, and remote verification.",
)
CLOSURE_REASON = ParameterSpec(
    "closure_reason",
    "User-approved reason for closing or canceling delivery without merge.",
)
CLEANUP_REPORT = ParameterSpec(
    "cleanup_report",
    "Report of cleanup performed, skipped, or blocked.",
)
EVIDENCE_CONTEXT = ParameterSpec(
    "evidence_context",
    "Packaging-only evidence findings and exact artifacts permitted for repair.",
)
PR_HEAD_OID = ParameterSpec(
    "pr_head_oid",
    "Pull-request head commit observed after green CI.",
)
PR_BASE_OID = ParameterSpec(
    "pr_base_oid",
    "Pull-request base commit observed after green CI.",
)
CLEANUP_MODE = ParameterSpec(
    "cleanup_mode",
    "Cleanup proof mode: merged, no_pr, closed_without_merge, or report_only.",
)
CLEANUP_SESSION_ID = ParameterSpec(
    "cleanup_session_id",
    "Kent session ID of the completed resource-owning Cleanup node.",
)
TASK_SHORT_ID = ParameterSpec(
    "task_short_id",
    "Stable human-readable Kent task short ID.",
)


def build_delivery_workflow(
    profile: ProjectProfile,
    version: int,
) -> WorkflowSpec:
    orchestrator = profile.role("orchestrator")
    gate = profile.optional_role("gate") or orchestrator
    implementation = profile.role("implementation")
    fix = profile.role("fix")
    release = profile.role("release")
    smoke_enabled = profile.smoke_policy() != "disabled"
    pull_requests = profile.capability("pull_requests")
    qa = profile.role("qa") if smoke_enabled else ""
    ci = profile.role("ci") if pull_requests else ""
    plan_review_role = (
        profile.optional_role("plan_review")
        or profile.optional_role("spec_review")
        or profile.role("researcher")
    )
    fresh_writers = profile.writer_session_policy() == "fresh_per_slice"
    writer_handoff_context = (
        "new_session" if fresh_writers else "compact_and_continue_session"
    )
    branch_identity_handoff_source = (
        "immediate_source" if fresh_writers else "node:plan"
    )
    implementation_continuation_context = (
        "new_session" if fresh_writers else "continue_session"
    )
    plan_contract_continuation_source = (
        "immediate_source" if fresh_writers else "previous_target_or_new"
    )
    fix_context = (
        "new_session" if fresh_writers else "compact_and_continue_session"
    )
    fix_context_source = (
        "immediate_source" if fresh_writers else "previous_target_or_new"
    )
    writer_recovery_context = (
        "new_session" if fresh_writers else "compact_and_continue_session"
    )
    non_writer_recovery_context = "compact_and_continue_session"
    final_compliance = (
        pull_requests
        and profile.capability("compliance_review")
    )
    package_publish = profile.package_publish_after_main()
    merged_target = "publish_package" if package_publish else "cleanup"
    merged_prompt = (
        package_publish_prompt(profile)
        if package_publish
        else cleanup_prompt(profile, merged=True)
    )
    implementation_parameters = (WORKSPACE, PLAN, WORK_KIND)
    branch_identity_enabled = profile.branch_identity_policy() != "task"
    nodes: list[NodeSpec] = [
        NodeSpec("backlog", "start", "Backlog"),
        agent_node("plan", "Plan", orchestrator),
        agent_node("plan_review", "Independent Plan Review", plan_review_role),
        NodeSpec(
            "plan_contract",
            "script",
            "Plan Contract Accept",
            script_path=profile.command("plan_contract_accept"),
        ),
        NodeSpec(
            "plan_contract_continue",
            "script",
            "Plan Contract Continue Guard",
            script_path=profile.command("plan_contract_continue"),
        ),
        NodeSpec(
            "plan_contract_verify",
            "script",
            "Plan Contract Verify Guard",
            script_path=profile.command("plan_contract_verify"),
        ),
        agent_node(
            "plan_revalidation",
            "Plan Revalidation",
            orchestrator,
        ),
        agent_node("implement", "Implement", implementation),
        NodeSpec(
            "verification_dispatch",
            "script",
            "Verification Dispatch",
            script_path=profile.command("dispatch"),
        ),
        NodeSpec(
            "deterministic_verify",
            "script",
            "Deterministic Verify",
            script_path=profile.command("verify"),
        ),
        agent_node("verification_gate", "Verification Gate", gate),
        agent_node("fix", "Fix", fix),
        agent_node("cleanup", "Cleanup", release),
        NodeSpec("wont_do", "terminal", "Won't Do"),
        NodeSpec("done", "terminal", "Done"),
    ]
    if branch_identity_enabled:
        nodes.extend(
            [
                NodeSpec(
                    "branch_identity",
                    "script",
                    "Branch Identity",
                    script_path=profile.command("branch_identity"),
                ),
                agent_node(
                    "branch_identity_resolution",
                    "Branch Identity Resolution",
                    orchestrator,
                ),
            ]
        )
    if fresh_writers:
        nodes.append(
            NodeSpec(
                "plan_contract_fix_continue",
                "script",
                "Plan Contract Fix Guard",
                script_path=profile.command("plan_contract_fix_continue"),
            )
        )

    review_branches = ["deterministic_verify"]
    if profile.capability("standards_review"):
        nodes.append(
            agent_node(
                "standards_review",
                "Standards Review",
                profile.role("standards_review"),
            )
        )
        review_branches.append("standards_review")
    if profile.capability("spec_review"):
        nodes.append(
            agent_node(
                "spec_review",
                "Spec Review",
                profile.role("spec_review"),
            )
        )
        review_branches.append("spec_review")
    if len(review_branches) > 1:
        nodes.append(NodeSpec("verification_join", "join", "Verification Join"))

    if smoke_enabled:
        nodes.append(agent_node("smoke", "Smoke Test", qa))
    if pull_requests:
        if final_compliance:
            nodes.append(
                agent_node(
                    "compliance",
                    "Compliance Review",
                    profile.role("compliance"),
                )
            )
            nodes.append(
                agent_node(
                    "evidence_repair",
                    "Evidence Repair",
                    fix,
                )
            )
        nodes.extend(
            [
                agent_node("prepare_pr", "Prepare PR", release),
                agent_node("waiting_pr", "Waiting PR", ci),
                NodeSpec(
                    "merge_watch",
                    "script",
                    "Wait For PR Merge",
                    script_path=profile.command("wait_pr"),
                ),
            ]
        )
        if package_publish:
            nodes.append(
                agent_node(
                    "publish_package",
                    "Publish Package",
                    profile.role("package_release"),
                )
            )
    if profile.capability("ci_monitoring"):
        nodes.append(
            NodeSpec(
                "ci_watch",
                "script",
                "CI Watch",
                script_path=profile.command("wait_ci"),
            )
        )
        nodes.append(agent_node("ci_monitor", "Monitor CI", ci))
    if profile.capability("managed_worktrees"):
        nodes.append(
            NodeSpec(
                "task_janitor",
                "script",
                "Task Janitor",
                script_path=profile.command("janitor"),
            )
        )

    edges: list[EdgeSpec] = [
        EdgeSpec(
            key="start_plan",
            source="backlog",
            transition="start",
            target="plan",
            prompt=plan_prompt(profile, recovery_aware=fresh_writers),
            transition_description="Start one planning session for this task.",
        ),
    ]
    edges.extend(
        [
            EdgeSpec(
                key="plan_review",
                source="plan",
                transition="review_plan",
                target="plan_review",
                prompt=plan_review_prompt(profile),
                transition_description=(
                    "Planning is complete; independently review its authority, "
                    "scope, evidence, and executable ordering."
                ),
                parameters=(
                    WORKSPACE,
                    PLAN,
                    WORK_KIND,
                    PLAN_ROUTE,
                    PLAN_ROUTE_CONTEXT,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ),
            ),
            EdgeSpec(
                key="plan_review_accept",
                source="plan_review",
                transition="accepted",
                target="plan_contract",
                transition_description=(
                    "The independent plan review passed; accept the normalized "
                    "plan contract and continue through its declared route."
                ),
                parameters=(
                    WORKSPACE,
                    PLAN,
                    WORK_KIND,
                    PLAN_ROUTE,
                    PLAN_ROUTE_CONTEXT,
                    PLAN_REVIEW_REPORT,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ),
            ),
            EdgeSpec(
                key="plan_review_revalidate",
                source="plan_review",
                transition="needs_changes",
                target="plan_revalidation",
                context="continue_session",
                context_source="node:plan",
                prompt=plan_revalidation_prompt(profile, from_review=True),
                transition_description=(
                    "The read-only review found plan-contract defects; revise "
                    "the plan before any writer or verification stage proceeds."
                ),
                parameters=(
                    WORKSPACE,
                    PLAN,
                    WORK_KIND,
                    PLAN_ROUTE,
                    PLAN_ROUTE_CONTEXT,
                    PLAN_REVIEW_REPORT,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ),
            ),
            recovery_edge(
                "plan_review",
                context=non_writer_recovery_context,
                extra_parameters=(
                    WORKSPACE,
                    PLAN,
                    WORK_KIND,
                    PLAN_ROUTE,
                    PLAN_ROUTE_CONTEXT,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ),
            ),
            cancellation_edge("plan_review"),
            EdgeSpec(
                key="plan_revalidation_review",
                source="plan_revalidation",
                transition="review_plan",
                target="plan_review",
                context="continue_session",
                context_source="previous_target",
                prompt=plan_review_prompt(profile),
                transition_description=(
                    "The plan contract was reconciled; independently re-review "
                    "it before accepting the new snapshot."
                ),
                parameters=(
                    WORKSPACE,
                    PLAN,
                    WORK_KIND,
                    PLAN_ROUTE,
                    PLAN_ROUTE_CONTEXT,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ),
            ),
            recovery_edge(
                "plan_revalidation",
                context=non_writer_recovery_context,
                extra_parameters=(
                    WORKSPACE,
                    PLAN,
                    WORK_KIND,
                    PLAN_ROUTE,
                    PLAN_ROUTE_CONTEXT,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ),
            ),
            cancellation_edge("plan_revalidation"),
            EdgeSpec(
                key="plan_contract_continue_revalidate",
                source="plan_contract_continue",
                transition="changed",
                target="plan_revalidation",
                context="continue_session",
                context_source="node:plan",
                prompt=plan_revalidation_prompt(profile, from_review=False),
                transition_description=(
                    "The normalized accepted plan changed materially; reconcile "
                    "authority and acceptance before continuing."
                ),
                parameters=(
                    WORKSPACE,
                    PLAN,
                    WORK_KIND,
                    PLAN_ROUTE,
                    PLAN_ROUTE_CONTEXT,
                    PLAN_CHANGE_REPORT,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ),
            ),
            EdgeSpec(
                key="plan_contract_verify_revalidate",
                source="plan_contract_verify",
                transition="changed",
                target="plan_revalidation",
                context="continue_session",
                context_source="node:plan",
                prompt=plan_revalidation_prompt(profile, from_review=False),
                transition_description=(
                    "The normalized accepted plan changed materially; reconcile "
                    "authority and acceptance before verification."
                ),
                parameters=(
                    WORKSPACE,
                    PLAN,
                    WORK_KIND,
                    PLAN_ROUTE,
                    PLAN_ROUTE_CONTEXT,
                    PLAN_CHANGE_REPORT,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ),
            ),
        ]
    )
    if fresh_writers:
        edges.append(
            EdgeSpec(
                key="plan_contract_fix_revalidate",
                source="plan_contract_fix_continue",
                transition="changed",
                target="plan_revalidation",
                context="continue_session",
                context_source="node:plan",
                prompt=plan_revalidation_prompt(profile, from_review=False),
                transition_description=(
                    "A bounded Fix slice changed the accepted plan; reconcile "
                    "authority before continuing repair."
                ),
                parameters=(
                    WORKSPACE,
                    PLAN,
                    WORK_KIND,
                    PLAN_ROUTE,
                    PLAN_ROUTE_CONTEXT,
                    PLAN_CHANGE_REPORT,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ),
            )
        )
    if branch_identity_enabled:
        edges.extend(
            [
                EdgeSpec(
                    key="plan_contract_branch_identity",
                    source="plan_contract",
                    transition="start",
                    target="branch_identity",
                    transition_description=(
                        "The accepted plan contract is stable; resolve "
                        "source-control branch identity before implementation."
                    ),
                    parameters=implementation_parameters,
                ),
                EdgeSpec(
                    key="branch_identity_implement",
                    source="branch_identity",
                    transition="ready",
                    target="implement",
                    context=writer_handoff_context,
                    context_source=branch_identity_handoff_source,
                    prompt=implement_prompt(
                        profile,
                        fresh_session=fresh_writers,
                    ),
                    transition_description=(
                        "Branch identity is deterministic and implementation "
                        "can start."
                    ),
                    parameters=implementation_parameters,
                ),
                EdgeSpec(
                    key="branch_identity_resolution",
                    source="branch_identity",
                    transition="blocked",
                    target="branch_identity_resolution",
                    prompt=branch_identity_resolution_prompt(profile),
                    transition_description=(
                        "Branch identity is ambiguous or collides with existing "
                        "repository state and requires a user decision."
                    ),
                    parameters=(BLOCKER,) + implementation_parameters,
                ),
                EdgeSpec(
                    key="branch_identity_retry",
                    source="branch_identity_resolution",
                    transition="retry",
                    target="branch_identity",
                    transition_description=(
                        "Retry deterministic branch identity after the reported "
                        "collision or repository blocker is resolved."
                    ),
                    parameters=implementation_parameters,
                ),
                recovery_edge(
                    "branch_identity_resolution",
                    context=non_writer_recovery_context,
                    extra_parameters=implementation_parameters,
                    extra_prompt=(
                        "When the exact branch blocker is resolved, choose "
                        "`retry`. Do not rename, delete, or overwrite another "
                        "branch from this agent node."
                    ),
                ),
                cancellation_edge("branch_identity_resolution"),
            ]
        )
    else:
        edges.append(
            EdgeSpec(
                key="plan_contract_implement",
                source="plan_contract",
                transition="start",
                target="implement",
                context=writer_handoff_context,
                context_source=branch_identity_handoff_source,
                prompt=implement_prompt(profile, fresh_session=fresh_writers),
                transition_description=(
                    "The accepted plan contract is stable and implementation "
                    "can start without branch-identity routing."
                ),
                parameters=implementation_parameters,
            )
        )
    edges.extend(
        [
            EdgeSpec(
                key="plan_contract_continue_implement",
                source="plan_contract",
                transition="continue",
                target="implement",
                context=implementation_continuation_context,
                context_source=plan_contract_continuation_source,
                prompt=implement_prompt(profile, fresh_session=fresh_writers),
                transition_description=(
                    "The accepted plan contract is unchanged; continue with "
                    "the next ready writer-owned step."
                ),
                parameters=implementation_parameters,
            ),
            EdgeSpec(
                key="plan_contract_verify",
                source="plan_contract",
                transition="verify",
                target="verification_dispatch",
                transition_description=(
                    "The accepted plan contract is unchanged and writer work "
                    "is complete; normalize inputs for read-only verification."
                ),
                parameters=(WORKSPACE, REVIEW_CONTEXT),
            ),
            EdgeSpec(
                key="plan_contract_checked_continue",
                source="plan_contract_continue",
                transition="stable",
                target="implement",
                context=implementation_continuation_context,
                context_source=plan_contract_continuation_source,
                prompt=implement_prompt(profile, fresh_session=fresh_writers),
                transition_description=(
                    "The accepted plan contract is unchanged; continue with "
                    "the next ready writer-owned step."
                ),
                parameters=implementation_parameters,
            ),
            EdgeSpec(
                key="plan_contract_checked_verify",
                source="plan_contract_verify",
                transition="stable",
                target="verification_dispatch",
                transition_description=(
                    "The accepted plan contract is unchanged and writer work "
                    "is complete; normalize inputs for read-only verification."
                ),
                parameters=(WORKSPACE, REVIEW_CONTEXT),
            ),
        ]
    )
    if fresh_writers:
        edges.extend(
            [
                EdgeSpec(
                    key="plan_contract_fix_continue",
                    source="plan_contract",
                    transition="fix_continue",
                    target="fix",
                    context="new_session",
                    prompt=fix_prompt(profile, bounded=True),
                    transition_description=(
                        "The independently revalidated plan is accepted; "
                        "continue the remaining bounded repair bundle."
                    ),
                    parameters=(WORKSPACE, FIX_CONTEXT),
                ),
                EdgeSpec(
                    key="plan_contract_checked_fix",
                    source="plan_contract_fix_continue",
                    transition="stable",
                    target="fix",
                    context="new_session",
                    prompt=fix_prompt(profile, bounded=True),
                    transition_description=(
                        "The accepted plan contract is unchanged; continue only "
                        "the remaining bounded repair bundle."
                    ),
                    parameters=(WORKSPACE, FIX_CONTEXT),
                ),
            ]
        )
    edges.extend(
        [
            recovery_edge(
                "plan",
                context=non_writer_recovery_context,
            ),
            cancellation_edge("plan"),
        ]
    )
    edges.extend(
        [
            EdgeSpec(
                key="implement_continue",
                source="implement",
                transition="continue_implementation",
                target="plan_contract_continue",
                transition_description=(
                    "One plan step is complete; check the accepted plan contract "
                    "before starting the next writer slice."
                ),
                parameters=(
                    WORKSPACE,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ),
            ),
            EdgeSpec(
                key="implement_verify",
                source="implement",
                transition="verify",
                target="plan_contract_verify",
                transition_description=(
                    "Implementation is complete; check the accepted plan "
                    "contract before read-only verification."
                ),
                parameters=(
                    WORKSPACE,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ),
            ),
            recovery_edge(
                "implement",
                profile=profile,
                context=writer_recovery_context,
                fresh_session=fresh_writers,
                extra_parameters=(WORK_KIND,),
                extra_prompt=work_kind_recovery_instruction(profile),
            ),
            cancellation_edge("implement"),
            EdgeSpec(
                key="fix_verify",
                source="fix",
                transition="verify",
                target="plan_contract_verify",
                transition_description=(
                    "Task-scoped fixes are complete; check the accepted plan "
                    "contract before rerunning every verification branch."
                ),
                parameters=(WORKSPACE, REVIEW_CONTEXT, TASK_SHORT_ID),
            ),
            recovery_edge(
                "fix",
                profile=profile,
                context=writer_recovery_context,
                fresh_session=fresh_writers,
            ),
            cancellation_edge("fix"),
        ]
    )

    if fresh_writers:
        edges.append(
            EdgeSpec(
                key="fix_continue",
                source="fix",
                transition="continue_fix",
                target="plan_contract_fix_continue",
                transition_description=(
                    "One bounded fix slice is complete; check the accepted plan "
                    "before continuing the remaining task-scoped findings."
                ),
                parameters=(WORKSPACE, FIX_CONTEXT, TASK_SHORT_ID),
            )
        )

    fanout_parameters = (WORKSPACE, REVIEW_CONTEXT)
    edges.append(
        EdgeSpec(
            key="dispatch_deterministic_verify",
            source="verification_dispatch",
            transition="fanout_verify",
            target="deterministic_verify",
            transition_description=(
                "Run deterministic verification and independent read-only reviews."
            ),
            parameters=fanout_parameters,
        )
    )
    edges.append(
        EdgeSpec(
            key="dispatch_invalid_workspace",
            source="verification_dispatch",
            transition="invalid_workspace",
            target="fix",
            context="new_session",
            context_source="immediate_source",
            prompt=workspace_path_fix_prompt(),
            transition_description=(
                "Reject an artifact or foreign workspace path before verification."
            ),
            parameters=(REPORTED_WORKSPACE, FIX_CONTEXT),
        )
    )
    if "standards_review" in review_branches:
        edges.append(
            EdgeSpec(
                key="dispatch_standards_review",
                source="verification_dispatch",
                transition="fanout_verify",
                target="standards_review",
                prompt=standards_review_prompt(profile),
                transition_description=(
                    "Run deterministic verification and independent read-only reviews."
                ),
                parameters=fanout_parameters,
            )
        )
    if "spec_review" in review_branches:
        edges.append(
            EdgeSpec(
                key="dispatch_spec_review",
                source="verification_dispatch",
                transition="fanout_verify",
                target="spec_review",
                prompt=spec_review_prompt(profile),
                transition_description=(
                    "Run deterministic verification and independent read-only reviews."
                ),
                parameters=fanout_parameters,
            )
        )

    gate_prompt = verification_gate_prompt(profile)
    if len(review_branches) > 1:
        edges.extend(
            [
                EdgeSpec(
                    key="deterministic_report_join",
                    source="deterministic_verify",
                    transition="reported",
                    target="verification_join",
                    transition_description=(
                        "Deterministic verification completed and reported its status."
                    ),
                    parameters=(VERIFICATION_STATUS, VERIFICATION_REPORT),
                ),
                EdgeSpec(
                    key="verification_join_gate",
                    source="verification_join",
                    transition="evaluate",
                    target="verification_gate",
                    prompt=gate_prompt,
                    transition_description=(
                        "All direct verification branches reported; evaluate them."
                    ),
                ),
            ]
        )
        if "standards_review" in review_branches:
            edges.append(
                EdgeSpec(
                    key="standards_report_join",
                    source="standards_review",
                    transition="reported",
                    target="verification_join",
                    transition_description=(
                        "Repository standards review completed and reported its status."
                    ),
                    parameters=(
                        STANDARDS_STATUS,
                        STANDARDS_REPORT,
                    ),
                )
            )
        if "spec_review" in review_branches:
            edges.append(
                EdgeSpec(
                    key="spec_report_join",
                    source="spec_review",
                    transition="reported",
                    target="verification_join",
                    transition_description=(
                        "Specification review completed and reported its status."
                    ),
                    parameters=(SPEC_STATUS, REVIEW_REPORT),
                )
            )
    else:
        edges.append(
            EdgeSpec(
                key="deterministic_report_gate",
                source="deterministic_verify",
                transition="reported",
                target="verification_gate",
                prompt=gate_prompt,
                transition_description=(
                    "Deterministic verification completed; evaluate its report."
                ),
                parameters=(VERIFICATION_STATUS, VERIFICATION_REPORT),
            )
        )

    edges.extend(
        [
            EdgeSpec(
                key="gate_fix",
                source="verification_gate",
                transition="needs_changes",
                target="fix",
                context=fix_context,
                context_source=fix_context_source,
                prompt=fix_prompt(profile, bounded=fresh_writers),
                transition_description=(
                    "Verification found task-scoped issues for the single writer to fix."
                ),
                parameters=(WORKSPACE, FIX_CONTEXT),
            ),
            EdgeSpec(
                key="gate_reverify_after_user_action",
                source="verification_gate",
                transition="needs_user_action",
                target="verification_dispatch",
                requires_approval=True,
                transition_description=(
                    "Verification is externally blocked; rerun all branches after approval."
                ),
                parameters=(WORKSPACE, REVIEW_CONTEXT, BLOCKER),
            ),
            cancellation_edge("verification_gate"),
        ]
    )

    smoke_policy = profile.smoke_policy()
    delivery_target = post_smoke_target(profile)
    if smoke_policy in {"conditional", "required"}:
        edges.append(
            EdgeSpec(
                key="gate_smoke_required",
                source="verification_gate",
                transition="smoke_required",
                target="smoke",
                prompt=smoke_prompt(profile),
                transition_description=(
                    "All verification passed and runtime smoke is required."
                ),
                parameters=(
                    WORKSPACE,
                    REVIEW_CONTEXT,
                    SMOKE_RATIONALE,
                    SMOKE_SCOPE,
                ),
            )
        )
    if smoke_policy in {"conditional", "disabled"}:
        edges.append(
            EdgeSpec(
                key="gate_delivery_ready",
                source="verification_gate",
                transition="delivery_ready",
                target=delivery_target,
                prompt=delivery_prompt(profile, delivery_target),
                transition_description=(
                    "All verification passed and delivery may continue without "
                    "runtime smoke."
                ),
                parameters=(WORKSPACE, REVIEW_CONTEXT, SMOKE_RATIONALE),
            )
        )

    if smoke_policy != "disabled":
        smoke_delivery_edge_key = (
            "smoke_prepare_pr"
            if profile.capability("pull_requests")
            else f"smoke_{delivery_target}"
        )
        edges.extend(
            [
                EdgeSpec(
                    # Keep the historical key stable while taskless Delivery
                    # workflows are reconciled from Prepare PR to Compliance.
                    key=smoke_delivery_edge_key,
                    source="smoke",
                    transition="passed",
                    target=delivery_target,
                    prompt=delivery_prompt(profile, delivery_target),
                    transition_description=(
                        "Focused smoke testing passed and delivery may continue."
                    ),
                    parameters=(WORKSPACE, REVIEW_CONTEXT),
                ),
                EdgeSpec(
                    key="smoke_fix",
                    source="smoke",
                    transition="needs_changes",
                    target="fix",
                    context=fix_context,
                    context_source=fix_context_source,
                    prompt=fix_prompt(profile, bounded=fresh_writers),
                    transition_description=(
                        "Smoke testing found task-scoped implementation issues."
                    ),
                    parameters=(WORKSPACE, FIX_CONTEXT),
                ),
                recovery_edge(
                    "smoke",
                    context=non_writer_recovery_context,
                ),
            ]
        )

    if final_compliance:
        edges.extend(
            [
                EdgeSpec(
                    key="compliance_prepare_pr",
                    source="compliance",
                    transition="ship_pr",
                    target="prepare_pr",
                    prompt=prepare_pr_prompt(profile),
                    transition_description=(
                        "Final compliance passed; prepare or update the task pull request."
                    ),
                    parameters=(WORKSPACE, REVIEW_CONTEXT, COMPLIANCE_REPORT),
                ),
                EdgeSpec(
                    key="compliance_fix",
                    source="compliance",
                    transition="needs_changes",
                    target="fix",
                    context=fix_context,
                    context_source=fix_context_source,
                    prompt=fix_prompt(profile, bounded=fresh_writers),
                    transition_description=(
                        "Final compliance found task-scoped issues that require fixes."
                    ),
                    parameters=(WORKSPACE, FIX_CONTEXT),
                ),
                EdgeSpec(
                    key="compliance_evidence_repair",
                    source="compliance",
                    transition="repair_evidence",
                    target="evidence_repair",
                    context="new_session",
                    prompt=evidence_repair_prompt(profile),
                    transition_description=(
                        "Only packaging evidence is incomplete; repair those "
                        "artifacts without repeating source or runtime work."
                    ),
                    parameters=(WORKSPACE, REVIEW_CONTEXT, EVIDENCE_CONTEXT),
                ),
                EdgeSpec(
                    key="compliance_needs_user_action",
                    source="compliance",
                    transition="needs_user_action",
                    target="compliance",
                    context=non_writer_recovery_context,
                    prompt=compliance_recovery_prompt(profile),
                    requires_approval=True,
                    transition_description=(
                        "Final compliance is externally blocked; recheck after approval."
                    ),
                    parameters=(WORKSPACE, REVIEW_CONTEXT, BLOCKER),
                ),
                cancellation_edge("compliance"),
                EdgeSpec(
                    key="evidence_repair_compliance",
                    source="evidence_repair",
                    transition="recheck_compliance",
                    target="compliance",
                    context="compact_and_continue_session",
                    context_source="previous_target",
                    prompt=compliance_recheck_prompt(profile),
                    transition_description=(
                        "Packaging evidence was repaired; rerun only final "
                        "Compliance Review."
                    ),
                    parameters=(WORKSPACE, REVIEW_CONTEXT),
                ),
                EdgeSpec(
                    key="evidence_repair_fix",
                    source="evidence_repair",
                    transition="needs_source_fix",
                    target="fix",
                    context=fix_context,
                    context_source=fix_context_source,
                    prompt=fix_prompt(profile, bounded=fresh_writers),
                    transition_description=(
                        "Evidence repair proved that substantive source work "
                        "is still required."
                    ),
                    parameters=(WORKSPACE, FIX_CONTEXT),
                ),
                recovery_edge(
                    "evidence_repair",
                    profile=profile,
                    context=writer_recovery_context,
                    fresh_session=fresh_writers,
                    extra_parameters=(
                        WORKSPACE,
                        REVIEW_CONTEXT,
                        EVIDENCE_CONTEXT,
                    ),
                    extra_prompt=(
                        "Preserve the exact workspace, review context, and "
                        "packaging-only evidence scope. Do not broaden repair."
                    ),
                ),
                cancellation_edge("evidence_repair"),
            ]
        )

    if profile.capability("pull_requests"):
        created_target = (
            "ci_watch" if profile.capability("ci_monitoring") else "waiting_pr"
        )
        edges.extend(
            [
                EdgeSpec(
                    key=f"prepare_pr_{created_target}",
                    source="prepare_pr",
                    transition="monitor_ci",
                    target=created_target,
                    prompt=(
                        None
                        if created_target == "ci_watch"
                        else waiting_pr_prompt(profile)
                    ),
                    transition_description=(
                        "The pull request exists and its delivery state must be checked."
                    ),
                    parameters=(WORKSPACE, PR_URL, BRANCH_NAME, MERGE_STRATEGY),
                ),
                EdgeSpec(
                    key="prepare_pr_no_pr",
                    source="prepare_pr",
                    transition="no_pr",
                    target="cleanup",
                    prompt=cleanup_prompt(profile, no_pr=True),
                    requires_approval=True,
                    transition_description=(
                        "A pull request is not applicable; require approval before cleanup."
                    ),
                    parameters=(PR_REPORT,),
                ),
                EdgeSpec(
                    key="prepare_pr_fix",
                    source="prepare_pr",
                    transition="needs_changes",
                    target="fix",
                    context=fix_context,
                    context_source=fix_context_source,
                    prompt=pr_recovery_fix_prompt(
                        profile,
                        bounded=fresh_writers,
                    ),
                    requires_approval=True,
                    transition_description=(
                        "PR preparation found task-scoped changes that must be fixed."
                    ),
                    parameters=(WORKSPACE, BLOCKER),
                ),
                recovery_edge(
                    "prepare_pr",
                    context=non_writer_recovery_context,
                ),
            ]
        )

        if profile.capability("ci_monitoring"):
            edges.extend(
                [
                    EdgeSpec(
                        key="ci_watch_waiting_pr",
                        source="ci_watch",
                        transition="passed",
                        target="waiting_pr",
                        prompt=waiting_pr_prompt(profile),
                        transition_description=(
                            "The deterministic watcher confirmed terminal "
                            "green CI; classify merge readiness once."
                        ),
                        parameters=(
                            WORKSPACE,
                            PR_URL,
                            BRANCH_NAME,
                            MERGE_STRATEGY,
                            CI_REPORT,
                        ),
                    ),
                    EdgeSpec(
                        key="ci_watch_diagnose",
                        source="ci_watch",
                        transition="failed",
                        target="ci_monitor",
                        prompt=ci_prompt(profile),
                        transition_description=(
                            "CI reached a failed, cancelled, empty, or "
                            "infrastructure-error state; classify it once."
                        ),
                        parameters=(
                            WORKSPACE,
                            PR_URL,
                            BRANCH_NAME,
                            MERGE_STRATEGY,
                            CI_REPORT,
                        ),
                    ),
                    EdgeSpec(
                        key="ci_watch_merged",
                        source="ci_watch",
                        transition="pr_merged",
                        target=merged_target,
                        prompt=merged_prompt,
                        requires_approval=package_publish,
                        transition_description=(
                            "The pull request merged while deterministic CI "
                            "watching was active."
                        ),
                        parameters=(
                            WORKSPACE,
                            PR_URL,
                            BRANCH_NAME,
                            MERGE_REPORT,
                        ),
                    ),
                    EdgeSpec(
                        key="ci_monitor_waiting_pr",
                        source="ci_monitor",
                        transition="waiting_pr",
                        target="waiting_pr",
                        prompt=waiting_pr_prompt(profile),
                        transition_description=(
                            "Required CI checks passed; wait for an actual merge."
                        ),
                        parameters=(
                            WORKSPACE,
                            PR_URL,
                            BRANCH_NAME,
                            MERGE_STRATEGY,
                            CI_REPORT,
                        ),
                    ),
                    EdgeSpec(
                        key="ci_monitor_watch",
                        source="ci_monitor",
                        transition="watch_ci",
                        target="ci_watch",
                        transition_description=(
                            "A bounded infrastructure retry or refreshed CI "
                            "run is ready for deterministic watching."
                        ),
                        parameters=(
                            WORKSPACE,
                            PR_URL,
                            BRANCH_NAME,
                            MERGE_STRATEGY,
                            CI_REPORT,
                        ),
                    ),
                    EdgeSpec(
                        key="ci_monitor_merged",
                        source="ci_monitor",
                        transition="pr_merged",
                        target=merged_target,
                        prompt=merged_prompt,
                        requires_approval=package_publish,
                        transition_description=(
                            "The pull request merged before CI observation "
                            "completed; continue from the confirmed merged state."
                        ),
                        parameters=(
                            WORKSPACE,
                            PR_URL,
                            BRANCH_NAME,
                            MERGE_REPORT,
                        ),
                    ),
                    EdgeSpec(
                        key="ci_monitor_fix",
                        source="ci_monitor",
                        transition="needs_changes",
                        target="fix",
                        context=fix_context,
                        context_source=fix_context_source,
                        prompt=fix_prompt(profile, bounded=fresh_writers),
                        transition_description=(
                            "CI found task-scoped failures that require fixes."
                        ),
                        parameters=(WORKSPACE, FIX_CONTEXT),
                    ),
                    recovery_edge(
                        "ci_monitor",
                        context=non_writer_recovery_context,
                        extra_parameters=(
                            WORKSPACE,
                            PR_URL,
                            BRANCH_NAME,
                            MERGE_STRATEGY,
                            CI_REPORT,
                        ),
                        extra_prompt=(
                            "Preserve the exact PR, branch, merge strategy, "
                            "and terminal CI report."
                        ),
                    ),
                ]
            )

        edges.extend(
            [
                EdgeSpec(
                    key="fix_pr_merged_cleanup",
                    source="fix",
                    transition="pr_merged",
                    target=merged_target,
                    context="new_session",
                    prompt=merged_prompt,
                    requires_approval=package_publish,
                    transition_description=(
                        "Recovery only: the pull request is already merged; "
                        "skip obsolete Fix work and continue post-merge delivery."
                    ),
                    parameters=(WORKSPACE, PR_URL, BRANCH_NAME, MERGE_REPORT),
                ),
                EdgeSpec(
                    key="waiting_pr_cleanup",
                    source="waiting_pr",
                    transition="pr_merged",
                    target=merged_target,
                    prompt=merged_prompt,
                    requires_approval=package_publish,
                    transition_description=(
                        "The pull request is confirmed merged; continue post-merge delivery."
                    ),
                    parameters=(WORKSPACE, PR_URL, BRANCH_NAME, MERGE_REPORT),
                ),
                EdgeSpec(
                    key="waiting_pr_watch_merge",
                    source="waiting_pr",
                    transition="watch_merge",
                    target="merge_watch",
                    transition_description=(
                        "The pull request is open and feasible; wait "
                        "deterministically for a meaningful state change."
                    ),
                    parameters=(
                        WORKSPACE,
                        PR_URL,
                        BRANCH_NAME,
                        MERGE_STRATEGY,
                        PR_HEAD_OID,
                        PR_BASE_OID,
                    ),
                ),
                EdgeSpec(
                    key="merge_watch_still_waiting",
                    source="merge_watch",
                    transition="still_waiting",
                    target="merge_watch",
                    transition_description=(
                        "No meaningful PR state changed during a bounded watch "
                        "window; continue without an agent turn."
                    ),
                    parameters=(
                        WORKSPACE,
                        PR_URL,
                        BRANCH_NAME,
                        MERGE_STRATEGY,
                        PR_HEAD_OID,
                        PR_BASE_OID,
                    ),
                ),
                EdgeSpec(
                    key="merge_watch_state_changed",
                    source="merge_watch",
                    transition="state_changed",
                    target="waiting_pr",
                    context="compact_and_continue_session",
                    context_source="previous_target",
                    prompt=waiting_pr_changed_prompt(profile),
                    transition_description=(
                        "PR state changed materially; let the retained Waiting "
                        "PR session classify it once."
                    ),
                    parameters=(
                        WORKSPACE,
                        PR_URL,
                        BRANCH_NAME,
                        MERGE_STRATEGY,
                        PR_REPORT,
                    ),
                ),
                EdgeSpec(
                    key="merge_watch_cleanup",
                    source="merge_watch",
                    transition="pr_merged",
                    target=merged_target,
                    prompt=merged_prompt,
                    requires_approval=package_publish,
                    transition_description=(
                        "The deterministic watcher confirmed the PR merged."
                    ),
                    parameters=(WORKSPACE, PR_URL, BRANCH_NAME, MERGE_REPORT),
                ),
                EdgeSpec(
                    key="waiting_pr_needs_user_action",
                    source="waiting_pr",
                    transition="needs_user_action",
                    target="waiting_pr",
                    context=non_writer_recovery_context,
                    prompt=waiting_pr_prompt(profile),
                    requires_approval=True,
                    transition_description=(
                        "Waiting PR requires an actual human decision or "
                        "external access recovery."
                    ),
                    parameters=(WORKSPACE, PR_URL, BRANCH_NAME, MERGE_STRATEGY, BLOCKER),
                ),
                EdgeSpec(
                    key="waiting_pr_fix",
                    source="waiting_pr",
                    transition="needs_changes",
                    target="fix",
                    context=fix_context,
                    context_source=fix_context_source,
                    prompt=pr_feedback_fix_prompt(
                        profile,
                        bounded=fresh_writers,
                    ),
                    transition_description=(
                        "The PR state requires task-scoped changes before merge."
                    ),
                    parameters=(WORKSPACE, MERGE_STRATEGY, PR_REPORT),
                ),
                EdgeSpec(
                    key="waiting_pr_close_without_merge",
                    source="waiting_pr",
                    transition="close_without_merge",
                    target="cleanup",
                    prompt=cleanup_prompt(profile, closed=True),
                    requires_approval=True,
                    transition_description=(
                        "The user explicitly approved closing delivery without merge."
                    ),
                    parameters=(WORKSPACE, PR_REPORT, CLOSURE_REASON),
                ),
            ]
        )
        if profile.capability("ci_monitoring"):
            edges.append(
                EdgeSpec(
                    key="waiting_pr_ci_monitor",
                    source="waiting_pr",
                    transition="ci_required",
                    target="ci_watch",
                    transition_description=(
                        "The PR head changed or checks restarted; wait "
                        "deterministically for terminal CI state."
                    ),
                    parameters=(WORKSPACE, PR_URL, BRANCH_NAME, MERGE_STRATEGY),
                )
            )

        if package_publish:
            edges.extend(
                [
                    EdgeSpec(
                        key="publish_cleanup",
                        source="publish_package",
                        transition="published",
                        target="cleanup",
                        prompt=published_cleanup_prompt(profile),
                        transition_description=(
                            "The approved package is verified remotely; clean "
                            "up the merged task resources."
                        ),
                        parameters=(
                            WORKSPACE,
                            PR_URL,
                            BRANCH_NAME,
                            MERGE_REPORT,
                            PUBLICATION_REPORT,
                        ),
                    ),
                    EdgeSpec(
                        key="publish_needs_user_action",
                        source="publish_package",
                        transition="needs_user_action",
                        target="publish_package",
                        context="compact_and_continue_session",
                        prompt=package_publish_recovery_prompt(profile),
                        requires_approval=True,
                        transition_description=(
                            "Publication is blocked or partial; retry only "
                            "after explicit approval and a fresh remote-state check."
                        ),
                        parameters=(
                            WORKSPACE,
                            PR_URL,
                            BRANCH_NAME,
                            MERGE_REPORT,
                            BLOCKER,
                        ),
                    ),
                ]
            )

    if profile.capability("managed_worktrees"):
        edges.extend(
            [
                EdgeSpec(
                    key="cleanup_task_janitor",
                    source="cleanup",
                    transition="run_janitor",
                    target="task_janitor",
                    transition_description=(
                        "Cleanup reporting is complete; remove only "
                        "deterministically safe task-owned resources."
                    ),
                    parameters=(
                        WORKSPACE,
                        TASK_SHORT_ID,
                        PR_URL,
                        BRANCH_NAME,
                        MERGE_REPORT,
                        CLEANUP_MODE,
                        CLEANUP_SESSION_ID,
                        CLEANUP_REPORT,
                    ),
                ),
                EdgeSpec(
                    key="task_janitor_done",
                    source="task_janitor",
                    transition="done",
                    target="done",
                    transition_description=(
                        "The janitor removed safe resources or preserved them "
                        "with an explicit cleanup report."
                    ),
                    parameters=(CLEANUP_REPORT,),
                ),
                EdgeSpec(
                    key="task_janitor_blocked",
                    source="task_janitor",
                    transition="blocked",
                    target="cleanup",
                    context="compact_and_continue_session",
                    context_source="previous_target",
                    prompt=janitor_recovery_prompt(profile),
                    transition_description=(
                        "Janitor infrastructure failed after safety checks; "
                        "return to Cleanup without losing its context."
                    ),
                    parameters=(CLEANUP_REPORT, BLOCKER),
                ),
                recovery_edge(
                    "cleanup",
                    context=non_writer_recovery_context,
                ),
            ]
        )
    else:
        edges.extend(
            [
                EdgeSpec(
                    key="cleanup_done",
                    source="cleanup",
                    transition="done",
                    target="done",
                    transition_description=(
                        "Delivery and conservative cleanup are complete."
                    ),
                    parameters=(CLEANUP_REPORT,),
                ),
                recovery_edge(
                    "cleanup",
                    context=non_writer_recovery_context,
                ),
            ]
        )

    edges = qualify_transition_keys(edges)
    spec = WorkflowSpec(
        name=profile.workflow_name("delivery", version),
        description=(
            "Plan, implement, independently verify, fix, and deliver an "
            f"engineering change for {profile.project_name}."
        ),
        execution_target=profile.execution_target("delivery"),
        nodes=tuple(nodes),
        edges=tuple(edges),
    )
    spec.validate()
    return spec


def build_canary_workflow(
    profile: ProjectProfile,
    version: int,
) -> WorkflowSpec:
    capabilities = dict(profile.capabilities)
    capabilities.update(
        {
            "pull_requests": False,
            "ci_monitoring": False,
        }
    )
    policies = dict(profile.policies)
    policies["smoke"] = "disabled"
    procedures = dict(profile.procedures)
    procedures.update(
        {
            "plan": "",
            "implement": "",
            "fix": "",
            "smoke": "",
            "ship": "",
            "ci": "",
            "waiting_pr": "",
        }
    )
    canary_profile = replace(
        profile,
        release_topology="none",
        capabilities=capabilities,
        policies=policies,
        procedures=procedures,
    )
    delivery_spec = build_delivery_workflow(canary_profile, version)
    spec = replace(
        delivery_spec,
        name=profile.workflow_name("canary", version),
        description=(
            "Exercise planning, implementation continuation, deterministic "
            f"verification, fan-out, Join, and cleanup for {profile.project_name}."
        ),
        execution_target=profile.execution_target("canary"),
    )
    spec.validate()
    return spec


def build_smoke_lab_workflow(
    profile: ProjectProfile,
    label: str = "",
) -> WorkflowSpec:
    if profile.smoke_policy() != "conditional":
        raise SpecError("smoke-lab requires conditional Smoke policy")

    capabilities = dict(profile.capabilities)
    capabilities.update(
        {
            "pull_requests": False,
            "ci_monitoring": False,
        }
    )
    procedures = dict(profile.procedures)
    procedures.update(
        {
            "plan": "",
            "implement": "",
            "fix": "",
            "ship": "",
            "ci": "",
            "waiting_pr": "",
        }
    )
    lab_profile = replace(
        profile,
        release_topology="none",
        capabilities=capabilities,
        procedures=procedures,
    )
    delivery_spec = build_delivery_workflow(lab_profile, 1)
    spec = replace(
        delivery_spec,
        name=profile.workflow_name("smoke-lab", label=label),
        description=(
            "Exercise conditional runtime Smoke routing without PR or CI "
            f"delivery stages for {profile.project_name}."
        ),
        execution_target=profile.execution_target("smoke-lab"),
    )
    spec.validate()
    return spec


def agent_node(key: str, display_name: str, role: str) -> NodeSpec:
    return NodeSpec(
        key=key,
        kind="agent",
        display_name=display_name,
        agent=role,
        completion_mode="shell_command",
    )


def transition_key(source: str, outcome: str) -> str:
    return f"{source}_{outcome}"


def qualify_transition_keys(edges: list[EdgeSpec]) -> list[EdgeSpec]:
    outcomes_by_source: dict[str, set[str]] = {}
    for edge in edges:
        outcomes_by_source.setdefault(edge.source, set()).add(edge.transition)

    qualified: list[EdgeSpec] = []
    for edge in edges:
        prompt = edge.prompt
        if prompt:
            for outcome in sorted(
                outcomes_by_source.get(edge.target, ()),
                key=len,
                reverse=True,
            ):
                prompt = prompt.replace(
                    f"`{outcome}`",
                    f"`{transition_key(edge.target, outcome)}`",
                )
        qualified.append(
            replace(
                edge,
                transition=transition_key(edge.source, edge.transition),
                prompt=prompt,
            )
        )
    return qualified


def recovery_edge(
    node_key: str,
    *,
    profile: ProjectProfile | None = None,
    context: str = "compact_and_continue_session",
    fresh_session: bool = False,
    extra_parameters: tuple[ParameterSpec, ...] = (),
    extra_prompt: str = "",
) -> EdgeSpec:
    extra_contract = f"\n{extra_prompt}" if extra_prompt else ""
    cancellation_contract = ""
    if node_key in {
        "plan",
        "plan_review",
        "plan_revalidation",
        "implement",
        "fix",
        "verification_gate",
        "evidence_repair",
    }:
        cancellation_contract = (
            "\nIf the latest user instruction explicitly cancels the task, choose "
            "`wont_do` and provide `closure_reason`."
        )
    if fresh_session:
        if profile is None:
            raise ValueError(
                f"fresh recovery for {node_key!r} requires a project profile"
            )
        manifest_contract = {
            "implement": ("implement", "implement", "implementation"),
            "fix": ("implement", "fix", "implementation"),
            "evidence_repair": (
                "implement",
                "evidence_repair",
                "implementation",
            ),
        }.get(node_key)
        if manifest_contract is None:
            raise ValueError(
                f"fresh recovery context is not defined for {node_key!r}"
            )
        recovery_context = context_instruction(profile, *manifest_contract)
        stage_contract = {
            "plan": """
Reconcile any declared checkpoint and source task into authoritative
design/specification/plan artifacts before selecting implementation work.""",
            "implement": """
Implement exactly one unchecked ready writer-owned plan step. Runtime Smoke and
other workflow-owned review or delivery items are downstream scope even when a
legacy plan renders them as unchecked entries. Do not execute or mark those
items complete. Update the plan before choosing `continue_implementation` for
another fresh writer session; choose `verify` when no writer-owned step
remains.""",
            "fix": """
Apply exactly one independently verifiable remaining fix slice. Update the
authoritative fix checklist. Choose `continue_fix` with `workspace_path` and a
refreshed `fix_context` containing only the remaining findings, or choose
`verify` with the complete `review_context` when no fix slice remains.""",
        }.get(node_key, "")
        prompt = (
            f"""Restart the `{node_key}` stage in a fresh bounded session after user action.

Previous blocker: {{{{.Params.blocker_reason}}}}

{recovery_context}

Read the task body, current task comments, project instructions, authoritative
task artifacts, preserved worktree diff, and existing evidence before editing
or repeating checks. If user feedback changed a product decision or acceptance
criterion, update the authoritative design/specification/plan first and
reference the exact task-comment ID. Do not restart completed work.
{stage_contract}

Approval means only that the exact reported blocker action is now complete; it
is not acknowledgement that waiting may begin. Verify resolution before
editing. If the blocker remains, preserve task scope and return
`needs_user_action` again. Do not infer approval for any broader or destructive
action.{extra_contract}"""
            + cancellation_contract
        )
    else:
        prompt = (
            f"""Resume the `{node_key}` stage after user action.

Previous blocker: {{{{.Params.blocker_reason}}}}

Use the retained compacted context and re-read current task comments and
project instructions. Approval means only that the exact reported blocker
action is now complete; it is not acknowledgement that waiting may begin.
Verify resolution before continuing. If the blocker remains, preserve task
scope and return `needs_user_action` again. Do not infer approval for any
broader or destructive action.{extra_contract}"""
            + cancellation_contract
        )
    return EdgeSpec(
        key=f"{node_key}_needs_user_action",
        source=node_key,
        transition="needs_user_action",
        target=node_key,
        context=context,
        prompt=prompt,
        requires_approval=True,
        transition_description=(
            "Work is externally blocked. Do not approve until the reported "
            "external action is complete; approval resumes this stage."
        ),
        parameters=(BLOCKER,) + extra_parameters,
    )


def cancellation_edge(node_key: str) -> EdgeSpec:
    return EdgeSpec(
        key=f"{node_key}_wont_do",
        source=node_key,
        transition="wont_do",
        target="wont_do",
        requires_approval=True,
        transition_description=(
            "Cancel this task only after explicit user approval and record why."
        ),
        parameters=(CLOSURE_REASON,),
    )


def procedure_instruction(profile: ProjectProfile, key: str) -> str:
    path = profile.procedure(key)
    if path:
        return f"Use {path} as the project procedure."
    return "Follow the project contract and repository instructions."


def context_instruction(
    profile: ProjectProfile,
    manifest_key: str,
    node_key: str,
    evidence_type: str,
) -> str:
    manifest = profile.context_manifest(manifest_key)
    return f"""Read `{manifest}` first and stay inside its required and
conditionally triggered sources.

Before transition, pipe one non-empty JSON object to
`{profile.command("evidence")} append --task {{{{.TaskShortId}}}} --workspace
<workspace>`. Set `node_key` to `{node_key}`, `evidence_type` to
`{evidence_type}`, and `context.manifest_path` to `{manifest}`.
`context.files_read` lists other project instruction files in actual read
order; do not repeat the manifest there. Record repeated questions and
verification loops, use null for unavailable model/compaction counters, and
exclude secrets or broad raw evidence. Append is idempotent for the current
Kent run; on recovery, reuse the returned sequence/hash and continue."""


def branch_identity_resolution_prompt(profile: ProjectProfile) -> str:
    return f"""Resolve the deterministic branch-identity blocker before Implement.

{context_instruction(profile, "delivery", "branch_identity_resolution", "delivery")}

Project branch policy: `{profile.branch_identity_policy()}`.
Reported blocker: {{{{.Params.blocker_reason}}}}

Write all user-facing explanation in Russian. Inspect repository and task state
without editing files, renaming branches, deleting refs, pushing, or starting
implementation. Explain the exact collision or identity ambiguity and the
smallest safe external action that resolves it.

Choose `needs_user_action` with an updated `blocker_reason` while the blocker
remains. After the user or external system resolves that exact blocker, verify
it and choose `retry`. Choose `wont_do` only for an explicit cancellation and
provide `closure_reason`."""


def checkpoint_instruction(profile: ProjectProfile, stage: str) -> str:
    stage_details = {
        "fix": (
            "Record the pinned baseline, supplied findings, completed fix "
            "slices, fresh green checks, remaining findings, mutation ledger, "
            "and one next permitted action. A same-node `continue_fix` action "
            "is consumed when the task re-enters Fix. If the current "
            "`fix_context` still contains findings, do not repeat that "
            "transition or append bookkeeping-only evidence: rewrite "
            "`next_action` to one concrete remaining slice and complete it in "
            "the current session."
        ),
        "smoke": (
            "Record acceptance stages, lock resource/token state, exact "
            "runtime target, completed build/install/launch work, sanitized "
            "evidence paths, remaining scenarios, external-side-effect "
            "ledger, restoration state, and one next permitted action. "
            "Recover a saved token with the lock adapter's `resume`. If "
            "acquire succeeded but stdout was lost before checkpointing, use "
            "`resume-owned` only when lock status proves the same non-empty "
            "Kent task ID; it must not create or adopt a lock."
        ),
    }[stage]
    return f"""Maintain the ignored `{stage}` checkpoint with
`{profile.command("checkpoint")} <validate|read|write> --stage {stage} --task
{{{{.TaskShortId}}}} --workspace <workspace>`. Reconcile an existing checkpoint
before repeating work. Pipe one JSON object to `write` before the first
expensive or mutating action, after each bounded stage, and before transition.
It contains `completed`, `remaining`, and `mutation_ledger` arrays plus one
non-empty `next_action`. {stage_details} Exclude credentials, authenticated
content, raw logs, and broad device data."""


def work_kind_catalog(profile: ProjectProfile) -> str:
    return "\n".join(
        (
            f"- `{key}` — {work_kind.description.rstrip('.')}; "
            f"Plan: `{work_kind.plan}`; Implement: `{work_kind.implement}`."
        )
        for key, work_kind in profile.work_kinds.items()
    )


def work_kind_plan_instruction(profile: ProjectProfile) -> str:
    return f"""Select exactly one supported `work_kind` before detailed planning:

{work_kind_catalog(profile)}

A standalone `work_kind: <key>` declaration in the task body is an explicit
human routing decision and wins when the key is supported. Otherwise classify
conservatively from the requested outcome and source context. Do not use the
Jira issue type alone as proof. If the kind is unsupported, multiple kinds are
materially coupled, or the classification remains ambiguous, complete with
`needs_user_action` and ask for one supported key.

After selection, load the mapped Plan procedure as a procedure module. Some
project procedures also document execution; this Plan node must use only their
bootstrap, discovery, design/specification, analysis, planning, and plan-review
sections. The generated stage contract overrides any instruction to edit
production files, execute plan steps, invoke nested prompt flows, or launch a
separate delivery workflow."""


def work_kind_implement_instruction(profile: ProjectProfile) -> str:
    return f"""Selected work kind: `{{{{.Params.work_kind}}}}`.

Use the matching Implement procedure from this profile catalog:

{work_kind_catalog(profile)}

The selected key must exist in the catalog and match the authoritative plan. If
it does not, stop with `needs_user_action`; do not silently reclassify it. Load
only the mapped Implement procedure. When that file combines planning and
execution, skip its discovery, planning, audit-launch, and scope-confirmation
stages: execute exactly one already-approved unchecked plan step."""


def work_kind_recovery_instruction(profile: ProjectProfile) -> str:
    return """Preserve the selected `{{.Params.work_kind}}` routing key. Use its
mapped Implement procedure from `.kent/workflow-profile.toml`; do not
reclassify the task during recovery. Re-emit `work_kind` on
`continue_implementation` or another `needs_user_action` transition."""


def jira_reference_instruction(profile: ProjectProfile) -> str:
    if "jira_api" not in profile.required_adapters:
        return ""

    return f"""

When the source contains a Jira issue, use `{profile.adapter("jira_api")}` and
inspect its normalized `issue_links` before relying on semantic repository
search. The implementation scope contains only root issues explicitly supplied
by the task source/body or an exact human-authored task comment. Parent,
linked, cloned, related, dependency, and sibling issues are evidence or
dependency context only; they do not silently add requirements or implementation
scope. Record root scope, related evidence, and deferred/out-of-scope issues
separately. If the root issue depends on unresolved product requirements owned
by a related issue, report that dependency instead of absorbing it.

Follow related issues only one graph level and within the project procedure's
research limit. Do not key platform discovery only on the Jira link type: use
the linked issue summary, labels, components, fix versions, and description to
identify an iOS, web, backend, or other sibling task.

When a linked sibling task has an existing implementation in a
project-declared reference repository, inspect that implementation and its
tests as mandatory bounded product evidence. Record the Jira relationship,
exact repository commit and paths, plus `checked`, `adopted`, `rejected`, and
`conflicts` conclusions. The target platform's explicit task decisions,
current design, and API contracts remain authoritative. If no useful Jira
relation exists, fall back to a bounded feature fingerprint built from API operations/models,
screen or flow names, distinctive domain terms, and user-visible copy."""


def sentry_reference_instruction(profile: ProjectProfile) -> str:
    if "sentry_issues" not in profile.required_adapters:
        return ""

    return f"""

When the task source/body explicitly identifies a Sentry issue URL or
unambiguous numeric Sentry issue ID, use `{profile.adapter("sentry_issues")}` to
load the normalized issue and bounded latest-event evidence. Persist only the
exception type/value/mechanism, bounded in-app frames, release/environment,
first/last seen timestamps, count, status, and seen state; never persist raw
request, user, breadcrumb, variable, or context payloads.

Write durable Sentry source context into the task's normal planning artifacts
before changing external state. Then preview and mark that exact issue seen;
this automatic `mark-seen --allow-mutate` authority exists only for an
explicitly Sentry-backed task. Do not resolve or mute a Sentry issue in Plan.
Those outcomes require a later exact approval after a merged fix or an explicit
no-action decision."""


def plan_prompt(
    profile: ProjectProfile,
    *,
    recovery_aware: bool = False,
) -> str:
    recovery_contract = ""
    if recovery_aware:
        recovery_contract = """

If the task body declares a checkpoint ref, source task, or exact task-comment
IDs, treat the current checkout as preserved implementation rather than a blank
feature:

- verify that HEAD matches the declared checkpoint;
- read the source task body and exact referenced comments without modifying or
  canceling the source task;
- update one authoritative design/specification/plan set and reference comment
  IDs instead of duplicating decisions;
- explicitly supersede conflicting earlier decisions;
- inspect the checkpoint diff and existing tests/evidence;
- plan only remaining independently verifiable work.

Do not reset, revert, or reimplement preserved code during Plan."""
        recovery_contract += """

If the task body says the recovery Plan must stop for confirmation, complete
through `needs_user_action` with a concise artifact/remaining-work summary and
an explicit confirmation request. Do not choose `implement` in that Plan run."""

    return f"""Plan {{{{.TaskShortId}}}}: {{{{.TaskTitle}}}}

{context_instruction(profile, "plan", "plan", "plan")}

{work_kind_plan_instruction(profile)}
{jira_reference_instruction(profile)}
{sentry_reference_instruction(profile)}

Task body:
{{{{.TaskBody}}}}

Keep discovery, design/spec ingestion, decisions, and implementation planning in
this one Plan session. Ask questions when a product decision is required. Do not
invoke nested prompt flows and do not implement production changes.
Before planning implementation, write an explicit scope boundary containing the
included root source IDs, related evidence, dependencies, and deferred or
out-of-scope issues. A relationship, shared parent, common design file, or
adjacent implementation does not merge issue scopes.

Inventory dependency and cross-module impact before implementation. SDK or
schema upgrades, generated-contract changes, and adaptations in modules outside
the root issue must be identified as either a required dependency adaptation or
a separate deferred task. Do not silently widen product behavior to make a
dependency compile. Preserve generated enum, sealed, identifier, provider,
status, action, and flow types through domain boundaries; free-form string
routing requires evidence that no typed source contract exists plus an explicit
unknown-value strategy.

Assign every required evidence artifact an owner and a production-edit
boundary. If the plan requires a pre-edit red run, make it the first
writer-owned step and require capture before any production edit. The plan must
not turn a later failure to capture agent-owned evidence into a user approval.
Any design, specification, or plan that narrows, replaces, or claims to
supersede the task body must cite the exact human-authored task-comment ID or
another explicit authoritative source. Agent-authored comments, implementation
inference, and unsupported claims that "the user clarified" are not product
authority. Use `needs_user_action` before implementation when that provenance
is absent.
{recovery_contract}

Complete with `review_plan` only when the plan has no unresolved product, API,
UX, or safety ambiguity. `workspace_path` is the repository or
managed-worktree root; it is never `.todo/<feature>` or another artifact
directory. Provide that root plus `plan_path`, selected `work_kind`,
`plan_route=start`, `plan_route_context=not-applicable`,
`task_short_id={{{{.TaskShortId}}}}`, and a concise `review_context` naming the
governing authority, source IDs, acceptance criteria, planned evidence, and
risks. Use the literal `not-applicable` as `plan_path` only when the project
contract explicitly allows planless work. Kent transition parameters must be
non-empty.
Complete with `needs_user_action` and `blocker_reason` for an external blocker.
Choose `wont_do` only for an explicit cancellation decision and provide
`closure_reason`."""


def implement_prompt(
    profile: ProjectProfile,
    *,
    fresh_session: bool = False,
) -> str:
    fresh_contract = ""
    if fresh_session:
        fresh_contract = """

This is a fresh writer session. Treat the current worktree, task comments, and
authoritative plan as the complete handoff. Inspect existing changes and
evidence before editing; do not repeat checked work. If a comment changes a
product decision, update the authoritative design/specification/plan with the
exact comment ID before implementation. Keep the step independently
verifiable; the next step runs in another fresh writer session."""

    return f"""Implement {{{{.TaskShortId}}}}: {{{{.TaskTitle}}}}

{context_instruction(profile, "implement", "implement", "implementation")}

Plan: {{{{.Params.plan_path}}}}. Workspace: {{{{.Params.workspace_path}}}}.

{work_kind_implement_instruction(profile)}

Use the project procedure for step selection, recipes, editing, focused checks,
and plan progress. This generated workflow's completion contract overrides any
legacy procedure transition names such as `audit`.
{fresh_contract}

Apply the `implementation-worker` role contract to exactly one ready
writer-owned plan step. Capture any planned pre-edit evidence before the first
production edit.

After marking that step complete, choose `continue_implementation` with
`workspace_path`, `task_short_id={{{{.TaskShortId}}}}`, and a concise
`review_context` with the completed step, changed files, checks, and next ready
step when unchecked writer-owned ready steps remain. Choose `verify` when every
writer-owned plan step is complete; provide the same workspace/task identity
plus `review_context` summarizing plan/spec paths, the fixed comparison point,
changed files, checks, risks, and any downstream runtime acceptance scope.
Both outcomes pass through graph-owned deterministic Plan Contract checks;
writer output cannot select accept mode or a different continuation route.
Use `needs_user_action` only for an external blocker and provide
`blocker_reason` plus the unchanged `work_kind`. Its approval is a resume signal
after the named external action is complete, not acknowledgement of waiting;
state that condition explicitly. Choose `wont_do` only for explicit
cancellation and provide `closure_reason`."""


def plan_review_prompt(profile: ProjectProfile) -> str:
    return f"""Independently review the proposed plan for
{{{{.TaskShortId}}}} without editing files.

{context_instruction(profile, "review", "plan_review", "review")}

Workspace: {{{{.Params.workspace_path}}}}
Plan: {{{{.Params.plan_path}}}}
Work kind: {{{{.Params.work_kind}}}}
Requested post-review route: {{{{.Params.plan_route}}}}
Route context: {{{{.Params.plan_route_context}}}}
Planning context: {{{{.Params.review_context}}}}

Read the task body, current human-authored comments, exact source records named
by the plan, and the plan itself. Use the read-only `spec-reviewer` contract,
adapted to the proposed plan rather than an implementation diff. Do not edit
the plan, code, task, or external systems.

Check:

- every narrowed or superseded decision has exact human authority;
- root scope is separate from related evidence and deferred work;
- product, API, UX, architecture, safety, and destructive-action choices are
  explicit rather than invented by the planner;
- acceptance criteria map to owned deterministic, review, or runtime evidence;
- dependencies and generated-contract adaptations stay bounded;
- writer steps are executable, dependency ordered, and do not hide workflow
  stages as implementation work.

Choose `accepted` only when implementation or verification may safely follow.
Provide `workspace_path`, `plan_path`, `work_kind`, unchanged `plan_route`,
unchanged `plan_route_context`, `task_short_id={{{{.TaskShortId}}}}`, a concise
`plan_review_report`, and refreshed `review_context` that includes the review
result. `plan_route_context` is `not-applicable` except when preserving the
remaining bounded Fix bundle for `fix_continue`.

Choose `needs_changes` for plan-contract defects and provide the same identity,
unchanged route and route context, `plan_review_report`, and `review_context`;
the retained Plan session will revise the artifact. Choose `needs_user_action`
only for a real missing product decision or external authority and provide the
preserved identity/context plus `blocker_reason`. Choose `wont_do` only for
explicit cancellation and provide `closure_reason`."""


def plan_revalidation_prompt(
    profile: ProjectProfile,
    *,
    from_review: bool,
) -> str:
    finding_label = (
        "Independent Plan Review findings: {{.Params.plan_review_report}}"
        if from_review
        else "Detected plan-contract change: {{.Params.plan_change_report}}"
    )
    return f"""Revalidate the authoritative plan for {{{{.TaskShortId}}}}.

{context_instruction(profile, "plan", "plan_revalidation", "plan")}

Workspace: {{{{.Params.workspace_path}}}}
Plan: {{{{.Params.plan_path}}}}
Work kind: {{{{.Params.work_kind}}}}
Intended route after acceptance: {{{{.Params.plan_route}}}}
Route context: {{{{.Params.plan_route_context}}}}
Current context: {{{{.Params.review_context}}}}
{finding_label}

Continue the retained planning context. Re-read current task comments and exact
authority sources. Distinguish operational feedback that fits the accepted
contract from material changes to requirements, architecture, acceptance,
safety, or evidence. Reconcile only material changes in the authoritative
design/specification/plan and cite exact human-authored task-comment IDs or
other explicit sources. Do not edit production code or execute verification.
When revalidation was triggered by deterministic drift, compare the current
plan with the prior normalized snapshot at
`.kent/runtime/{{{{.TaskShortId}}}}/plan-contract.json`; checkbox state alone
is intentionally absent from that contract.

Choose `review_plan` after reconciliation and provide `workspace_path`,
`plan_path`, unchanged `work_kind`, unchanged `plan_route`,
unchanged `plan_route_context`, `task_short_id={{{{.TaskShortId}}}}`, and
refreshed `review_context`. Preserve the remaining bounded Fix bundle only in
`plan_route_context` when the route is `fix_continue`; otherwise use
`not-applicable`. The independent Plan Review will run again before the new
normalized snapshot is accepted. Use `needs_user_action` with preserved
context and `blocker_reason` only for a real unresolved decision or external
authority. Choose `wont_do` only for explicit cancellation and provide
`closure_reason`."""


def fix_prompt(
    profile: ProjectProfile,
    *,
    bounded: bool = False,
) -> str:
    bounded_contract = ""
    completion_contract = """
Treat the incoming findings as one dependency-ordered repair bundle. Deduplicate
overlap, group findings by root cause, and resolve every compatible group in
this retained Fix session. Verify each group narrowly and update the checkpoint
after meaningful work; do not transition merely to hand off bookkeeping. When
the bundle is empty, complete with `verify` and provide `workspace_path` plus a
refreshed `review_context` containing the findings, fixes, changed files,
artifact paths, and focused checks."""
    if bounded:
        bounded_contract = """

This is a fresh bounded writer session. Read exact task-comment IDs referenced
by the findings and inspect the preserved diff, authoritative artifacts, and
existing evidence before editing. If feedback changes a product decision or
acceptance criterion, update the authoritative design/specification/plan first
and reference the comment ID. Do not redo completed work.

Apply exactly one independently verifiable fix slice and update the
authoritative fix checklist. A non-empty incoming `fix_context` is a work
assignment. If the inherited checkpoint only says to take `fix_continue_fix`,
that action was consumed by entry into this session: replace it with one
concrete supplied slice before work. Do not create a transition-only session or
append evidence for a bookkeeping-only handoff."""
        completion_contract = """
After one slice, choose `continue_fix` with `workspace_path`,
`task_short_id={{.TaskShortId}}`, and a refreshed `fix_context` containing
only the remaining findings. Choose `verify` only when no fix slice remains,
and provide `workspace_path`, the same `task_short_id`, plus a refreshed
`review_context` containing the findings, fixes, changed files, artifact paths,
and focused checks."""
    checkpoint = checkpoint_instruction(profile, "fix")

    return f"""Apply task-scoped fixes for {{{{.TaskShortId}}}}.

{context_instruction(profile, "implement", "fix", "implementation")}

Workspace: {{{{.Params.workspace_path}}}}. Findings:
{{{{.Params.fix_context}}}}

{procedure_instruction(profile, "fix")}
{bounded_contract}

{checkpoint}

Apply the `fix-worker` role contract only to concrete task-introduced or
task-worsened findings in `fix_context`. Baseline-wide debt, an unproven
differential, or contradictory policy is not writer scope.
{completion_contract}
Use `needs_user_action` only for an external blocker and provide
`blocker_reason`. Its approval is a resume signal after the named external
action is complete, not acknowledgement of waiting; state that condition
explicitly. Choose `wont_do` only for explicit cancellation and provide
`closure_reason`."""


def workspace_path_fix_prompt() -> str:
    return """Correct invalid workflow metadata without editing production files.

Verification dispatch rejected the reported workspace:
{{.Params.reported_workspace_path}}
Reason: {{.Params.fix_context}}

Treat Kent's current task execution root as authoritative. Resolve its canonical
repository root with `git rev-parse --show-toplevel`, or canonical current
directory for an intentional non-Git workspace. Do not move the worktree,
change task artifacts, or edit code.

Complete with `verify` and provide the canonical root as `workspace_path`,
`task_short_id={{.TaskShortId}}`, plus the preserved `review_context`. Use
`needs_user_action` only if Kent's execution root itself is unavailable or
ambiguous. Choose `wont_do` only for explicit cancellation and provide
`closure_reason`."""


def standards_review_prompt(profile: ProjectProfile) -> str:
    return f"""Run the independent read-only Standards Review.

{context_instruction(profile, "review", "standards_review", "review")}

Workspace: {{{{.Params.workspace_path}}}}
Review context: {{{{.Params.review_context}}}}

Apply the `standards-reviewer` role contract to the supplied task delta and its
pinned baseline. Do not edit files. Findings are data for Join, not a routing
decision.

Complete only with `reported`. Provide `standards_status` as exactly `passed`,
`needs_changes`, or `blocked`, plus `standards_report` with rule, path, and
differential evidence."""


def spec_review_prompt(profile: ProjectProfile) -> str:
    return f"""Run the independent read-only Specification Review.

{context_instruction(profile, "review", "spec_review", "review")}

Workspace: {{{{.Params.workspace_path}}}}
Review context: {{{{.Params.review_context}}}}

Apply the `spec-reviewer` role contract to the supplied task authority,
acceptance evidence, and task delta. Do not edit files. Findings are data for
Join, not a routing decision.

Complete only with `reported`. Provide `spec_status` as exactly `passed`,
`needs_changes`, or `blocked`, plus `review_report` with evidence and concrete
gaps."""


def verification_gate_prompt(profile: ProjectProfile) -> str:
    if profile.capability("standards_review"):
        standards = (
            "Standards status: {{.Params.standards_status}}\n"
            "Standards report: {{.Params.standards_report}}"
        )
    else:
        standards = "Standards review: not enabled by the project profile."
    spec = (
        "Spec status: {{.Params.spec_status}}\n"
        "Spec report: {{.Params.review_report}}"
        if profile.capability("spec_review")
        else "Specification review: not enabled by the project profile."
    )
    smoke_decision = smoke_decision_instruction(profile)
    return f"""Evaluate the joined verification reports without editing files.

{context_instruction(profile, "review", "verification_gate", "review")}

Workspace: {{{{.Params.verification_dispatch_fanout_verify.workspace_path}}}}
Review context: {{{{.Params.verification_dispatch_fanout_verify.review_context}}}}
Verification status: {{{{.Params.verification_status}}}}
Verification report: {{{{.Params.verification_report}}}}
{standards}
{spec}

Apply the `workflow-gate` role contract. Choose a delivery transition only when
every enabled status is `passed`. Do not poll or classify PR CI here; delivery
owns CI after PR preparation. Keep full reports in `review_context` and make
user-facing commentary name only a real decision or external action.

For `needs_changes`, emit the deduplicated, dependency-ordered repair bundle
required by the `workflow-gate` role contract.

Provide `workspace_path`, a refreshed `review_context` summarizing all reports,
and the required Smoke decision fields. The refreshed `review_context` must
record the profile Smoke policy, selected transition, rationale, and required
scope or concrete evidence for bypassing Smoke.

{smoke_decision}

Choose `needs_changes` for task-scoped failures and provide `workspace_path`
plus `fix_context`. Choose `needs_user_action` for external or contradictory
blockers and provide `workspace_path`, `review_context`, and `blocker_reason`;
after approval every verification branch reruns. Choose `wont_do` only for an
explicit cancellation decision and provide `closure_reason`."""


def smoke_prompt(profile: ProjectProfile) -> str:
    checkpoint = checkpoint_instruction(profile, "smoke")
    return f"""Run focused smoke testing for {{{{.TaskShortId}}}}.

{context_instruction(profile, "smoke", "smoke", "smoke")}

Workspace: {{{{.Params.workspace_path}}}}. Review context:
{{{{.Params.review_context}}}}
Smoke rationale: {{{{.Params.smoke_rationale}}}}
Required scope: {{{{.Params.smoke_scope}}}}

{procedure_instruction(profile, "smoke")}

{checkpoint}

Follow the project-specific browser, device, simulator, hardware, resource-lock,
build, deploy, install, launch, account, and isolation rules that apply. Do not
edit production files; route implementation findings to the single writer.
Complete with `passed` and provide `workspace_path` plus an updated
`review_context` containing the decision, rationale, tested scope, evidence,
artifacts, and untested areas. Use `needs_changes` with `workspace_path` and
`fix_context` for task code issues. Use `needs_user_action` with
`blocker_reason` for external blockers."""


def compliance_prompt(profile: ProjectProfile) -> str:
    completed_reviews = ["deterministic verification"]
    if profile.capability("standards_review"):
        completed_reviews.append("Standards Review")
    if profile.capability("spec_review"):
        completed_reviews.append("Spec Review")
    evidence_chain = ", ".join(completed_reviews)
    return f"""Run the final read-only delivery compliance review for {{{{.TaskShortId}}}}.

{context_instruction(profile, "review", "compliance", "review")}

Workspace: {{{{.Params.workspace_path}}}}. Final review context:
{{{{.Params.review_context}}}}

{procedure_instruction(profile, "compliance")}

Apply the `compliance_reviewer` role contract as a thin final attestation, not
another broad review. Verify the final diff, authority hierarchy,
{evidence_chain}, Gate, and any required Smoke or documented bypass. Do not
edit files or perform state-changing actions.

Choose `ship_pr` only when the final work product is compliant. Provide
`workspace_path`, a complete `review_context`, and `compliance_report`. Choose
`repair_evidence` only when the source diff and substantive deterministic,
Standards, Specification, Gate, and Smoke decisions are already valid and the
only defect is a missing, empty, stale, or internally contradictory
report/checklist/evidence package. Provide `workspace_path`, `review_context`,
and `evidence_context` naming the exact permitted artifacts and required
correction. Choose `needs_changes` for substantive task-scoped violations and
provide `workspace_path` plus `fix_context`; source fixes must rerun the full
verification flow. Choose
`needs_user_action` for missing or contradictory authority, evidence, or
external decisions and provide `workspace_path`, `review_context`, and
`blocker_reason`. Choose `wont_do` only for explicit cancellation and provide
`closure_reason`."""


def compliance_recovery_prompt(profile: ProjectProfile) -> str:
    return f"""Resume the final read-only delivery compliance review.

{context_instruction(profile, "review", "compliance", "review")}

Workspace: {{{{.Params.workspace_path}}}}
Final review context: {{{{.Params.review_context}}}}
Previous blocker: {{{{.Params.blocker_reason}}}}

Re-read current task comments and applicable authority sources, then verify
that the exact blocker is resolved. Do not infer approval for broader scope or
destructive actions.

{procedure_instruction(profile, "compliance")}

Choose `ship_pr` only when the final work product is compliant. Provide
`workspace_path`, a complete `review_context`, and `compliance_report`. Choose
`repair_evidence` only for packaging-only evidence defects and provide
`workspace_path`, `review_context`, and `evidence_context`. Choose
`needs_changes` with `workspace_path` and `fix_context` for substantive
task-scoped violations. Choose `needs_user_action` with `workspace_path`,
`review_context`, and `blocker_reason` if the blocker remains. Choose `wont_do`
only for explicit cancellation and provide `closure_reason`."""


def evidence_repair_prompt(profile: ProjectProfile) -> str:
    return f"""Repair packaging-only workflow evidence for {{{{.TaskShortId}}}}.

{context_instruction(profile, "implement", "evidence_repair", "implementation")}

Workspace: {{{{.Params.workspace_path}}}}. Final review context:
{{{{.Params.review_context}}}}
Allowed evidence repair:
{{{{.Params.evidence_context}}}}

{procedure_instruction(profile, "compliance")}

This is not a source-code Fix or another verification pass. Confirm first that
the supplied context proves the source diff and substantive deterministic,
Standards, Specification, Gate, and required Smoke decisions already passed.
Edit only the exact ignored reports, summaries, checklists, or evidence indexes
named in `evidence_context`. Do not edit production source, tests, build files,
specifications, or plans; do not build, install, launch, reacquire a device, or
repeat runtime navigation.
This node's narrow write contract overrides a compliance procedure's general
read-only clause only for the exact artifacts named in `evidence_context`. Do
not create or update the Fix checkpoint from this node.

Repair missing or empty required text, reconcile contradictory status wording
with Kent's authoritative task state, and rerun only the project evidence audit
or deterministic artifact validation needed for those files.

Choose `recheck_compliance` with `workspace_path` and a refreshed
`review_context` when packaging is valid. Choose `needs_source_fix` with
`workspace_path` and `fix_context` if the supplied defect is actually
substantive and cannot be repaired inside the named evidence artifacts. Use
`needs_user_action` only for an external blocker and provide `blocker_reason`.
Also preserve `workspace_path`, `review_context`, and `evidence_context` for
recovery. Choose `wont_do` only for explicit cancellation and provide
`closure_reason`."""


def compliance_recheck_prompt(profile: ProjectProfile) -> str:
    return f"""Recheck final Compliance after packaging-only evidence repair.

{context_instruction(profile, "review", "compliance", "review")}

Workspace: {{{{.Params.workspace_path}}}}
Updated review context: {{{{.Params.review_context}}}}

{procedure_instruction(profile, "compliance")}

Apply the retained `compliance_reviewer` role context only to the repaired
evidence artifacts. Confirm they agree with already-passed substantive results;
do not repeat code, architecture, specification, build, or runtime review.

Choose `ship_pr` with `workspace_path`, `review_context`, and
`compliance_report` when the package is complete. Choose `repair_evidence`
again only for another packaging-only defect and provide `evidence_context`.
Choose `needs_changes` with `workspace_path` and `fix_context` only when the
repair exposed a substantive task defect. Use `needs_user_action` with
`workspace_path`, `review_context`, and `blocker_reason` for an external or
authority blocker. Choose `wont_do` only for explicit cancellation and provide
`closure_reason`."""


def prepare_pr_prompt(profile: ProjectProfile) -> str:
    compliance_context = (
        "Final Compliance Review: {{.Params.compliance_report}}"
        if (
            profile.capability("pull_requests")
            and profile.capability("compliance_review")
        )
        else "Final Compliance Review is disabled by the project profile."
    )
    merge_policy = profile.pr_merge_strategy()
    return f"""Prepare delivery for {{{{.TaskShortId}}}}.

{context_instruction(profile, "delivery", "prepare_pr", "delivery")}

Workspace: {{{{.Params.workspace_path}}}}. Review context:
{{{{.Params.review_context}}}}
{compliance_context}

{procedure_instruction(profile, "ship")}

This workflow explicitly authorizes committing the task changes, pushing only
the current task branch, and creating or updating its pull request. It never
authorizes merging, pushing protected branches, or broadening scope.

Treat `git branch --show-current` as branch authority. The branch may differ
from the Kent task short ID when the project enabled deterministic branch
identity; never reconstruct or reject it by comparing branch text with
`{{{{.TaskShortId}}}}`.

If the task source URL or body identifies an issue in the same repository and
this task fully resolves it, include the provider-native closing reference in
the pull-request body, such as `Fixes #N` on GitHub. Use a non-closing link for
cross-repository, partial, or follow-up relationships. Read the task's current
`source_url` through Kent task metadata when it is not already visible in the
prompt; do not infer issue linkage from branch text alone.

Configured PR merge policy: `{merge_policy}`.

Apply the `delivery-operator` role contract. Resolve the strategy once before
publishing. On GitHub, serialize repository capabilities, target branch
protection, applicable rulesets, merge queue, and PR method state, then run:

`~/.kent/bin/kent-resolve-github-merge-strategy --policy {merge_policy}`

The adapter's structured result is authoritative for strategy selection.
Continue only when it returns `outcome=resolved`; for
`outcome=needs_user_action`, preserve its code, candidates, and reason instead
of manually choosing. Validate the resolved method through the role's
method-specific contract before and after creating or updating the PR.

Before committing, prove that the checkout is on a task-owned branch permitted
by the project contract. A source workspace, detached checkout, protected
branch, or ambiguous branch owner must route to `needs_user_action`; never push
through that ambiguity.

Complete through `monitor_ci` and provide `workspace_path`, `pr_url`, and
`branch_name`, plus the resolved `merge_strategy`. If no PR is genuinely
applicable, choose `no_pr` and provide `pr_report`; this path requires
approval. Use `needs_changes` with `workspace_path` and `blocker_reason` for
recoverable PR/branch issues; this path also requires approval. Use
`needs_user_action` with `blocker_reason` for external blockers."""


def ci_prompt(profile: ProjectProfile) -> str:
    return f"""Monitor CI for {{{{.TaskShortId}}}} without editing files.

{context_instruction(profile, "delivery", "ci_monitor", "delivery")}

PR: {{{{.Params.pr_url}}}}
Branch: {{{{.Params.branch_name}}}}
Resolved merge strategy: {{{{.Params.merge_strategy}}}}
Workspace: {{{{.Params.workspace_path}}}}
Deterministic CI report: {{{{.Params.ci_report}}}}

{procedure_instruction(profile, "ci")}

Apply the `ci-monitor` role contract to this exact terminal watcher report.
Do not start another polling loop or ask for approval merely to wait. Re-read
only the exact PR/run/job metadata and bounded failed-job logs. If the role's
bounded exact-job retry policy applies, perform one permitted retry and choose
`watch_ci` with `workspace_path`, `pr_url`, `branch_name`, and
`merge_strategy` plus the unchanged accumulated `ci_report`; the deterministic
watcher owns the wait and appends the new terminal observation. Preserve every
attempt and failure fingerprint in `ci_report`.

Query authoritative PR merge state before classifying any failed or late check.
If the PR is already merged, never route the merged task branch to Fix. Complete
with `pr_merged` and provide `workspace_path`, `pr_url`, `branch_name`, and a
`merge_report` that includes merge proof plus the late CI state. Any actionable
post-merge regression belongs in a separate follow-up task.

While the PR remains open, complete with `waiting_pr` only when all required
checks are green and the resolved method remains feasible. Provide
`workspace_path`, `pr_url`, `branch_name`, `merge_strategy`, and `ci_report`.
Use `needs_changes` with `workspace_path` and `fix_context` only for a proven
task-differential code or history failure. After retry exhaustion, use
`needs_user_action` only for a real external blocker or decision.
Never use `needs_user_action` merely because CI is still running."""


def waiting_pr_prompt(profile: ProjectProfile) -> str:
    ci_recheck = (
        """If the PR head changed or required checks restarted, choose
`ci_required` and provide `workspace_path`, `pr_url`, `branch_name`, and
`merge_strategy`; the CI node will watch the new exact run."""
        if profile.capability("ci_monitoring")
        else """If the PR head changed, revalidate the available checks and
method in this node before starting another deterministic merge watch."""
    )
    return f"""Check delivery state for {{{{.TaskShortId}}}}.

{context_instruction(profile, "delivery", "waiting_pr", "delivery")}

PR: {{{{.Params.pr_url}}}}
Branch: {{{{.Params.branch_name}}}}
Resolved merge strategy: {{{{.Params.merge_strategy}}}}
Workspace: {{{{.Params.workspace_path}}}}

{procedure_instruction(profile, "waiting_pr")}

Apply the `ci-monitor` role contract. Do not merge or push. Revalidate the
resolved method using method-specific evidence; investigate contradictory
source-control or user-reported feasibility without mutating the task branch.

{ci_recheck}

Choose `pr_merged` only when the source-control system conclusively reports the
PR as merged; provide `workspace_path`, `pr_url`, `branch_name`, and
`merge_report`. If it remains open, required checks are green, no changes are
requested, and the selected method is feasible, choose `watch_merge` and
provide `workspace_path`, `pr_url`, `branch_name`, `merge_strategy`, and the
exact current head and base commits as `pr_head_oid` and `pr_base_oid`. The
deterministic watcher waits without an approval or model turn and wakes this
node only after meaningful state changes. A base OID change wakes this node so
method-specific feasibility can be revalidated through the normal merge-policy
contract.

Use `needs_user_action` only for authentication/access failure, ambiguous or
contradictory merge policy, or another actual human decision.
Merely waiting for review or merge is not a blocker. Use
`needs_changes` with `workspace_path`, `merge_strategy`, and `pr_report` when
task code or history must change. Choose `close_without_merge` only when the
latest user comment explicitly approves closing or canceling this PR; provide
`workspace_path`, `pr_report`, and `closure_reason`."""


def waiting_pr_changed_prompt(profile: ProjectProfile) -> str:
    return (
        waiting_pr_prompt(profile)
        + """

The deterministic watcher stopped because PR state changed:
{{.Params.pr_report}}

Classify only that fresh state. Do not repeat passive polling in the agent."""
    )


def cleanup_prompt(
    profile: ProjectProfile,
    *,
    merged: bool = False,
    no_pr: bool = False,
    closed: bool = False,
) -> str:
    workspace = "Workspace: {{.Params.workspace_path}}"
    if merged:
        cleanup_mode = "merged"
        context = """PR: {{.Params.pr_url}}
Branch: {{.Params.branch_name}}
Merge proof: {{.Params.merge_report}}"""
    elif no_pr:
        cleanup_mode = "no_pr"
        workspace = "Use the current task execution root as the workspace."
        context = "PR not applicable: {{.Params.pr_report}}"
    elif closed:
        cleanup_mode = "closed_without_merge"
        context = """PR closure report: {{.Params.pr_report}}
Closure reason: {{.Params.closure_reason}}"""
    else:
        cleanup_mode = "report_only"
        context = "Delivery context: {{.Params.review_context}}"
    if profile.capability("managed_worktrees"):
        completion = f"""Do not remove the managed worktree or local branch
inside this agent session. The following deterministic Task Janitor runs only
after this resource-owning Cleanup session exits. It may remove an exact
task-owned clean managed worktree and local branch when recoverability is
proven. For `merged`, it may also delete the same-repository remote task branch
only when GitHub reports the exact branch head in the merged PR and the remote
head has not changed.

Before `run_janitor`, close or terminate every task-owned background shell or
kept-open tool session, then run `kent worktree leave` from this Cleanup
session. Treat a failed leave request as an infrastructure blocker. The
Janitor will refuse deletion while this session still targets the task
worktree.

Complete with `run_janitor` and provide:

- canonical `workspace_path`;
- `task_short_id` as `{{{{.TaskShortId}}}}`;
- `branch_name` as the exact non-empty output of
  `git branch --show-current`, including `no_pr` and `report_only`; never use
  `null`, `none`, `not-applicable`, an empty value, or the Kent task ID by
  inference;
- `pr_url` and `merge_report`, using the literal `not-applicable` when absent;
- `cleanup_mode` as `{cleanup_mode}`;
- `cleanup_session_id` from the current `KENT_SESSION_ID`;
- a non-empty `cleanup_report` describing preflight and preserved resources.

Use `needs_user_action` only when even conservative preservation requires a
human decision."""
    else:
        completion = """Complete with `done` and provide `cleanup_report`
describing performed and skipped actions. Use `needs_user_action` with
`blocker_reason` when safe cleanup requires a human decision."""
    return f"""Perform conservative cleanup for {{{{.TaskShortId}}}}.

{context_instruction(profile, "delivery", "cleanup", "delivery")}

{workspace}
{context}

{procedure_instruction(profile, "cleanup")}

Treat cleanup as report-first. Never delete the primary checkout, dirty or
ambiguous state, or content not proven recoverable.

{completion}"""


def package_publish_prompt(profile: ProjectProfile) -> str:
    return f"""Publish the package for {{{{.TaskShortId}}}} only after this
incoming approval.

{context_instruction(profile, "delivery", "publish_package", "delivery")}

This dedicated node is the separate explicit user-approved release procedure
required by the project contract. Approval authorizes only the exact package
identity, version, destination, merged source, and tag policy declared in the
human-authored task body or an exact human-authored task comment. It does not
authorize source edits, another version, branch pushes, package deletion, or
any unrelated release action.

Task body:
{{{{.TaskBody}}}}

Merged delivery context:
- Workspace: {{{{.Params.workspace_path}}}}
- PR: {{{{.Params.pr_url}}}}
- Branch: {{{{.Params.branch_name}}}}
- Merge proof: {{{{.Params.merge_report}}}}

{procedure_instruction(profile, "publish")}

Before any remote mutation:

1. Re-read repository instructions, the project contract, and latest human
   task comments.
2. Verify the source-control system reports the PR as merged and resolve the
   exact merged commit.
3. Require explicit publication authority naming the package, exact version,
   version override mechanism, destination, and tag policy. Missing or
   contradictory authority is `needs_user_action`.
4. Use a clean checkout representing the exact merged source tree. Never
   publish unmerged, dirty, or tree-divergent code, and never rewrite the
   retained task worktree to manufacture that state.
5. Validate generated publication metadata before mutation.
6. Resolve the project-declared credential source just in time. Verify its
   principal and required registry access without printing it. Do not rely on
   ambient CLI authentication or inherited credential variables.
7. Check whether the exact remote package version already exists. Never
   overwrite or delete a colliding version.

Run only the project-authorized remote publish command. Never use a local
repository substitute for a required remote publication. Do not expose
credentials in output, task comments, artifacts, or logs. Inject the resolved
credential only into the publish subprocess using the project-declared
environment mapping, then clear it.

After publication, verify the exact expected package versions through the
remote package registry. Record package names, version, merged commit, command
shape with secrets omitted, and verification evidence in
`publication_report`.

Complete with `published` and provide `workspace_path`, `pr_url`,
`branch_name`, `merge_report`, and `publication_report`. Complete with
`needs_user_action` for authentication failure, version collision, partial
publication, source mismatch, missing authority, or unverifiable remote state;
preserve the same delivery context and provide `blocker_reason`. A retry must
re-check remote state before another publish attempt."""


def package_publish_recovery_prompt(profile: ProjectProfile) -> str:
    return f"""Resume the approval-gated package publication for
{{{{.TaskShortId}}}}.

{context_instruction(profile, "delivery", "publish_package", "delivery")}

Previous blocker: {{{{.Params.blocker_reason}}}}
Workspace: {{{{.Params.workspace_path}}}}
PR: {{{{.Params.pr_url}}}}
Branch: {{{{.Params.branch_name}}}}
Merge proof: {{{{.Params.merge_report}}}}

Re-read the task body, latest human comments, repository instructions, and the
project contract.

{procedure_instruction(profile, "publish")}

Approval authorizes a fresh remote-state check and, only when safe, one attempt
to publish the exact already-approved package identity. It does not authorize
another version, source edits, tags beyond the declared policy, package
deletion, or overwrite.

Before any publish command, re-resolve the project-declared credential source,
verify its principal and registry access without exposing it, and do not
substitute ambient CLI authentication. Inspect the remote package registry
again for partial or completed publication and verify the clean merged source
identity.
If the package is already completely present with task-owned proof, complete as
`published` without republishing. If state is partial, conflicting, or
unverifiable, return `needs_user_action` again. Never hide a partial remote
mutation. Inject the credential only into the authorized publish subprocess
and clear it afterward.

On success, provide `workspace_path`, `pr_url`, `branch_name`, `merge_report`,
and `publication_report`. On another blocker, preserve the same delivery
context and provide `blocker_reason`."""


def published_cleanup_prompt(profile: ProjectProfile) -> str:
    if not profile.capability("managed_worktrees"):
        return f"""Perform conservative cleanup for {{{{.TaskShortId}}}} only
after successful package publication.

{context_instruction(profile, "delivery", "cleanup", "delivery")}

Workspace: {{{{.Params.workspace_path}}}}
PR: {{{{.Params.pr_url}}}}
Branch: {{{{.Params.branch_name}}}}
Merge proof: {{{{.Params.merge_report}}}}
Publication proof: {{{{.Params.publication_report}}}}

{procedure_instruction(profile, "cleanup")}

Require a non-empty publication report proving the exact task-authorized
package version exists in the remote registry. Do not publish, tag, push, or
edit project files from Cleanup. Treat cleanup as report-first and preserve
dirty, primary, ambiguous, or unrecoverable state.

Complete with `done` and provide a non-empty `cleanup_report`. Use
`needs_user_action` with `blocker_reason` only when safe cleanup requires a
human decision."""

    return f"""Perform conservative cleanup for {{{{.TaskShortId}}}} only after
successful package publication.

{context_instruction(profile, "delivery", "cleanup", "delivery")}

Workspace: {{{{.Params.workspace_path}}}}
PR: {{{{.Params.pr_url}}}}
Branch: {{{{.Params.branch_name}}}}
Merge proof: {{{{.Params.merge_report}}}}
Publication proof: {{{{.Params.publication_report}}}}

{procedure_instruction(profile, "cleanup")}

Require a non-empty publication report proving the exact task-authorized
package version exists in the remote registry. Do not publish, tag, push, or
edit project files from Cleanup.

Treat cleanup as report-first. Never delete the primary checkout, dirty or
ambiguous state, or content not proven recoverable. Do not remove the managed
worktree or local branch inside this agent session. The deterministic Task
Janitor runs only after this resource-owning Cleanup session exits.

Close every task-owned background shell or kept-open tool session and run
`kent worktree leave` from this Cleanup session before `run_janitor`. The
Janitor must observe this session outside the task worktree before deletion.

Complete with `run_janitor` and provide canonical `workspace_path`;
`task_short_id` as `{{{{.TaskShortId}}}}`; `pr_url`; `branch_name`;
`merge_report` including the publication proof; `cleanup_mode` as `merged`;
`cleanup_session_id` from `KENT_SESSION_ID`; and a non-empty `cleanup_report`.
Use `needs_user_action` only when conservative preservation requires a human
decision."""


def janitor_recovery_prompt(profile: ProjectProfile) -> str:
    return f"""Recover the task cleanup after deterministic Janitor failure.

{context_instruction(profile, "delivery", "cleanup", "delivery")}

Previous cleanup report:
{{{{.Params.cleanup_report}}}}
Blocker:
{{{{.Params.blocker_reason}}}}

{procedure_instruction(profile, "cleanup")}

Use the retained Cleanup context. Do not directly remove a Kent-managed
worktree from this agent session. Close every task-owned background shell or
kept-open tool session. If this session still targets the task worktree, run
`kent worktree leave` before retrying. If the infrastructure failure is
transient and the same safety proofs still hold, choose `run_janitor` again
with the complete canonical parameter contract. Otherwise choose
`needs_user_action` with the exact blocker. Preserve every ambiguous or unique
resource."""


def pr_recovery_fix_prompt(
    profile: ProjectProfile,
    *,
    bounded: bool = False,
) -> str:
    fresh_contract = ""
    completion_contract = """After resolving task-scoped code, complete with
`verify` and provide `workspace_path`, `task_short_id={{.TaskShortId}}`, plus
refreshed `review_context`."""
    if bounded:
        fresh_contract = """

This is a fresh bounded writer session. Inspect the preserved diff, task
comments, authoritative artifacts, and existing evidence before editing.
Resolve exactly one independently verifiable PR or branch recovery slice."""
        completion_contract = """After one slice, choose `continue_fix` with
`workspace_path` and `fix_context` containing only the remaining task-scoped
issues. Choose `verify` only when no recovery slice remains, and provide
`workspace_path`, `task_short_id={{.TaskShortId}}`, plus refreshed
`review_context`."""

    return f"""Resolve an approved PR or branch recovery issue.

{context_instruction(profile, "implement", "fix", "implementation")}

Workspace: {{{{.Params.workspace_path}}}}.
Recovery issue: {{{{.Params.blocker_reason}}}}

{procedure_instruction(profile, "fix")}
{fresh_contract}

The approval applies only to the exact reported PR/branch recovery. Never infer
permission for a broader rebase or force-push. {completion_contract}
Use `needs_user_action` with `blocker_reason` if the approved recovery is still
unsafe. Choose `wont_do` only for an explicit cancellation decision and
provide `closure_reason`."""


def pr_feedback_fix_prompt(
    profile: ProjectProfile,
    *,
    bounded: bool = False,
) -> str:
    fresh_contract = ""
    completion_contract = """Complete with `verify` and provide
`workspace_path`, `task_short_id={{.TaskShortId}}`, plus refreshed
`review_context`."""
    if bounded:
        fresh_contract = """

This is a fresh bounded writer session. Inspect the preserved diff, task
comments, authoritative artifacts, and existing evidence before editing.
Resolve exactly one independently verifiable PR-feedback slice."""
        completion_contract = """After one slice, choose `continue_fix` with
`workspace_path` and `fix_context` containing only the remaining task-scoped
issues. Choose `verify` only when no PR-feedback slice remains, and provide
`workspace_path`, `task_short_id={{.TaskShortId}}`, plus refreshed
`review_context`."""

    return f"""Fix task-scoped PR feedback.

{context_instruction(profile, "implement", "fix", "implementation")}

Workspace: {{{{.Params.workspace_path}}}}.
Resolved merge strategy: {{{{.Params.merge_strategy}}}}.
PR report: {{{{.Params.pr_report}}}}

{procedure_instruction(profile, "fix")}
{fresh_contract}

Remain the single writer. Do not merge or push protected branches. A history
rewrite or force-push requires an exact latest user authorization naming the
branch and permitted repair. Preserve the old remote head in a local backup,
pin the expected remote head, prove the repaired final tree is byte-for-byte
identical unless the authorization explicitly permits code changes, and update
only the task branch with force-with-lease. Stop with `needs_user_action` on a
lease, tree, target-tip, or authorization mismatch; never fall back to an
unconditional force push.
{completion_contract}
Use `needs_user_action` with `blocker_reason` for external or policy blockers.
Choose `wont_do` only for an explicit cancellation decision and provide
`closure_reason`."""


def post_smoke_target(profile: ProjectProfile) -> str:
    if profile.capability("pull_requests"):
        if profile.capability("compliance_review"):
            return "compliance"
        return "prepare_pr"
    return "cleanup"


def delivery_prompt(profile: ProjectProfile, target: str) -> str:
    if target == "compliance":
        return compliance_prompt(profile)
    if target == "prepare_pr":
        return prepare_pr_prompt(profile)
    if target == "cleanup":
        return cleanup_prompt(profile)
    raise ValueError(f"unsupported delivery target {target!r}")


def smoke_decision_instruction(profile: ProjectProfile) -> str:
    procedure = procedure_instruction(profile, "smoke_decision")
    policy = profile.smoke_policy()
    if policy == "required":
        return f"""Project policy requires runtime Smoke after verification.
{procedure}
Choose `smoke_required` and provide `smoke_rationale` plus a focused
`smoke_scope`. Device, browser, simulator, or hardware unavailability is not a
reason to bypass Smoke; the Smoke node must report `needs_user_action`."""
    if policy == "disabled":
        return """Project policy disables runtime Smoke for this workflow.
Choose `delivery_ready` and set `smoke_rationale` to the profile policy. Do not
invent a Smoke requirement that the project cannot execute."""
    if policy == "conditional":
        return f"""Project policy makes runtime Smoke conditional.
{procedure}
Choose `smoke_required` for user-visible or runtime behavior, navigation, state
or data flow, permissions or security, storage or migrations, external
integration, browser/device/hardware interaction, explicit acceptance criteria,
or uncertain runtime impact. Provide `smoke_rationale` and a focused
`smoke_scope`.

Choose `delivery_ready` only for changes proven not to affect a runtime
artifact or user-observable behavior, and provide an evidence-based
`smoke_rationale`. Uncertainty must route to `smoke_required`. Resource
unavailability must never downgrade the decision."""
    raise ValueError(f"unsupported smoke policy {policy!r}")
