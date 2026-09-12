# Review Context Budget

Required:
- `AGENTS.md`, `.kent/project-contract.md`, `.kent/workflow-profile.toml`;
- authoritative task, approved scope, immutable `fixed_point`;
- incoming plan/preview or implementation diff and verification evidence.

Conditional:
- Plan Review: `.kent/commands/plan.md`, exact preview hash and first
  independent read-only PASS receipt; independently review the same preview;
- Standards review: only affected source, tests and normative contracts;
- Gate: current fan-out reports, their HEAD/baseline bindings and findings;
- target-dependent findings: actual merge/replay evidence.

Review is inspection-only even when shell access exists. Return structured
PASS/failure/blocker evidence via the existing graph. No edits, delegated
writers, effect approvals, or third routine preview review. Do not preload
delivery, disabled Smoke, consumer rules or unrelated source trees.
