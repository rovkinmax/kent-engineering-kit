# Delivery Context Budget

Required:
- `AGENTS.md`, `.kent/project-contract.md`, `.kent/workflow-profile.toml`;
- authoritative delivery/report-only permission and current gate evidence;
- the active mapped procedure: `.kent/commands/ship-pr.md`,
  `.kent/commands/waiting-pr.md`, or `.kent/commands/cleanup-task.md`;
- exact branch, HEAD, PR/disposition and owned resource evidence.

Conditional:
- PR feedback: only affected scope and review findings;
- source refresh: README self-development preflight;
- cleanup: current ownership, process, path and Git/Kent registration proofs.

Do not preload implementation recipes, disabled CI/Smoke, consumer rollout or
installation instructions. Append evidence before each Agent transition.

For Kit source delivery, read the source-verification ownership and identity
contract in `.kent/project-contract.md`. Prepare delivery only with the
current report/log and matching source/environment identity references for
the exact source. Do not add a routine full validator run in Delivery;
identity uncertainty routes back through the existing verification path.
