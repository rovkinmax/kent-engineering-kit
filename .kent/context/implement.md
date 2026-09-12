# Implement Context Budget

Required:
- `AGENTS.md`, `.kent/project-contract.md`, `.kent/workflow-profile.toml`;
- approved plan/preview and its exact human approval;
- `.kent/commands/implement.md` for Implement, or `.kent/commands/fix.md`
  for Fix;
- incoming task baseline, findings, checkpoint and relevant prior evidence.

Conditional:
- only source/tests/contracts touched by the approved slice;
- authoritative templates when generated copies are involved;
- merge/replay evidence when a finding depends on target-only changes.

Do not preload delivery, Smoke, consumer/device procedures or unrelated
backlog. Preserve continuous-writer context and append slice evidence.
