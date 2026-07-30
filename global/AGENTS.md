# Engineering Operating Principles

- Investigate facts with available tools. Ask the user for decisions, not facts
  that can be discovered safely.
- Resolve product and architecture decisions one at a time. Include a concrete
  recommendation and keep final authority with the user.
- For hard bugs, establish a deterministic command that reproduces the exact
  symptom before changing production code.
- Before reviewing changes, pin the immutable task baseline and identify the
  specification or acceptance criteria. Keep that task baseline distinct from
  a moving PR merge target. Target-only commits added after task start are
  integration inputs, not task regressions, unless merge/replay evidence proves
  conflict or delivered-tree loss.
- Review repository standards and specification fidelity independently so one
  axis cannot hide failures in the other.
- Treat reviewer shell access as inspection-only. Kent's shell is not a
  sandbox or a tool-enforced read-only boundary.
- Store each durable decision in one authoritative artifact. Reference it
  elsewhere instead of copying it.
- A specification or plan may narrow, replace, or claim to supersede the task
  body only when it cites the exact human-authored task-comment ID or another
  explicit authoritative source. Agent-authored summaries and unsupported
  phrases such as "the user clarified" do not create product authority.
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
- On an acquired test emulator or simulator, bounded inspection and safe
  navigation of an already-authenticated app UI are allowed for focused Smoke
  without another user question. Unless the task body or a durable task comment
  explicitly says otherwise, do not retain broad/raw UI dumps, screenshots from
  production or unknown environments, physical devices, foreign apps,
  credentials, or secrets. On a project-declared non-production stage/test
  environment with synthetic data, scoped screenshots may be retained in the
  ignored evidence directory and audited without asking. Do not enter
  credentials, use a physical device, or perform account-, server-, or
  otherwise externally observable state changes. Opening and closing screens,
  dialogs, drawers, and menus is local navigation, not an external side effect.
  If the user grants a scoped exception during a task, record it in a durable
  task comment before continuing. Recovery sessions must not ask again when
  that authorization is already present.
  Directional Smoke navigation establishes runtime focus and inspects relevant
  UI source or semantic ordering. When focus order is known, compute the exact
  bounded route, execute it in one call, and verify the destination once.
  Replan on mismatch; otherwise use adaptive bursts and single-step only near
  the target. Never spend one model turn per key or use an ungrounded fixed
  loop.
  Prefer semantic control interaction and test directional focus separately
  when required. For mutable controls, prove state before and after activation,
  the intended local effect, and restoration of the original state. When
  semantics omit state, take a bounded task-scoped screenshot or perform visual
  inspection without asking the user. Declared stage/test screenshots are
  normal audited evidence. Visibility or an agent narrative alone cannot
  support a pass. A user-reported contradiction invalidates the affected
  evidence until a focused rerun resolves it.
  Allocate acceptance evidence before device work: runtime for rendering,
  focus/navigation, integration, restoration, and liveness; deterministic tests
  for non-observable defaults, classification, filtering, paging, and state
  transitions. Use mixed evidence unless explicit end-to-end acceptance
  requires otherwise. Do not clear profiles, require special fixtures, or add
  test-only product semantics merely to duplicate deterministic proof.
  Required summary, report, and checklist artifacts must be non-empty.
  Smoke must not mark its checklist item complete or claim a pass when it
  returns a blocker or findings transition; Kent task state is authoritative.
- Runtime target selection enforces the declared form factor before acquisition.
  Never pass an unfiltered mixed phone/TV/watch/automotive list to
  `acquire-any`; acquire an eligible exact serial and verify its identity.
  Evidence setup fails fast, and agents use the patch tool instead of invoking a
  shell `apply_patch`.
- Workflow-owned Standards, Specification, and Compliance reviewers are leaf
  sessions. They complete their bounded pass directly and never start child
  agents.
- Plan checkboxes track writer-owned implementation only. Runtime Smoke and
  other workflow-owned review/delivery stages are recorded as downstream scope,
  not unchecked implementation prerequisites. A writer encountering such a
  legacy checkbox carries it into verification context instead of executing or
  marking it complete.
- Prefer independently verifiable vertical slices. Treat broad mechanical
  refactors as expand-migrate-contract work when slices cannot stay green.
- Do not commit, push, publish, flash hardware, or perform other irreversible
  actions unless the user or an applicable workflow explicitly authorizes it.
