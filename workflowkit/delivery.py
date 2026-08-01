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
    "Path to the approved implementation plan, or an empty string when not applicable.",
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
    "Concrete task-scoped findings that the single writer must fix.",
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
LEGACY_STANDARDS_REPORT = ParameterSpec(
    "compliance_report",
    "Read-only repository standards and architecture compliance report.",
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
BRANCH_NAME = ParameterSpec("branch_name", "Task branch name.")
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
    fresh_writers = profile.writer_session_policy() == "fresh_per_slice"
    writer_handoff_context = (
        "new_session" if fresh_writers else "compact_and_continue_session"
    )
    implementation_continuation_context = (
        "new_session" if fresh_writers else "continue_session"
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
    implementation_parameters = (WORKSPACE, PLAN, WORK_KIND)
    nodes: list[NodeSpec] = [
        NodeSpec("backlog", "start", "Backlog"),
        agent_node("plan", "Plan", orchestrator),
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
    if profile.capability("ci_monitoring"):
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
        EdgeSpec(
            key="plan_implement",
            source="plan",
            transition="implement",
            target="implement",
            context=writer_handoff_context,
            prompt=implement_prompt(profile, fresh_session=fresh_writers),
            transition_description=(
                "Planning is complete and implementation can start without ambiguity."
            ),
            parameters=implementation_parameters,
        ),
        recovery_edge(
            "plan",
            context=non_writer_recovery_context,
        ),
        cancellation_edge("plan"),
        EdgeSpec(
            key="implement_continue",
            source="implement",
            transition="continue_implementation",
            target="implement",
            context=implementation_continuation_context,
            prompt=implement_prompt(profile, fresh_session=fresh_writers),
            transition_description=(
                "One plan step is complete; continue with the next ready step."
            ),
            parameters=implementation_parameters,
        ),
        EdgeSpec(
            key="implement_verify",
            source="implement",
            transition="verify",
            target="verification_dispatch",
            transition_description=(
                "Implementation is complete; normalize inputs for read-only verification."
            ),
            parameters=(WORKSPACE, REVIEW_CONTEXT),
        ),
        recovery_edge(
            "implement",
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
            target="verification_dispatch",
            transition_description=(
                "Task-scoped fixes are complete; rerun every verification branch."
            ),
            parameters=(WORKSPACE, REVIEW_CONTEXT),
        ),
        recovery_edge(
            "fix",
            context=writer_recovery_context,
            fresh_session=fresh_writers,
        ),
        cancellation_edge("fix"),
    ]

    if fresh_writers:
        edges.append(
            EdgeSpec(
                key="fix_continue",
                source="fix",
                transition="continue_fix",
                target="fix",
                context="new_session",
                prompt=fix_prompt(profile, bounded=True),
                transition_description=(
                    "One bounded fix slice is complete; continue with only the "
                    "remaining task-scoped findings."
                ),
                parameters=(WORKSPACE, FIX_CONTEXT),
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
                prompt=spec_review_prompt(),
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
                        standards_report_parameter(profile),
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
            "ci_monitor" if profile.capability("ci_monitoring") else "waiting_pr"
        )
        edges.extend(
            [
                EdgeSpec(
                    key=f"prepare_pr_{created_target}",
                    source="prepare_pr",
                    transition="monitor_ci",
                    target=created_target,
                    prompt=(
                        ci_prompt(profile)
                        if created_target == "ci_monitor"
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
                        key="ci_monitor_merged",
                        source="ci_monitor",
                        transition="pr_merged",
                        target="cleanup",
                        prompt=cleanup_prompt(profile, merged=True),
                        transition_description=(
                            "The pull request merged before CI observation "
                            "completed; preserve late check state and clean up."
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
                    ),
                ]
            )

        edges.extend(
            [
                EdgeSpec(
                    key="fix_pr_merged_cleanup",
                    source="fix",
                    transition="pr_merged",
                    target="cleanup",
                    context="new_session",
                    prompt=cleanup_prompt(profile, merged=True),
                    transition_description=(
                        "Recovery only: the pull request is already merged; "
                        "skip obsolete Fix work and clean up."
                    ),
                    parameters=(WORKSPACE, PR_URL, BRANCH_NAME, MERGE_REPORT),
                ),
                EdgeSpec(
                    key="waiting_pr_cleanup",
                    source="waiting_pr",
                    transition="pr_merged",
                    target="cleanup",
                    prompt=cleanup_prompt(profile, merged=True),
                    transition_description=(
                        "The pull request is confirmed merged; perform conservative cleanup."
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
                    target="cleanup",
                    prompt=cleanup_prompt(profile, merged=True),
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
                    target="ci_monitor",
                    context="new_session",
                    prompt=ci_prompt(profile),
                    transition_description=(
                        "The PR head changed or required checks restarted; "
                        "monitor the new exact CI state."
                    ),
                    parameters=(WORKSPACE, PR_URL, BRANCH_NAME, MERGE_STRATEGY),
                )
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


def recovery_edge(
    node_key: str,
    *,
    context: str = "compact_and_continue_session",
    fresh_session: bool = False,
    extra_parameters: tuple[ParameterSpec, ...] = (),
    extra_prompt: str = "",
) -> EdgeSpec:
    extra_contract = f"\n{extra_prompt}" if extra_prompt else ""
    cancellation_contract = ""
    if node_key in {
        "plan",
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

Read the task body, current task comments, project instructions, authoritative
task artifacts, preserved worktree diff, and existing evidence before editing
or repeating checks. If user feedback changed a product decision or acceptance
criterion, update the authoritative design/specification/plan first and
reference the exact task-comment ID. Do not restart completed work.
{stage_contract}

Verify that the exact blocker is resolved. Do not infer approval for any broader
or destructive action.{extra_contract}"""
            + cancellation_contract
        )
    else:
        prompt = (
            f"""Resume the `{node_key}` stage after user action.

Previous blocker: {{{{.Params.blocker_reason}}}}

Use the retained compacted context, re-read current task comments and project
instructions, verify that the blocker is actually resolved, and continue the
same stage. Do not infer approval for any broader or destructive
action.{extra_contract}"""
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
            "Work is externally blocked; continue this stage only after approval."
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


def checkpoint_instruction(profile: ProjectProfile, stage: str) -> str:
    stage_details = {
        "fix": (
            "Record the pinned baseline, supplied findings, completed fix "
            "slices, fresh green checks, remaining findings, mutation ledger, "
            "and one next permitted action."
        ),
        "smoke": (
            "Record acceptance stages, lock resource/token state, exact "
            "runtime target, completed build/install/launch work, sanitized "
            "evidence paths, remaining scenarios, external-side-effect "
            "ledger, restoration state, and one next permitted action."
        ),
    }[stage]
    return f"""Use `{profile.command("checkpoint")}` for a durable ignored
`{stage}` checkpoint at
`.kent/runtime/{{{{.TaskShortId}}}}/{stage}-checkpoint.json`.

Before repeating work, run its `validate` and `read` commands when the file
exists, reconcile the checkpoint with current Git/task/device state, and resume
only its `next_action`. Before the first expensive or mutating action and after
every completed bounded stage, pipe one JSON object to its `write` command.
Every checkpoint contains arrays named `completed`, `remaining`, and
`mutation_ledger`, plus a non-empty `next_action`; the helper supplies task,
stage, workspace, schema, and timestamp fields. {stage_details}

Persist the latest checkpoint before every workflow transition, including a
blocker or finding. Never put credentials, authenticated UI content, raw logs,
or broad device data in it. Use these exact command forms:

`{profile.command("checkpoint")} validate --stage {stage} --task {{{{.TaskShortId}}}} --workspace <workspace>`

`{profile.command("checkpoint")} read --stage {stage} --task {{{{.TaskShortId}}}} --workspace <workspace>`

`{profile.command("checkpoint")} write --stage {stage} --task {{{{.TaskShortId}}}} --workspace <workspace>`"""


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

Read .kent/project-contract.md, .kent/workflow-profile.toml, and repository
instructions first.

{work_kind_plan_instruction(profile)}

Task body:
{{{{.TaskBody}}}}

Keep discovery, design/spec ingestion, decisions, and implementation planning in
this one Plan session. Ask questions when a product decision is required. Do not
invoke nested prompt flows and do not implement production changes.
Any design, specification, or plan that narrows, replaces, or claims to
supersede the task body must cite the exact human-authored task-comment ID or
another explicit authoritative source. Agent-authored comments, implementation
inference, and unsupported claims that "the user clarified" are not product
authority. Use `needs_user_action` before implementation when that provenance
is absent.
{recovery_contract}

Complete with `implement` only when the plan has no unresolved product, API, UX,
or safety ambiguity. `workspace_path` is the repository or managed-worktree
root; it is never `.todo/<feature>` or another artifact directory. Provide that
root plus `plan_path` and the selected `work_kind`; use an empty `plan_path`
only when the project contract explicitly allows planless work.
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

Read .kent/project-contract.md, .kent/workflow-profile.toml, repository
instructions, and the plan at {{{{.Params.plan_path}}}}. Workspace:
{{{{.Params.workspace_path}}}}.

{work_kind_implement_instruction(profile)}

Use the project procedure for step selection, recipes, editing, focused checks,
and plan progress. This generated workflow's completion contract overrides any
legacy procedure transition names such as `audit`.
{fresh_contract}

Act as the single writer and implement exactly one ready writer-owned plan step
per node run. Writer-owned steps change code, tests, configuration,
documentation, or run their deterministic checks. Runtime Smoke and other
workflow-owned review or delivery items are downstream scope, even when a
legacy plan accidentally renders them as unchecked checklist entries. Do not
acquire a device, build/install for Smoke, execute those stages, or mark their
items complete.
Do not launch nested final Standards, Specification, Compliance, or
project-specialized review passes; the generated verification graph owns those
independent reviews. Bounded implementation, research, and diagnosis
delegation remains allowed.
After marking that step complete, choose `continue_implementation` with
`workspace_path`, `plan_path`, and the unchanged `work_kind` when unchecked
writer-owned ready steps remain. Choose
`verify` when every writer-owned plan step is complete; provide
`workspace_path` plus `review_context` summarizing plan/spec paths, the fixed
comparison point, changed files, checks, risks, and any downstream runtime
acceptance scope for the read-only branches and Gate.
Use `needs_user_action` only for an external blocker and provide
`blocker_reason` plus the unchanged `work_kind`. Choose `wont_do` only for
explicit cancellation and provide `closure_reason`."""


def fix_prompt(
    profile: ProjectProfile,
    *,
    bounded: bool = False,
) -> str:
    bounded_contract = ""
    completion_contract = """
Complete with `verify` and provide `workspace_path` plus a refreshed
`review_context` containing the findings, fixes, changed files, artifact paths,
and focused checks."""
    if bounded:
        bounded_contract = """

This is a fresh bounded writer session. Read exact task-comment IDs referenced
by the findings and inspect the preserved diff, authoritative artifacts, and
existing evidence before editing. If feedback changes a product decision or
acceptance criterion, update the authoritative design/specification/plan first
and reference the comment ID. Do not redo completed work.

Apply exactly one independently verifiable fix slice and update the
authoritative fix checklist."""
        completion_contract = """
After one slice, choose `continue_fix` with `workspace_path` and a refreshed
`fix_context` containing only the remaining findings. Choose `verify` only when
no fix slice remains, and provide `workspace_path` plus a refreshed
`review_context` containing the findings, fixes, changed files, artifact paths,
and focused checks."""
    checkpoint = checkpoint_instruction(profile, "fix")

    return f"""Apply task-scoped fixes for {{{{.TaskShortId}}}}.

Read .kent/project-contract.md and repository instructions first. Workspace:
{{{{.Params.workspace_path}}}}. Findings:
{{{{.Params.fix_context}}}}

{procedure_instruction(profile, "fix")}
{bounded_contract}

{checkpoint}

Remain the single writer. Fix root causes without broadening product scope.
Before editing, require each supplied finding to identify a concrete
task-introduced or task-worsened violation. A whole-repository analyzer failure,
a finding that also exists on the pinned baseline, or an unproven differential
is not authorization for cleanup. If the finding requires baseline-wide work
or only exposes contradictory repository policy, do not edit; choose
`needs_user_action` with the exact policy/evidence blocker.
Do not launch nested final Standards, Specification, Compliance, or
project-specialized review passes; return to the generated verification graph
after fixing the supplied findings.
{completion_contract}
Use `needs_user_action` only for an external blocker and provide
`blocker_reason`. Choose `wont_do` only for explicit cancellation and provide
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

Complete with `verify` and provide the canonical root as `workspace_path` plus
the preserved `review_context`. Use `needs_user_action` only if Kent's execution
root itself is unavailable or ambiguous. Choose `wont_do` only for explicit
cancellation and provide `closure_reason`."""


def standards_review_prompt(profile: ProjectProfile) -> str:
    report_key = (
        "compliance_report"
        if profile.legacy_review_contract
        else "standards_report"
    )
    prompt = """Run an independent read-only repository standards review.

Read AGENTS.md and .kent/project-contract.md first. Workspace:
{{.Params.workspace_path}}. Review context:
{{.Params.review_context}}

Inspect the change against repository architecture, engineering rules, security,
and maintainability constraints. Do not edit files and do not run destructive
commands. Findings are data for Join, not a routing decision.

Pin the immutable task baseline named by the review context or repository
contract. Use the task fixed point or Kent-resolved execution commit for
task-delta review; do not substitute a newer merge-target tip. Target-only
commits added after task start are integration inputs, not task regressions.
Classify them only when a three-way merge or method-specific replay proves a
conflict or delivered-tree loss. Never request copying unrelated target files
into the task diff merely because the task checkout predates them.
For a whole-repository analyzer or quality gate that fails on the candidate,
establish whether the same command or equivalent machine-readable findings fail
on that baseline. A changed file or touched method is not proof that an analyzer
finding is new.

Use `needs_changes` only for concrete task-introduced or task-worsened
violations, with the rule, path, and differential evidence. For metric findings,
`worsened` means the same rule/path/declaration has a larger measured value.
For non-metric findings, it means a new normalized declaration signature or a
larger occurrence count. Line shifts do not count, and an improved total does
not waive an individual worsened finding. If the baseline has the same failure
and no task delta is proven, report the baseline debt as non-blocking and use
`passed` for the task-scoped standards result. If an explicit project rule
requires an absolutely clean repository independent of baseline but the
baseline itself violates that rule, use `blocked` and identify the
repository-policy contradiction; never route baseline-wide cleanup to Fix.

Complete only with `reported`. Provide `standards_status` as exactly `passed`,
`needs_changes`, or `blocked`, plus `__REPORT_KEY__` with evidence and
path-specific findings."""
    return prompt.replace("__REPORT_KEY__", report_key)


def spec_review_prompt() -> str:
    return """Run an independent read-only specification review.

Read the task body, plan/spec artifacts named in the review context, and
.kent/project-contract.md. Workspace:
{{.Params.workspace_path}}. Review context:
{{.Params.review_context}}

Check acceptance criteria, product behavior, edge cases, and scope fidelity
independently from repository standards. Do not edit files. Findings are data
for Join, not a routing decision.

A design, specification, or plan may narrow or supersede the task body only
when it cites the exact human-authored task-comment ID or another explicit
authoritative source. Agent-authored summaries and unsupported claims that
"the user clarified" are not authority. Report missing provenance as
`blocked`; do not accept the narrowed scope or route it to writer Fix.

Use the task fixed point or Kent-resolved execution commit for task-delta scope,
not a newer merge-target tip. Missing target-only commits in an older task
checkout are not specification regressions unless a three-way merge or
method-specific replay proves delivered-tree loss. Do not request copying
unrelated target files into the task diff.

Complete only with `reported`. Provide `spec_status` as exactly `passed`,
`needs_changes`, or `blocked`, plus `review_report` with evidence and concrete
gaps."""


def verification_gate_prompt(profile: ProjectProfile) -> str:
    if profile.capability("standards_review"):
        standards_report = (
            "Standards report: {{.Params.compliance_report}}"
            if profile.legacy_review_contract
            else "Standards report: {{.Params.standards_report}}"
        )
        standards = (
            "Standards status: {{.Params.standards_status}}\n"
            + standards_report
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

Workspace: {{{{.Params.fanout_verify.workspace_path}}}}
Review context: {{{{.Params.fanout_verify.review_context}}}}
Verification status: {{{{.Params.verification_status}}}}
Verification report: {{{{.Params.verification_report}}}}
{standards}
{spec}

Choose a delivery transition only when every enabled status is `passed`.
Choose `needs_changes` only when a report proves concrete task-introduced or task-worsened findings.
Do not convert a whole-repository analyzer failure,
pre-existing baseline debt, or an unproven differential into writer work.
Do not convert target-only commits missing from an older task checkout into
writer work without a proven merge/replay conflict or delivered-tree loss.
Repository-policy contradictions and unavailable mandatory baseline evidence
route to `needs_user_action`, not Fix.
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

Read .kent/project-contract.md and repository instructions first. Workspace:
{{{{.Params.workspace_path}}}}. Review context:
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

Read all applicable AGENTS.md files, .kent/project-contract.md, the task body,
human-authored task comments, and the plan/spec sources named in the review
context. Workspace: {{{{.Params.workspace_path}}}}. Final review context:
{{{{.Params.review_context}}}}

{procedure_instruction(profile, "compliance")}

This is a thin final attestation, not another general code, architecture,
specification, or runtime review. Verify the authority hierarchy and final
worktree diff; confirm that {evidence_chain}, Gate, and any required Smoke
completed with adequate evidence; confirm that a Smoke bypass has a concrete
rationale; and check that no unresolved blocker, approval, unauthorized
rule/spec change, or workflow obligation remains. Do not edit files or perform
state-changing actions.

A specification or plan may narrow or supersede the task body only with an
exact human-authored task-comment ID or another explicit authoritative source.
Agent-authored summaries and unsupported claims that "the user clarified" are
not product authority.

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

Read .kent/project-contract.md and repository instructions first. Workspace:
{{{{.Params.workspace_path}}}}. Final review context:
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

Workspace: {{{{.Params.workspace_path}}}}
Updated review context: {{{{.Params.review_context}}}}

{procedure_instruction(profile, "compliance")}

Use the retained Compliance context. Inspect only the repaired evidence
artifacts and confirm they now agree with the already-passed substantive
results. Do not repeat code, architecture, specification, build, or runtime
review.

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

Read .kent/project-contract.md and repository instructions first. Workspace:
{{{{.Params.workspace_path}}}}. Review context:
{{{{.Params.review_context}}}}
{compliance_context}

{procedure_instruction(profile, "ship")}

This workflow explicitly authorizes committing the task changes, pushing only
the current task branch, and creating or updating its pull request. It never
authorizes merging, pushing protected branches, or broadening scope.

Configured PR merge policy: `{merge_policy}`.

Resolve the merge strategy once before publishing. Supported resolved values
are `merge`, `squash`, and `rebase`. For `auto`, query the source-control
system's repository capabilities, target-branch protection/rulesets, and any
required merge-queue method. On GitHub this includes the allowed merge methods,
`required_linear_history`, merge-queue configuration when applicable, and the
target PR's method-specific state. Serialize the GitHub evidence as
`repository`, `branch_protection`, applicable `rulesets`, and `merge_queue`,
then run:

`~/.kent/bin/kent-resolve-github-merge-strategy --policy {merge_policy}`

The adapter's structured result is authoritative for strategy selection.
Continue only when it returns `outcome=resolved`; for
`outcome=needs_user_action`, preserve its code, candidates, and reason instead
of manually choosing. An explicit policy still must be enabled and compatible
with target-branch rules.

Validate the selected method, not merely generic mergeability:

- `merge` requires merge commits to be enabled, permitted by branch rules, and
  a clean final-tree merge;
- `squash` requires squash merging to be enabled and a clean final-tree merge;
- `rebase` requires rebase merging to be enabled and the exact branch commits
  to replay cleanly onto the current target branch. On GitHub, query
  `canBeRebased`; `mergeable=MERGEABLE` or `mergeStateStatus=CLEAN` alone does
  not prove rebase feasibility.

Before an initial rebase-strategy push, test the prospective history in an
isolated temporary clone or branch with a forced replay onto the fresh target
tip. Do not mutate the task branch while diagnosing. A clean merge-tree or
target-ancestor check is not a substitute for replaying the commits. After
creating or updating the PR, requery method-specific feasibility. Route a
task-history conflict to `needs_changes`; never dismiss a user's rebase-conflict
report using generic mergeability.

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

PR: {{{{.Params.pr_url}}}}
Branch: {{{{.Params.branch_name}}}}
Resolved merge strategy: {{{{.Params.merge_strategy}}}}
Workspace: {{{{.Params.workspace_path}}}}

{procedure_instruction(profile, "ci")}

Use the project source-control adapter. Never merge or push. Pending, queued, or
in-progress checks are not workflow outcomes and must not produce
`needs_user_action`. Resolve the exact PR/run identity, then use one blocking
first-party watcher instead of one model turn per poll. On GitHub prefer
`gh pr checks <pr> --watch --interval 30`; when monitoring an exact Actions run,
use `gh run watch <run-id> --exit-status --interval 30`. After the watcher exits,
re-read authoritative PR, check, and run state before choosing a transition.
"Bounded" monitoring means bounded identity, interval, and log scope, not an
arbitrary wall-clock cutoff while CI is still running.

Query authoritative PR merge state before classifying any failed or late check.
If the PR is already merged, never route the merged task branch to Fix. Complete
with `pr_merged` and provide `workspace_path`, `pr_url`, `branch_name`, and a
`merge_report` that includes merge proof plus the late CI state. Any actionable
post-merge regression belongs in a separate follow-up task.

While the PR remains open, revalidate the resolved method against current
repository capabilities, target branch rules, merge queue, and PR state. For
GitHub rebase delivery,
`canBeRebased=true` is required; generic `MERGEABLE/CLEAN` is insufficient.
Complete with `waiting_pr` only when all required checks are conclusively green
and the selected method remains feasible. Provide `workspace_path`, `pr_url`,
`branch_name`, `merge_strategy`, and `ci_report`. Use `needs_changes` with
`workspace_path` and `fix_context` only when task-differential evidence proves
the task introduced or worsened a code or history failure. Baseline, flaky,
unrelated, or unattributed failures use `needs_user_action` with
`blocker_reason`, as do external failures, policy ambiguity, or access
problems. Never use `needs_user_action` merely because CI is still running."""


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

PR: {{{{.Params.pr_url}}}}
Branch: {{{{.Params.branch_name}}}}
Resolved merge strategy: {{{{.Params.merge_strategy}}}}
Workspace: {{{{.Params.workspace_path}}}}

{procedure_instruction(profile, "waiting_pr")}

Do not merge or push. Revalidate the resolved method independently from generic
mergeability. For GitHub `rebase`, require `canBeRebased=true`;
`mergeable=MERGEABLE`, `mergeStateStatus=CLEAN`, a clean merge-tree, or proof
that the target branch is already an ancestor does not establish replay
feasibility. For `merge` and `squash`, verify that the selected method remains
enabled, allowed by branch rules or the merge queue, and final-tree mergeable.

Treat a user report that the selected merge method is blocked as evidence to
investigate, not as a claim to dismiss. If source-control signals disagree,
reproduce a rebase failure only in an isolated temporary clone or branch using
a forced replay onto the fresh target tip. Do not mutate the task branch during
diagnosis.

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

Complete with `run_janitor` and provide:

- canonical `workspace_path`;
- `task_short_id` as `{{{{.TaskShortId}}}}`;
- `pr_url`, `branch_name`, and `merge_report`, using empty strings when absent;
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

{workspace}
{context}

{procedure_instruction(profile, "cleanup")}

Treat cleanup as report-first. Never delete the primary checkout, dirty or
ambiguous state, or content not proven recoverable.

{completion}"""


def janitor_recovery_prompt(profile: ProjectProfile) -> str:
    return f"""Recover the task cleanup after deterministic Janitor failure.

Previous cleanup report:
{{{{.Params.cleanup_report}}}}
Blocker:
{{{{.Params.blocker_reason}}}}

{procedure_instruction(profile, "cleanup")}

Use the retained Cleanup context. Do not directly remove a Kent-managed
worktree from this agent session. If the infrastructure failure is transient
and the same safety proofs still hold, choose `run_janitor` again with the
complete canonical parameter contract. Otherwise choose `needs_user_action`
with the exact blocker. Preserve every ambiguous or unique resource."""


def pr_recovery_fix_prompt(
    profile: ProjectProfile,
    *,
    bounded: bool = False,
) -> str:
    fresh_contract = ""
    completion_contract = """After resolving task-scoped code, complete with
`verify` and provide `workspace_path` plus refreshed `review_context`."""
    if bounded:
        fresh_contract = """

This is a fresh bounded writer session. Inspect the preserved diff, task
comments, authoritative artifacts, and existing evidence before editing.
Resolve exactly one independently verifiable PR or branch recovery slice."""
        completion_contract = """After one slice, choose `continue_fix` with
`workspace_path` and `fix_context` containing only the remaining task-scoped
issues. Choose `verify` only when no recovery slice remains, and provide
`workspace_path` plus refreshed `review_context`."""

    return f"""Resolve an approved PR or branch recovery issue.

Read .kent/project-contract.md, repository instructions, and latest task
comments first. Workspace: {{{{.Params.workspace_path}}}}.
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
`workspace_path` plus refreshed `review_context`."""
    if bounded:
        fresh_contract = """

This is a fresh bounded writer session. Inspect the preserved diff, task
comments, authoritative artifacts, and existing evidence before editing.
Resolve exactly one independently verifiable PR-feedback slice."""
        completion_contract = """After one slice, choose `continue_fix` with
`workspace_path` and `fix_context` containing only the remaining task-scoped
issues. Choose `verify` only when no PR-feedback slice remains, and provide
`workspace_path` plus refreshed `review_context`."""

    return f"""Fix task-scoped PR feedback.

Read .kent/project-contract.md, repository instructions, and latest task
comments first. Workspace: {{{{.Params.workspace_path}}}}.
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


def standards_report_parameter(profile: ProjectProfile) -> ParameterSpec:
    if profile.legacy_review_contract:
        return LEGACY_STANDARDS_REPORT
    return STANDARDS_REPORT


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
