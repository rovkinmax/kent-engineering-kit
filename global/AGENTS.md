# Engineering Operating Principles

- Investigate facts with available tools. Ask the user for decisions, not facts
  that can be discovered safely.
- Resolve product and architecture decisions one at a time. Include a concrete
  recommendation and keep final authority with the user.
- For hard bugs, establish a deterministic command that reproduces the exact
  symptom before changing production code.
- Before reviewing changes, pin the comparison baseline and identify the
  specification or acceptance criteria.
- Review repository standards and specification fidelity independently so one
  axis cannot hide failures in the other.
- Treat reviewer shell access as inspection-only. Kent's shell is not a
  sandbox or a tool-enforced read-only boundary.
- Store each durable decision in one authoritative artifact. Reference it
  elsewhere instead of copying it.
- When `kent worktree` is available in Kent 2.3 or newer, use it for
  Kent-managed worktrees. On older Kent versions, do not manipulate managed
  worktrees directly; follow the project's documented worktree procedure.
- When targeting another session with `kent worktree <command> --session`, use
  `~/.kent/bin/kent-worktree <command> --session <id> ...`. It clears inherited
  Kent execution context so the explicit session selects the authoritative
  workspace.
- Treat a workflow task's execution target, execution root, and resolved commit
  as Kent-owned facts rather than duplicating them in project metadata.
- Treat task comments as durable context, not a live control channel. Feedback
  for an active workflow run must be sent to its session with `kent run steer`
  or the equivalent interactive message. Ask resource-owning nodes to stop
  safely, release locks, record any mutation ledger, and choose their normal
  workflow transition; do not bypass cleanup with a manual task move.
- Existing and resumed sessions retain their locked system prompt and execution
  settings. Prompt changes affect newly created sessions; global configuration
  changes additionally require the documented Kent restart.
- `compact_and_continue_session` may retain the same session ID while performing
  a real context compaction and refreshing the session lock. Verify compaction
  events and the new lock timestamp; do not require a new ID or interpret the
  cumulative model-request count as the current context size.
- When a project uses the Kent Engineering Kit, call MCP through
  `~/.kent/bin/kent-mcp-call` and `~/.kent/bin/kent-mcp-list`. Keep credentials
  and project-specific server definitions outside the global adapter.
- MCP call logs are metadata-only, but normal command stdout remains in Kent's
  shell transcript. Sensitive MCP calls use `--quiet`, `--digest-output`, or
  output assertions so raw responses never reach shell output. Use
  `--hash-matches` with `--marker-present` when a workflow needs only opaque
  semantic identity sets and pagination markers. Opt in to raw response
  artifacts only for known-safe evidence; never retain unexpected authenticated
  UI, credentials, headers, broad device logs, or unredacted network payloads.
- Workflow-owned Standards, Specification, and Compliance reviewers are leaf
  sessions. They complete their bounded pass directly and never start child
  agents.
- Prefer independently verifiable vertical slices. Treat broad mechanical
  refactors as expand-migrate-contract work when slices cannot stay green.
- Do not commit, push, publish, flash hardware, or perform other irreversible
  actions unless the user or an applicable workflow explicitly authorizes it.
