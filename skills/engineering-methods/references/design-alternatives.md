# Design alternatives

## Objective

Design materially different interfaces for the current bounded design
question.

## Method

1. State constraints, dependencies, invariants, and failure modes.
2. Identify existing supported routes that can satisfy the accepted outcome;
   include reuse of such a route when feasible.
3. Compare designs with different priorities:
   - minimum surface area;
   - easiest common-case usage;
   - maximum justified flexibility;
   - ports and adapters when a real external seam exists.
4. Compare depth, locality, testability, migration cost, and operational risk.
5. Distinguish necessary tools, roles, or infrastructure from optional
   implementation means.
6. Recommend one design or a deliberate hybrid.

Use callable read-only architects when independent investigation materially
helps the decision. Required project governance reviews remain mandatory.

Do not edit production code or introduce abstractions for speculative needs.
