# Workflow Contract

The common workflow layer is platform-neutral. Projects supply build, test,
device, source-control, issue-tracker, and release adapters.

## Lifecycle

- Kent task state owns workflow lifecycle.
- Kent owns the selected execution target, execution root, and resolved Git
  commit. Project metadata must not mirror those facts.
- A project artifact may own implementation-step progress.
- Recoverable blockers use approval-gated `needs_user_action` self-loops.
- A post-Join blocker may approval-loop through verification dispatch so every
  read-only branch reruns with fresh state.
- `wont_do` is terminal, requires an explicit cancellation decision, and emits
  `closure_reason`.
- Parallel verification branches are read-only.
- One writer owns fixes and integration.
- `done` means delivered or explicitly approved report-only completion.

## Portable parameters

- `workspace_path`
- `plan_path`
- `spec_path`
- `fixed_point`
- `changed_files`
- `verification_report`
- `review_report`
- `compliance_report`
- `review_context`
- `fix_context`
- `verification_status`
- `standards_status`
- `spec_status`
- `smoke_report`
- `blocker_reason`
- `pr_url`
- `branch_name`
- `pr_report`
- `ci_report`
- `merge_report`
- `closure_reason`
- `cleanup_report`

## Execution targets

- Generated workflows always set an explicit Kent 2.3 execution-target policy.
- The profile supplies a default and may override it by workflow kind.
- Supported policy values are `ask-on-first-execution`, `none`, `head`,
  `default-branch`, and `ref:<revision>`.
- Delivery workflows should normally ask on first execution.
- Canary workflows should normally use Source HEAD.
- Trunk maintenance should normally use the repository default branch.
- Release and hotfix workflows should use an explicitly selected revision.
- `none` is reserved for intentional source-workspace execution, including
  non-Git workspaces and small local jobs that do not need isolation.
- A task-level start, approval, or move override may select a concrete target
  without mutating the workflow policy.

## Fan-out constraints

- Every branch transitions directly to its Join.
- Branch failures are reported to the Join as data.
- A branch emits one stable parameter contract on every completion.
- Only the post-Join gate chooses Fix, QA, Ship, or Needs User Action.

## Role resolution

- Operational nodes use a `default` orchestrator role.
- Project-local implementation, build, QA, release, and CI roles are delegated
  from the orchestrator when useful.
- Direct custom node assignees are limited to globally registered roles that
  Kent 2.3 execution validation can resolve.
- Independent standards and specification reviews use global read-only roles.

## Project adapter boundary

Each project provides:

- `.kent/project-contract.md`;
- `.kent/workflow-profile.toml`;
- `.kent/scripts/workflow-verify`;
- an optional idempotent `.kent/worktrees/setup.sh` conforming to
  `worktree-contract.md`;
- canonical project-local role keys;
- optional smoke, resource-lock, PR, CI, and release adapters.
