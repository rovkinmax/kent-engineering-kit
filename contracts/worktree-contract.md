# Worktree Contract

Kent 2.3 owns managed workflow worktrees. Project setup remains responsible for
making a fresh checkout usable without silently copying unrelated local state.

## Operations

- Use `kent worktree` commands for Kent-managed worktrees.
- Use `~/.kent/bin/kent-worktree <command> --session <id> ...` when targeting a
  session other than the caller. The wrapper removes inherited
  `KENT_SESSION_ID`, `KENT_RUN_ID`, and `KENT_STEP_ID` before invoking the Kent
  CLI.
- Direct Git worktree commands are allowed only for project-local worktrees that
  Kent does not manage.
- Never move or rename a Kent-managed worktree behind the service.

## Setup hook

- The setup script is idempotent and safe to rerun after partial failure.
- It accepts the source workspace, branch name, and worktree root as positional
  arguments.
- It prefers Kent's authoritative `KENT_WORKTREE_*` environment values when
  available.
- It accepts the structured Kent 2.3 JSON payload on stdin. `session_id` may be
  null for workflow-created worktrees.
- It copies or generates only an explicit allowlist of required local files.
- Credentials and project secrets are not copied by default.

## Verification resilience

When deterministic verification depends on untracked machine configuration, the
project verification entrypoint must either bootstrap the minimum non-secret
configuration itself or fail with an actionable diagnostic. The setup hook may
call the same bootstrap helper, but verification must not rely on the hook being
the only path to a usable checkout.

## MCP project identity

The global MCP adapter separates the current execution root from the primary
project root. Calls run in the current worktree and store artifacts there, while
machine config lookup uses the primary Git worktree identity. Task-specific
worktree names must not become MCP config identities.
