---
description: Implement one approved bugfix plan step
---

# Bugfix Implementation

1. Read `AGENTS.md`, `.kent/project-contract.md`, the selected `plan.md`, current task comments, and applicable rules.
2. Read `.kent/commands/mock-scenario-policy.md`, then select exactly one ready unchecked writer-owned plan step and
   inspect preserved changes before editing.
3. Reproduce the defect or execute the plan's deterministic proxy before changing code when feasible.
4. Fix the proven root cause with the smallest coherent change. Follow generated SDK source-of-truth rules when relevant.
5. Add or update focused regression tests; refresh the final-delta behavior inventory and execute the selected harness
   with the packet, receipt, and review envelope required by `mock-scenario-policy.md`.
6. Run the step's verification. In any worktree use `./tools/agentw`.
7. Mark the step `[x]` only after its focused checks pass.
8. Do not execute runtime Smoke or another workflow-owned review/delivery item. If a legacy plan renders one as an
   unchecked step, leave it unchanged, carry its scope into `review_context`, and advance to verification when no
   writer-owned step remains.
9. Do not run nested final reviewers, commit, or push. The generated workflow owns review and delivery.

The generated Implement node owns transition parameters and continuation.
