# Worktree Contract

Kent owns managed workflow worktrees. Project setup remains responsible for
making a fresh checkout usable without silently copying unrelated local state.

## Operations

- Use `kent worktree` commands for Kent-managed worktrees.
- Use `~/.kent/bin/kent-worktree <command> --session <id> ...` when targeting a
  session other than the caller. The wrapper removes inherited
  `KENT_SESSION_ID`, `KENT_RUN_ID`, and `KENT_STEP_ID` before invoking the Kent
  CLI.
- Cleanup closes every task-owned background shell and runs
  `kent worktree leave` before handing deletion to Task Janitor. Janitor
  verifies that the Cleanup session no longer targets the task worktree.
- A zero exit code from `kent worktree delete --json` is not sufficient:
  `scheduled` is non-terminal. Janitor accepts only `kind=completed` plus an
  absent worktree path and Git registration; every other result returns to
  Cleanup.
- Direct Git worktree commands are allowed only for project-local worktrees that
  Kent does not manage.
- Never move or rename a Kent-managed worktree behind the service.

## Setup hook

- The setup script is idempotent and safe to rerun after partial failure.
- It accepts the source workspace, branch name, and worktree root as positional
  arguments.
- It prefers Kent's authoritative `KENT_WORKTREE_*` environment values when
  available.
- It accepts Kent's structured JSON setup payload on stdin. `session_id` may be
  null for workflow-created worktrees.
- It copies or generates only an explicit allowlist of required local files.
- Credentials and project secrets are not copied by default.

## Verification resilience

When deterministic verification depends on untracked machine configuration, the
project verification entrypoint must either bootstrap the minimum non-secret
configuration itself or fail with an actionable diagnostic. The setup hook may
call the same bootstrap helper, but verification must not rely on the hook being
the only path to a usable checkout.

## Task runtime state

- Generated Fix and Smoke stages store resumable state only under the ignored
  `.kent/runtime/<task-short-id>/` directory.
- The project adds `/.kent/runtime/` to `.gitignore`.
- The checkpoint helper refuses to write when Git does not prove that path is
  ignored, and writes atomically inside the current repository root.
- Append-only evidence lives beside checkpoints at
  `.kent/runtime/<task-short-id>/evidence-ledger.jsonl`. The evidence helper
  validates repository identity, ignored storage, project instruction paths,
  and the hash chain before appending.
- Checkpoints are mutable current-state snapshots. The evidence ledger is the
  immutable slice history; neither replaces the other.
- Task Janitor removes this runtime state only as part of safe task cleanup.

## MCP project identity

The global MCP adapter separates the current execution root from the primary
project root. Calls run in the current worktree and store artifacts there, while
machine config lookup uses the primary Git worktree identity. Task-specific
worktree names must not become MCP config identities.
