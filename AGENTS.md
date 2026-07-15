# Repository Rules

- Keep the kit platform-neutral. Android, Web, iOS, embedded, and product-specific
  behavior belongs in project profiles and adapters.
- Global skills contain reusable engineering disciplines, not framework tutorials.
- Explicit user-driven orchestration belongs in `prompts/`; model-triggered
  disciplines belong in `skills/`.
- Review roles disable first-class edit tools. Kent 2.2 has no read-only shell
  capability, so their role contracts restrict shell usage to inspection.
- Workflow fragments must use canonical parameters from `contracts/workflow-contract.md`.
- Fan-out branches are read-only, transition directly to Join, and report failures
  as structured results.
- Do not edit live Kent workflow database records directly.
- Do not commit, push, publish, or create a remote repository unless explicitly requested.
- Generated or modified file content is written in English.
