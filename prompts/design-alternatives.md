# Objective

Design materially different interfaces for `$ARGUMENTS`.

# Method

1. State constraints, dependencies, invariants, and failure modes.
2. Run independent `architecture-designer` roles in parallel with different
   priorities:
   - minimum surface area;
   - easiest common-case usage;
   - maximum justified flexibility;
   - ports and adapters when a real external seam exists.
3. Compare depth, locality, testability, migration cost, and operational risk.
4. Recommend one design or a deliberate hybrid.

Do not edit production code or introduce abstractions for speculative needs.
