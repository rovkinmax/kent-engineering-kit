# Repository Rules

- Keep the kit platform-neutral. Android, Web, iOS, embedded, and product-specific
  behavior belongs in project profiles and adapters.
- Global skills contain reusable engineering disciplines, not framework tutorials.
- Explicit user-driven orchestration belongs in `prompts/`; model-triggered
  disciplines belong in `skills/`.
- Review roles disable first-class edit tools. Kent shell access is not a
  tool-enforced read-only boundary, so their role contracts restrict it to
  inspection.
- Role prompts own behavior only. Model, reasoning, verbosity, tools, and
  delegation eligibility belong in Kent configuration; role-prompt
  frontmatter must not declare `model` or `tools`.
- Workflow fragments must use canonical parameters from `contracts/workflow-contract.md`.
- Generated workflows require Kent 2.5+ and declare an explicit
  execution-target policy.
- Fan-out branches are read-only, transition directly to Join, and report failures
  as structured results.
- Do not edit live Kent workflow database records directly.
- Do not commit, push, publish, or create a remote repository unless explicitly requested.
- Generated or modified file content is written in English.
