# Repository Rules

- Keep the kit platform-neutral. Android, Web, iOS, embedded, and product-specific
  behavior belongs in project profiles and adapters.
- Global skills contain reusable engineering disciplines, not framework tutorials.
- Explicit user-driven orchestration belongs in `prompts/`; model-triggered
  disciplines belong in `skills/`.
- Review roles disable first-class edit tools. Reviewer shell access is
  inspection-only with two narrow exceptions for a genuinely assigned current
  Workflow review node: append only this node's own required evidence through
  the qualified configured evidence adapter, using genuine current
  Task/Node/Session/Step identity; and, only when explicitly configured for
  `shell_command`, submit this node's own actual result through the
  reminder-prescribed `kent task complete`, using current native identity,
  the declared transition and required parameters. Never use `--force`, another
  Task/Session selector, manual move/start/stop/resume/cancel, approval answers
  or graph/config mutation. This permits no source/checkpoint edits, fabricated
  sibling results, direct ledger writes, another producer's append, sealing,
  recovery or external
  product effects; no other shell writes are permitted. Missing qualification
  blocks the dependent action: never guess a binding or bypass initialization.
  Standalone/non-Workflow review remains inspection-only.
- Role prompts own behavior only. Model, reasoning, verbosity, tools, and
  delegation eligibility belong in Kent configuration; role-prompt
  frontmatter must not declare `model` or `tools`.
- Workflow fragments must use canonical parameters from `contracts/workflow-contract.md`.
- Generated workflows declare an explicit execution-target policy; the project
  profile and executable validation own minimum-version compatibility.
- Fan-out branches are read-only, transition directly to Join, and report failures
  as structured results.
- Do not edit live Kent workflow database records directly.
- Do not commit, push, publish, or create a remote repository unless explicitly requested.
- Generated or modified file content is written in English.
