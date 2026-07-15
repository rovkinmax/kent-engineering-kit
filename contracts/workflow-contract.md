# Workflow Contract

The common workflow layer is platform-neutral. Projects supply build, test,
device, source-control, issue-tracker, and release adapters.

## Lifecycle

- Kent task state owns workflow lifecycle.
- A project artifact may own implementation-step progress.
- Recoverable blockers use approval-gated `needs_user_action` self-loops.
- `wont_do` is terminal and requires an explicit cancellation decision.
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
- `blocker_reason`
- `pr_url`
- `branch_name`
- `ci_report`
- `waiting_reason`
- `merge_report`
- `cleanup_report`

## Kent 2.2 fan-out constraints

- Every branch transitions directly to its Join.
- Branch failures are reported to the Join as data.
- A branch emits one stable parameter contract on every completion.
- Only the post-Join gate chooses Fix, QA, Ship, or Needs User Action.

## Project adapter boundary

Each project provides:

- `.kent/project-contract.md`;
- `.kent/workflow-profile.toml`;
- `.kent/scripts/workflow-verify`;
- canonical project-local role keys;
- optional smoke, resource-lock, PR, CI, and release adapters.
