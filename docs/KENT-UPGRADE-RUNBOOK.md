# Kent Upgrade Runbook

Use this runbook when Kent changes workflow persistence, runtime continuity, or
Script completion contracts.

## Before upgrading

1. Upgrade CLI/TUI, service, and Desktop as one version set.
2. Record every active Task, Current Node, execution target, worktree, pending
   approval, and dirty path.
3. Export every live Workflow and create a verified Kent database backup.
4. Do not edit a task-backed Workflow in place. Generate and validate a new
   version unless the upgrade itself makes the old graph invalid.

## After upgrading

1. Validate every linked Workflow in execution mode. Relative `script_path`
   checks skipped without a task worktree are contextual warnings; duplicate
   Transition keys or other `blocks_context=true` findings are real blockers.
2. Treat Transition keys, prompt references, Script stdout, and persisted
   prior-value keys as one contract. Kent 2.5 requires source-qualified,
   workflow-wide unique Transition keys.
3. Re-export snapshots only after live validation passes.
4. Check project-owned command implementations before synchronization. Generic
   kit sync must not replace a custom verifier or release wrapper.

## Active task recovery

`kent task resume` returns after durable requeueing. It does not prove that the
Session or Script started. Re-read the Task after startup:

- `running` or `active` with no attention means recovery started;
- an immediate `interrupted` state means Resume failed asynchronously;
- `resumed current node has no assigned Session` or `has no retained session`
  requires re-entry through the smallest valid incoming `new_session`
  Transition with preserved values;
- a failed fan-out branch is recovered by re-entering the fan-out source, not
  by starting one sibling independently.

Task-managed worktrees remain pinned to their original revision. When an
unavoidable graph repair changes Script transition output, synchronize only
the required runtime scripts into each affected old worktree before resuming.
Record that the files are recovery infrastructure already present on the target
branch, not task scope.

## Evidence and approvals

Questions and approvals are reserved for human decisions and external actions.
If an agent failed to retain a required pre-edit red run, reconstruct it only
when bounded and safe. Otherwise record the absence and continue with current
deterministic evidence. Never ask the user to approve the absence of
agent-owned bookkeeping.

## Pull requests

When a task fully resolves an issue in the same repository, add the provider's
closing reference to the PR body, for example `Fixes #51` on GitHub. Use a
non-closing link for partial, cross-repository, or follow-up relationships.
