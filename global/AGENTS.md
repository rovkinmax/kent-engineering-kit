# Engineering Operating Principles

## Authority And Decisions

- Investigate facts with available tools. Ask the user for decisions, not facts
  that can be discovered safely.
- Changes to the Kent Engineering Kit, generated or live workflows, workflow
  adapters/contracts, agent roles, or Kent configuration require one bounded
  governance pass before mutation:
  1. investigate the current behavior and affected surfaces;
  2. present a preview naming the files, graph delta, rollout, rollback, and
     restart impact;
  3. obtain at least two independent read-only reviews of that preview;
  4. obtain explicit user approval;
  5. only then edit files or mutate live Kent state.
  Ordinary product implementation does not inherit this ceremony. Approval is
  valid for the previewed scope only; stop and present a new preview before a
  material scope expansion.
- Resolve material product or architecture decisions one at a time. Include a
  concrete recommendation and keep final authority with the user.
- Store each durable decision in one authoritative artifact and reference it
  elsewhere instead of copying it.
- A specification or plan may narrow or supersede the task body only when it
  cites an exact human-authored task-comment ID or another explicit
  authoritative source. Agent summaries and unsupported claims that "the user
  clarified" do not create product authority.
- Missing agent-produced bookkeeping is not a user decision. Reconstruct it
  only when bounded and safe; otherwise record the gap and continue with the
  available evidence.
- Resolve an ordinary missing operational date to the current execution date.
  Ask only when the date has independent business meaning or conflicts with an
  explicit source.

## User Communication

- Write user-facing workflow communication in the user's preferred language;
  for this user, default to Russian. Keep code, commands, identifiers,
  structured keys, and repository artifacts in their project-defined language.
- This includes questions, transition commentary, `blocker_reason`,
  `closure_reason`, and approval summaries.
- Make approval and blocker text decision-oriented. Use the compact Russian
  structure `Нужно от вас`, `Почему`, and `После подтверждения` when useful.
- Questions and approvals are for real user decisions or external actions. In approval text, do not paste raw review reports; do not present task-scoped code fixes as actions the user must perform.

## Engineering Safety

- For hard bugs, establish a deterministic reproduction before changing
  production code.
- Keep the immutable task baseline separate from a moving merge target.
  Target-only commits are integration inputs, not task regressions, unless
  merge or replay evidence proves conflict or delivered-tree loss.
- Treat reviewer shell access as inspection-only; shell availability is not a
  sandbox or a tool-enforced read-only boundary.
- Prefer independently verifiable vertical slices. Use
  expand-migrate-contract when broad mechanical changes cannot stay green.
- Do not commit, push, publish, flash hardware, merge, delete remote state, or
  perform another irreversible action unless the user or an applicable
  workflow explicitly authorizes it.

## Kent Sessions And Tasks

- Kent task state owns workflow lifecycle. Do not mirror Current Node,
  execution target, execution root, or resolved commit in project metadata.
- Task comments are durable context, not a live control channel. Send active
  run feedback through `kent run steer` or the equivalent interactive message.
- Existing and resumed sessions retain their locked system prompt and
  execution settings. Prompt changes affect new sessions; global configuration
  changes additionally require the documented Kent restart.
- `kent task resume` confirms durable requeueing, not successful Session or
  Script startup. Re-read task state after a short delay and diagnose a return
  to `interrupted`.
- A Current Node without an assigned or retained Session must re-enter through
  a supported incoming `new_session` transition with preserved values.
  Repeated Resume is not recovery.
- `compact_and_continue_session` may keep the same session ID. Verify the
  compaction event and refreshed lock rather than expecting a new ID.
- Workflow transition keys, Script stdout, prompts, and parameters are one
  revisioned contract. Do not mutate a task-backed graph edge by edge; create a
  new workflow revision and let active tasks finish on their frozen graph.

## Worktrees And Shared Resources

- Use `kent worktree` for Kent-managed worktrees. For an explicit other
  session, use `~/.kent/bin/kent-worktree ... --session <id>` so inherited
  execution context cannot select the wrong workspace.
- Do not move or directly remove Kent-managed worktrees. Follow the
  project-owned setup, build, cleanup, and shared-resource procedures.
- A resource-owning node must preserve its durable checkpoint/evidence, release
  or safely retain its lease, and take a normal workflow transition. Do not
  bypass node-owned cleanup with a manual task move.

## Context Discipline

- When a project declares a node context manifest, read it first. Load only its
  required sources and conditionally triggered references; do not preload the
  rest of the project documentation.
- Keep workflow evidence append-only. A later slice adds evidence and metrics;
  it does not rewrite the previous slice's record.
- Platform, runtime, source-control, release, MCP, and project architecture
  details belong in the applicable project skill or workflow contract and are
  loaded only by nodes that need them.
