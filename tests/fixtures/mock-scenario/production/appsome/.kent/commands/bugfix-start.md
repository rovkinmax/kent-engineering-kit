---
description: Produce an implementation-ready plan for a reproducible defect
---

# Bugfix Planning

Plan a defect fix without editing production code.

1. Read `AGENTS.md`, `.kent/project-contract.md`, the task body, current task comments, and applicable Appsome rules.
2. Create or reuse `.todo/bugfix-<slug>/` without any global pointer file.
3. Record expected behavior, actual behavior, reproduction conditions, affected surfaces, and Jira/source evidence.
4. Establish the narrowest deterministic reproduction available: a failing test, command, log signature, or explicit
   runtime scenario. If reproduction requires unavailable access or a product decision, record the blocker instead of
   guessing.
5. Inspect Android code and any required generated SDK or iOS reference source before forming a root-cause hypothesis.
6. Read `.kent/commands/mock-scenario-policy.md` and inventory each planned behavior delta with a stable behavior ID,
   initial applicability, and either its mock-backed scenario/assertions or a concrete per-behavior
   non-applicability rationale and alternative verification.
7. Write `.todo/bugfix-<slug>/plan.md` with unchecked, independently verifiable writer-owned steps covering reproduction
   evidence, the minimal root-cause fix, regression tests, deterministic verification, and that behavior inventory.
8. When behavior can change, add a separate `## Workflow-owned verification` section describing the focused runtime
   Smoke scope and prerequisites without an implementation checkbox. Gate and Smoke own that work after verification.
9. Do not edit production files, commit, push, or invoke another workflow.

The generated Plan node owns routing and completion. This file is only the project bugfix planning procedure.
