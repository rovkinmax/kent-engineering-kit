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

For Kit self-development, use the source-verification ownership and identity
contract in `.kent/project-contract.md`: run affected tests and focused
production-shaped wrapper/helper checks, leave the one fresh full validator
run to the configured verifier, and carry report/log/identity references
through the existing handoff. Identity uncertainty requires fresh
verification; it does not authorize a cache or a skipped verifier run.
