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
- Cleanup follows the qualified owner/procedure explicitly selected by the
  native Workflow. The selected legacy route closes task-owned shells and
  leaves before post-session Script Janitor deletion. An Agent cleanup route
  needs separate qualification and explicit selection; neither a profile map
  nor ordinary R2 storage access selects it automatically.
  Native refusal must not be bypassed with force or foreign-owner cancellation.
  Reconcile partial effects before retry.
- For workflows carrying `pr_base_branch`, Cleanup freezes that exact
  authority-bound target. The selected legacy Script Janitor re-observes the
  PR target before merged cleanup; other selected routes retain their
  qualified target checks. Non-merged cleanup uses explicit `not-applicable`;
  it is absence of target evidence, not proof of a branch name. See the
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
external action. Confirmed release resumes normal preparation/leave under the
selected cleanup procedure; post-session Script Janitor handoff belongs only
to the selected legacy route.

Owner acknowledgement is not an atomic reservation. Native managed-process
checks do not exclude arbitrary processes, child startup or same-user writes.
Report unavailable exclusion honestly; do not add a sweeper or global lock
service.

## Separately qualified Agent cleanup

An explicitly selected Agent-owned route uses supported native worktree
operations and its reviewed retention, owner-release, leave, deletion and
recovery procedure. Ordinary document publication is not terminal acceptance
or deletion authority. Preserve exact original Task/worktree/root/branch
identity and before/after observations outside the retiring worktree.

An interruption after deletion must settle the observed effect before retry.
Do not recreate the tree, replay deletion, move a Task past its cleanup owner,
or infer success from Resume acknowledgement. If the retained target no longer
exists, use only the separately authorized supported replacement-target
recovery and re-read actual native startup/root identity.

Task interruption is not a subprocess-kill guarantee. Prove owner-controlled
shell/child settlement or safely retain the resource; a surviving bounded child
that exits naturally is not evidence that interruption killed it. Controlled
post-delete recovery does not qualify server-crash, power-loss, mid-RPC or
SIGKILL recovery, nor establish blanket production cleanup safety.

## Setup hook

The idempotent setup script accepts source workspace, branch and worktree root,
prefers authoritative `KENT_WORKTREE_*` values, and accepts structured stdin
setup JSON (`session_id` may be null). Copy/generate only an explicit allowlist;
never copy credentials by default.

Diagnostics go to stderr without secrets. Verification must bootstrap minimum
non-secret configuration itself or fail actionably, rather than depend solely
on setup having run.

## Task runtime state

Selected legacy project checkpoints use ignored
`.kent/runtime/<task-short-id>/` through the qualified profile command.
Checkpoints are mutable state; append-only evidence is immutable history.
Neither replaces the other. Helpers verify repository/
ignored-storage identity and safe file ownership. On the selected legacy route,
Script Janitor retires state only after evidence retention and acknowledged
owning cleanup. Other routes must explicitly qualify their own runtime
storage, retention and terminal cleanup obligations rather than importing
legacy checkpoint/ledger state.

## MCP project identity

MCP runs in the current worktree while machine configuration lookup uses primary
project identity. Task-specific worktree names are not MCP config identities.
Default operational artifacts use ignored `build/mcp-artifacts`, not plan
history. Explicit legacy overrides and qualified external R2 storage remain.
