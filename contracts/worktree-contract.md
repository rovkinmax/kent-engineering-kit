# Worktree Contract

Kent owns managed worktrees. Projects make a fresh checkout usable without
copying unrelated machine state.

## Operations

- Use `kent worktree`; do not move/rename/remove managed roots behind the service.
  Direct Git worktree operations are only for unmanaged project worktrees.
- Validate configured `worktrees.base_dir` and source-workspace non-overlap.
  Preserve retained target/root on startup failure; Start/Move/Resume and
  scheduled deletion are asynchronous, not completion proof.
- To target another Session use
  `~/.kent/bin/kent-worktree <command> --session <id> ...`, avoiding inherited
  caller identity. An explicit initial `--branch-name` does not authorize
  renaming an active task branch.
- Cleanup closes task-owned shells and leaves before post-session Janitor
  deletion. Native refusal must not be bypassed with force or foreign-owner
  cancellation. Reconcile partial effects before retry.
- For workflows carrying `pr_base_branch`, Cleanup freezes that exact
  authority-bound target and the Janitor re-observes the PR target before
  merged cleanup. Non-merged cleanup uses explicit `not-applicable`; it is
  absence of target evidence, not proof of a branch name. See the
  [delivery continuity contract](workflow-contract.md#delivery-continuity).
- Deletion succeeds only with validated terminal/native success, absent path
  and Git registration, and exact retained worktree identity. `scheduled`
  remains pending; unknown results return to Cleanup.

## Cross-Session cleanup coordination

Freeze exact Task/project/root/branch and recover prior coordination evidence.
Close only proven task-owned children through their existing handles. Neither
cwd/ancestry nor parent movement/PID exit establishes foreign-child ownership
or release.

Steer a verified active owner to release its own material from the exact root
and avoid re-entry. Obtain fresh request-bound release evidence and independently
recheck owner target, known children, runtime inventory and clean/recoverable
state before terminal preparation and leave/handoff. Relevant re-entry or
changed state invalidates the all-clear; unrelated activity elsewhere does not.

Bound observers and preserve real human/technical limits. Keep the contact,
outcome, unresolved owner/root and next action in existing evidence outside the
retiring root. Elapsed time alone does not justify a new Question or effect
retry. Do not use Task comments as a live-control requirement, stop foreign
Sessions or signal unknown processes.

Unknown/refused release or incomplete proof preserves resources and follows
the existing recovery route. Ask only for a genuine cross-owner decision or
external action. Confirmed release resumes normal preparation/leave/Janitor.

Owner acknowledgement is not an atomic reservation. Native managed-process
checks do not exclude arbitrary processes, child startup or same-user writes.
Report unavailable exclusion honestly; do not add a sweeper or global lock
service.

## Setup hook

The idempotent setup script accepts source workspace, branch and worktree root,
prefers authoritative `KENT_WORKTREE_*` values, and accepts structured stdin
setup JSON (`session_id` may be null). Copy/generate only an explicit allowlist;
never copy credentials by default.

Diagnostics go to stderr without secrets. Verification must bootstrap minimum
non-secret configuration itself or fail actionably, rather than depend solely
on setup having run.

## Task runtime state

Project checkpoints use ignored `.kent/runtime/<task-short-id>/` through the
qualified profile command. Checkpoints are mutable state; append-only evidence
is immutable history. Neither replaces the other. Helpers verify repository/
ignored-storage identity and safe file ownership. Janitor retires state only
after evidence retention and acknowledged owning cleanup.

## MCP project identity

MCP runs in the current worktree while machine configuration lookup uses primary
project identity. Task-specific worktree names are not MCP config identities.
Default operational artifacts use ignored `build/mcp-artifacts`, not plan
history. Explicit legacy overrides and qualified external R2 storage remain.
