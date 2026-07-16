from __future__ import annotations

from .model import EdgeSpec, NodeSpec, ParameterSpec, WorkflowSpec
from .profile import ProjectProfile


WORKSPACE = ParameterSpec(
    "workspace_path",
    "Path to the task workspace or managed worktree.",
)
PLAN = ParameterSpec(
    "plan_path",
    "Path to the approved implementation plan, or an empty string when not applicable.",
)
REVIEW_CONTEXT = ParameterSpec(
    "review_context",
    "Implementation, fix, report, and artifact context required by verification.",
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
COMPLIANCE_REPORT = ParameterSpec(
    "compliance_report",
    "Read-only repository standards and architecture compliance report.",
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
def build_delivery_workflow(
    profile: ProjectProfile,
    version: int,
) -> WorkflowSpec:
    orchestrator = profile.role("orchestrator")
    nodes: list[NodeSpec] = [
        NodeSpec("backlog", "start", "Backlog"),
        agent_node("plan", "Plan", orchestrator),
        agent_node("implement", "Implement", orchestrator),
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
        agent_node("verification_gate", "Verification Gate", orchestrator),
        agent_node("fix", "Fix", orchestrator),
        agent_node("cleanup", "Cleanup", orchestrator),
        NodeSpec("wont_do", "terminal", "Won't Do"),
        NodeSpec("done", "terminal", "Done"),
    ]

    review_branches = ["deterministic_verify"]
    if profile.capability("compliance_review"):
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

    if profile.capability("device_smoke"):
        nodes.append(agent_node("smoke", "Smoke Test", orchestrator))
    if profile.capability("pull_requests"):
        nodes.extend(
            [
                agent_node("prepare_pr", "Prepare PR", orchestrator),
                agent_node("waiting_pr", "Waiting PR", orchestrator),
            ]
        )
    if profile.capability("ci_monitoring"):
        nodes.append(agent_node("ci_monitor", "Monitor CI", orchestrator))

    edges: list[EdgeSpec] = [
        EdgeSpec(
            key="start_plan",
            source="backlog",
            transition="start",
            target="plan",
            prompt=plan_prompt(profile),
            transition_description="Start one planning session for this task.",
        ),
        EdgeSpec(
            key="plan_implement",
            source="plan",
            transition="implement",
            target="implement",
            context="compact_and_continue_session",
            prompt=implement_prompt(profile),
            transition_description=(
                "Planning is complete and implementation can start without ambiguity."
            ),
            parameters=(WORKSPACE, PLAN),
        ),
        recovery_edge("plan"),
        cancellation_edge("plan"),
        EdgeSpec(
            key="implement_continue",
            source="implement",
            transition="continue_implementation",
            target="implement",
            context="continue_session",
            prompt=implement_prompt(profile),
            transition_description=(
                "One plan step is complete; continue with the next ready step."
            ),
            parameters=(WORKSPACE, PLAN),
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
        recovery_edge("implement"),
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
        recovery_edge("fix"),
        cancellation_edge("fix"),
    ]

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
    if "standards_review" in review_branches:
        edges.append(
            EdgeSpec(
                key="dispatch_standards_review",
                source="verification_dispatch",
                transition="fanout_verify",
                target="standards_review",
                prompt=standards_review_prompt(),
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
                    parameters=(STANDARDS_STATUS, COMPLIANCE_REPORT),
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
                context="compact_and_continue_session",
                context_source="previous_target_or_new",
                prompt=fix_prompt(profile),
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

    accepted_target = post_verification_target(profile)
    edges.append(
        EdgeSpec(
            key=f"gate_{accepted_target}",
            source="verification_gate",
            transition="accepted",
            target=accepted_target,
            prompt=post_verification_prompt(profile, accepted_target),
            transition_description=(
                "All enabled verification branches passed and delivery may continue."
            ),
            parameters=(WORKSPACE, REVIEW_CONTEXT),
        )
    )

    if profile.capability("device_smoke"):
        smoke_target = "prepare_pr" if profile.capability("pull_requests") else "cleanup"
        edges.extend(
            [
                EdgeSpec(
                    key=f"smoke_{smoke_target}",
                    source="smoke",
                    transition="passed",
                    target=smoke_target,
                    prompt=post_verification_prompt(profile, smoke_target),
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
                    context="compact_and_continue_session",
                    context_source="previous_target_or_new",
                    prompt=fix_prompt(profile),
                    transition_description=(
                        "Smoke testing found task-scoped implementation issues."
                    ),
                    parameters=(WORKSPACE, FIX_CONTEXT),
                ),
                recovery_edge("smoke"),
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
                    parameters=(WORKSPACE, PR_URL, BRANCH_NAME),
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
                    context="compact_and_continue_session",
                    context_source="previous_target_or_new",
                    prompt=pr_recovery_fix_prompt(profile),
                    requires_approval=True,
                    transition_description=(
                        "PR preparation found task-scoped changes that must be fixed."
                    ),
                    parameters=(WORKSPACE, BLOCKER),
                ),
                recovery_edge("prepare_pr"),
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
                        parameters=(WORKSPACE, PR_URL, BRANCH_NAME, CI_REPORT),
                    ),
                    EdgeSpec(
                        key="ci_monitor_fix",
                        source="ci_monitor",
                        transition="needs_changes",
                        target="fix",
                        context="compact_and_continue_session",
                        context_source="previous_target_or_new",
                        prompt=fix_prompt(profile),
                        transition_description=(
                            "CI found task-scoped failures that require fixes."
                        ),
                        parameters=(WORKSPACE, FIX_CONTEXT),
                    ),
                    recovery_edge("ci_monitor"),
                ]
            )

        edges.extend(
            [
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
                    key="waiting_pr_needs_user_action",
                    source="waiting_pr",
                    transition="needs_user_action",
                    target="waiting_pr",
                    context="compact_and_continue_session",
                    prompt=waiting_pr_prompt(profile),
                    requires_approval=True,
                    transition_description=(
                        "The pull request is still open; recheck only after user approval."
                    ),
                    parameters=(WORKSPACE, PR_URL, BRANCH_NAME, BLOCKER),
                ),
                EdgeSpec(
                    key="waiting_pr_fix",
                    source="waiting_pr",
                    transition="needs_changes",
                    target="fix",
                    context="compact_and_continue_session",
                    context_source="previous_target_or_new",
                    prompt=pr_feedback_fix_prompt(profile),
                    transition_description=(
                        "The PR state requires task-scoped changes before merge."
                    ),
                    parameters=(WORKSPACE, PR_REPORT),
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
            recovery_edge("cleanup"),
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


def agent_node(key: str, display_name: str, role: str) -> NodeSpec:
    return NodeSpec(
        key=key,
        kind="agent",
        display_name=display_name,
        agent=role,
        completion_mode="shell_command",
    )


def recovery_edge(node_key: str) -> EdgeSpec:
    return EdgeSpec(
        key=f"{node_key}_needs_user_action",
        source=node_key,
        transition="needs_user_action",
        target=node_key,
        context="compact_and_continue_session",
        prompt=f"""Resume the `{node_key}` stage after user action.

Previous blocker: {{{{.Params.blocker_reason}}}}

Use the retained compacted context, re-read current task comments and project
instructions, verify that the blocker is actually resolved, and continue the
same stage. Do not infer approval for any broader or destructive action.""",
        requires_approval=True,
        transition_description=(
            "Work is externally blocked; continue this stage only after approval."
        ),
        parameters=(BLOCKER,),
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


def plan_prompt(profile: ProjectProfile) -> str:
    return f"""Plan {{{{.TaskShortId}}}}: {{{{.TaskTitle}}}}

Read .kent/project-contract.md, .kent/workflow-profile.toml, and repository
instructions first. {procedure_instruction(profile, "plan")}

Task body:
{{{{.TaskBody}}}}

Keep discovery, design/spec ingestion, decisions, and implementation planning in
this one Plan session. Ask questions when a product decision is required. Do not
invoke nested prompt flows and do not implement production changes.

Complete with `implement` only when the plan has no unresolved product, API, UX,
or safety ambiguity. Provide `workspace_path` and `plan_path`; use an empty
`plan_path` only when the project contract explicitly allows planless work.
Complete with `needs_user_action` and `blocker_reason` for an external blocker.
Choose `wont_do` only for an explicit cancellation decision."""


def implement_prompt(profile: ProjectProfile) -> str:
    return f"""Implement {{{{.TaskShortId}}}}: {{{{.TaskTitle}}}}

Read .kent/project-contract.md, .kent/workflow-profile.toml, repository
instructions, and the plan at {{{{.Params.plan_path}}}}. Workspace:
{{{{.Params.workspace_path}}}}.

{procedure_instruction(profile, "implement")}
{delegation_instruction(profile, "implementation", "bounded write slices")}

Use the project procedure for step selection, recipes, editing, focused checks,
and plan progress. This generated workflow's completion contract overrides any
legacy procedure transition names such as `audit`.

Act as the single writer and implement exactly one ready plan step per node run.
After marking that step complete, choose `continue_implementation` with
`workspace_path` and `plan_path` when unchecked ready steps remain. Choose
`verify` only when every required plan step is complete; provide
`workspace_path` plus `review_context` summarizing plan/spec paths, the fixed
comparison point, changed files, checks, and risks for the read-only branches.
Use `needs_user_action` only for an external blocker and provide
`blocker_reason`. Choose `wont_do` only for explicit cancellation."""


def fix_prompt(profile: ProjectProfile) -> str:
    return f"""Apply task-scoped fixes for {{{{.TaskShortId}}}}.

Read .kent/project-contract.md and repository instructions first. Workspace:
{{{{.Params.workspace_path}}}}. Findings:
{{{{.Params.fix_context}}}}

{procedure_instruction(profile, "fix")}
{delegation_instruction(profile, "implementation", "bounded fixes")}

Remain the single writer. Fix root causes without broadening product scope.
Complete with `verify` and provide `workspace_path` plus a refreshed
`review_context` containing the findings, fixes, changed files, artifact paths,
and focused checks. Use `needs_user_action` only for an external blocker and
provide `blocker_reason`. Choose `wont_do` only for explicit cancellation."""


def standards_review_prompt() -> str:
    return """Run an independent read-only repository standards review.

Read AGENTS.md and .kent/project-contract.md first. Workspace:
{{.Params.workspace_path}}. Review context:
{{.Params.review_context}}

Inspect the change against repository architecture, engineering rules, security,
and maintainability constraints. Do not edit files and do not run destructive
commands. Findings are data for Join, not a routing decision.

Complete only with `reported`. Provide `standards_status` as exactly `passed`,
`needs_changes`, or `blocked`, plus `compliance_report` with evidence and
path-specific findings."""


def spec_review_prompt() -> str:
    return """Run an independent read-only specification review.

Read the task body, plan/spec artifacts named in the review context, and
.kent/project-contract.md. Workspace:
{{.Params.workspace_path}}. Review context:
{{.Params.review_context}}

Check acceptance criteria, product behavior, edge cases, and scope fidelity
independently from repository standards. Do not edit files. Findings are data
for Join, not a routing decision.

Complete only with `reported`. Provide `spec_status` as exactly `passed`,
`needs_changes`, or `blocked`, plus `review_report` with evidence and concrete
gaps."""


def verification_gate_prompt(profile: ProjectProfile) -> str:
    standards = (
        "Standards status: {{.Params.standards_status}}\n"
        "Standards report: {{.Params.compliance_report}}"
        if profile.capability("compliance_review")
        else "Standards review: not enabled by the project profile."
    )
    spec = (
        "Spec status: {{.Params.spec_status}}\n"
        "Spec report: {{.Params.review_report}}"
        if profile.capability("spec_review")
        else "Specification review: not enabled by the project profile."
    )
    return f"""Evaluate the joined verification reports without editing files.

Workspace: {{{{.Params.fanout_verify.workspace_path}}}}
Review context: {{{{.Params.fanout_verify.review_context}}}}
Verification status: {{{{.Params.verification_status}}}}
Verification report: {{{{.Params.verification_report}}}}
{standards}
{spec}

Choose `accepted` only when every enabled status is `passed`. Provide
`workspace_path` and a refreshed `review_context` summarizing all reports.
Choose `needs_changes` for task-scoped failures and provide `workspace_path`
plus `fix_context`. Choose `needs_user_action` for external or contradictory
blockers and provide `workspace_path`, `review_context`, and `blocker_reason`;
after approval every verification branch reruns. Choose `wont_do` only for an
explicit cancellation decision."""


def smoke_prompt(profile: ProjectProfile) -> str:
    return f"""Run focused smoke testing for {{{{.TaskShortId}}}}.

Read .kent/project-contract.md and repository instructions first. Workspace:
{{{{.Params.workspace_path}}}}. Review context:
{{{{.Params.review_context}}}}

{procedure_instruction(profile, "smoke")}

Follow all device, emulator, hardware-lock, install, and serial-selection rules.
Do not edit production files; route implementation findings to the single
writer. Complete with `passed` and provide `workspace_path` plus an updated
`review_context` containing the smoke report. Use `needs_changes` with
`workspace_path` and `fix_context` for task code issues. Use
`needs_user_action` with `blocker_reason` for external blockers."""


def prepare_pr_prompt(profile: ProjectProfile) -> str:
    return f"""Prepare delivery for {{{{.TaskShortId}}}}.

Read .kent/project-contract.md and repository instructions first. Workspace:
{{{{.Params.workspace_path}}}}. Review context:
{{{{.Params.review_context}}}}

{procedure_instruction(profile, "ship")}
{delegation_instruction(profile, "release", "bounded delivery checks")}

This workflow explicitly authorizes committing the task changes, pushing only
the current task branch, and creating or updating its pull request. It never
authorizes merging, pushing protected branches, or broadening scope.

Before committing, prove that the checkout is on a task-owned branch permitted
by the project contract. A source workspace, detached checkout, protected
branch, or ambiguous branch owner must route to `needs_user_action`; never push
through that ambiguity.

Complete through `monitor_ci` and provide `workspace_path`, `pr_url`, and
`branch_name`. If no PR is genuinely
applicable, choose `no_pr` and provide `pr_report`; this path requires approval.
Use `needs_changes` with `workspace_path` and `blocker_reason` for recoverable
PR/branch issues; this path also requires approval. Use `needs_user_action` with
`blocker_reason` for external blockers."""


def ci_prompt(profile: ProjectProfile) -> str:
    return f"""Monitor CI for {{{{.TaskShortId}}}} without editing files.

PR: {{{{.Params.pr_url}}}}
Branch: {{{{.Params.branch_name}}}}
Workspace: {{{{.Params.workspace_path}}}}

{procedure_instruction(profile, "ci")}
{delegation_instruction(profile, "ci", "bounded CI inspection")}

Use bounded polling and the project source-control adapter. Never merge or push.
Complete with `waiting_pr` only when all required checks are conclusively green
and provide `workspace_path`, `pr_url`, `branch_name`, and `ci_report`. Use
`needs_changes` with `workspace_path` and `fix_context` for task-code failures.
Use `needs_user_action` with `blocker_reason` for external failures or access
problems."""


def waiting_pr_prompt(profile: ProjectProfile) -> str:
    return f"""Check delivery state for {{{{.TaskShortId}}}}.

PR: {{{{.Params.pr_url}}}}
Branch: {{{{.Params.branch_name}}}}
Workspace: {{{{.Params.workspace_path}}}}

{procedure_instruction(profile, "waiting_pr")}

Do not merge or push. Choose `pr_merged` only when the source-control system
conclusively reports the PR as merged; provide `workspace_path`, `pr_url`,
`branch_name`, and `merge_report`. If it remains open, write a task comment with
the current state, choose `needs_user_action`, and provide `workspace_path`,
`pr_url`, `branch_name`, and `blocker_reason`; the workflow pauses for approval
before rechecking. Use `needs_changes` with `workspace_path` and `pr_report`
when task changes are required. Choose `close_without_merge` only when the
latest user comment explicitly approves closing or canceling this PR; provide
`workspace_path`, `pr_report`, and `closure_reason`."""


def cleanup_prompt(
    profile: ProjectProfile,
    *,
    merged: bool = False,
    no_pr: bool = False,
    closed: bool = False,
) -> str:
    workspace = "Workspace: {{.Params.workspace_path}}"
    if merged:
        context = """PR: {{.Params.pr_url}}
Branch: {{.Params.branch_name}}
Merge proof: {{.Params.merge_report}}"""
    elif no_pr:
        workspace = "Use the current task execution root as the workspace."
        context = "PR not applicable: {{.Params.pr_report}}"
    elif closed:
        context = """PR closure report: {{.Params.pr_report}}
Closure reason: {{.Params.closure_reason}}"""
    else:
        context = "Delivery context: {{.Params.review_context}}"
    return f"""Perform conservative cleanup for {{{{.TaskShortId}}}}.

{workspace}
{context}

{procedure_instruction(profile, "cleanup")}

Treat cleanup as report-first. Do not move, rename, or directly delete
Kent-managed worktrees. Remove a checkout or branch only when the project
procedure and user authorization make recovery safety conclusive.

Complete with `done` and provide `cleanup_report` describing performed and
skipped actions. Use `needs_user_action` with `blocker_reason` when safe cleanup
requires a human decision."""


def pr_recovery_fix_prompt(profile: ProjectProfile) -> str:
    return f"""Resolve an approved PR or branch recovery issue.

Read .kent/project-contract.md, repository instructions, and latest task
comments first. Workspace: {{{{.Params.workspace_path}}}}.
Recovery issue: {{{{.Params.blocker_reason}}}}

{procedure_instruction(profile, "fix")}

The approval applies only to the exact reported PR/branch recovery. Never infer
permission for a broader rebase or force-push. After resolving task-scoped code,
complete with `verify` and provide `workspace_path` plus refreshed
`review_context`. Use `needs_user_action` with `blocker_reason` if the approved
recovery is still unsafe."""


def pr_feedback_fix_prompt(profile: ProjectProfile) -> str:
    return f"""Fix task-scoped PR feedback.

Read .kent/project-contract.md, repository instructions, and latest task
comments first. Workspace: {{{{.Params.workspace_path}}}}.
PR report: {{{{.Params.pr_report}}}}

{procedure_instruction(profile, "fix")}

Remain the single writer. Do not merge or push protected branches. Complete
with `verify` and provide `workspace_path` plus refreshed `review_context`.
Use `needs_user_action` with `blocker_reason` for external or policy blockers."""


def post_verification_target(profile: ProjectProfile) -> str:
    if profile.capability("device_smoke"):
        return "smoke"
    if profile.capability("pull_requests"):
        return "prepare_pr"
    return "cleanup"


def delegation_instruction(
    profile: ProjectProfile,
    role_key: str,
    scope: str,
) -> str:
    role = profile.optional_role(role_key)
    if not role or role == "default":
        return ""
    return (
        f"When useful, delegate {scope} to `{role}`. Keep orchestration, "
        "integration, and transition selection in this node session."
    )


def post_verification_prompt(profile: ProjectProfile, target: str) -> str:
    if target == "smoke":
        return smoke_prompt(profile)
    if target == "prepare_pr":
        return prepare_pr_prompt(profile)
    if target == "cleanup":
        return cleanup_prompt(profile)
    raise ValueError(f"unsupported post-verification target {target!r}")
