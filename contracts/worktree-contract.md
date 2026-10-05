# Worktree Contract

Kent owns managed workflow worktrees. Project setup remains responsible for
making a fresh checkout usable without silently copying unrelated local state.

## Operations

- Use `kent worktree` commands for Kent-managed worktrees.
- Kent 2.6.1 requires every managed worktree path, automatic or explicitly
  selected, to remain under the configured `worktrees.base_dir` and never
  overlap the source Workspace. Validate this namespace before upgrade and
  before recovery. A persisted path outside it cannot be activated or restored
  until moved with a supported Kent operation.
- Do not treat `kent task start`, `move`, or `resume` as synchronous worktree
  completion. Re-read Task state and preserve the retained target/worktree when
  startup is still in progress or fails.
- The initial managed-worktree branch may be supplied with `--branch-name` on
  Task Start, Move, or Resume. It is not a reason to rename an active task
  branch during workflow migration.
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

## Bounded cross-Session cleanup coordination

Use this bounded cross-Session cleanup coordination protocol only when retiring
a managed task worktree may affect
another Session's child process or runtime material. It adds no authority to
stop another Session, signal an unknown process, or delete resources outside
the current task. Never infer ownership from a working directory or process
ancestry alone.

1. Freeze the exact Task ID, project, canonical worktree root, and branch from
   the current task carrier. Recover prior contact, timeout, blocker, and
   Question records before retrying. Close only safe children proven to belong
   to this Task through their existing tool handles.
2. Inspect both the owner Session's current target and retained child cwd
   evidence. A parent move, old PID exit, or successful tool call does not
   prove that the child released this root. Unknown ownership preserves the
   worktree.
3. For a verified active foreign owner, retain a request record in the exact
   cleanup Task before contact. Then use the supported
   `kent run steer <owner-session-id>` path to ask that owner to release only
   its own children using this exact root, move its own Session elsewhere,
   handle its runtime material under its own authority, and not re-enter during
   cleanup. Identify the canonical Task, project, root, and current request
   reference in the message. Require a fresh Task comment attributed to the
   owner Session that references this request and exact root, identifies the
   released children, and states the runtime-material disposition. Read
   comments with the supported
   `kent task comment list <task> --project <project> --limit <n> --offset <n>`
   interface. Do not hardcode a Task, project, Session, root, or historical
   process identity.
4. Bound the entire attempt—including commands and waiting—to one monotonic
   60-second deadline, at most two bounded Task-comment observations, and no
   more than 30 seconds between observations. Bound each observer subprocess
   to the remaining deadline. An incomplete page or unavailable readback is
   an evidence gap, not proof that the owner is unavailable. Do not use
   `kent run watch` or `wait` as an acknowledgement channel. Questions,
   approvals, interruptions, and unrelated run outcomes are not
   acknowledgements. If a materially new blocker appears, raise it to the
   owner at most once during this same attempt and only if the deadline
   permits; do not restart the deadline. On expiry, stop only the task-owned
   observer if needed; never stop or signal the owner Session.
5. After an acknowledgement, freshly recheck the exact owner target, known
   child cwd, clean/recoverable worktree state, runtime inventory, and existing
   evidence immediately before terminal preparation and again before leave or
   handoff. Owner re-entry, new activity in this root, a retained child cwd, or
   changed relevant runtime/evidence invalidates the previous all-clear.
   Independent owner activity in another worktree does not invalidate release
   of this root.
6. Retain the attempt identity (Task/root/owner/blocker), start and deadline,
   contact outcome, exhausted budget, and any original Question reference in
   existing Task records outside the retiring root. An identical retry reuses
   that record and existing escalation carrier; elapsed time alone does not
   justify another Question. A materially changed owner, blocker, release
   proof, or human decision can justify a new bounded attempt after the
   previous attempt concludes or its basis materially changes.
7. When release is confirmed and fresh safety checks pass, continue through
   the existing terminal preparation, supported `kent worktree leave`, and
   normal post-Session Janitor handoff. A native `scheduled` result is not
   deletion success.
8. On refusal, timeout, unavailable or unknown ownership, incomplete evidence,
   or unsupported action, preserve the worktree and report the exact blocker
   through the existing route. Ask the user only for a real cross-owner
   decision or unavailable external action, not for safe task-owned cleanup.

A Task comment acknowledgement and the last observation are not an atomic
reservation. Existing native managed-process checks cover supported managed
shells with retained launch workdirs, and supported Session Enter/Delete
operations share the existing workspace mutation lane. They do not establish
exclusion for arbitrary OS processes, unknown ownership, arbitrary same-UID
filesystem writes, or arbitrary child startup between checks. Preserve and
report any case requiring unavailable native exclusion; do not claim
race-free protection, add a global lock service or process sweeper, or
automatically stop another Session.

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

## Setup and recovery diagnostics

Kent 2.6.1 preserves Script stderr diagnostics and keeps invalid, unavailable,
or failed setup work resumable. Setup failures must expose an actionable choice:
retry the retained target, select another permitted target, or inspect and clean
up the retained worktree. Project setup hooks and verification commands must
write useful diagnostics to stderr without leaking credentials or raw
authenticated payloads.

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

Runtime v2 opens workspace, `.kent`, runtime, task, and lock components
descriptor-relatively with no-following and owner-only regular-file checks.
Terminal cleanup preserves the ledger and sentinel until the owning Kent
deletion is acknowledged.

## MCP project identity

The global MCP adapter separates the current execution root from the primary
project root. Calls run in the current worktree and store artifacts there, while
machine config lookup uses the primary Git worktree identity. Task-specific
worktree names must not become MCP config identities.
