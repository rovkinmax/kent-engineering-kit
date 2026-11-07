# Wayfinding

## Objective

Map a large or uncertain effort without prematurely turning unknowns into
implementation tasks.

## Process

1. Define the destination and explicit scope boundary.
2. List facts, accepted constraints, decisions that can already be stated
   precisely, and unresolved questions.
3. In the map, distinguish pending human questions, agent proposals, and
   accepted constraints. Link accepted decisions to their original
   decision-locator sources; a proposal or unresolved question is not an
   accepted constraint.
4. Separate unresolved items into:
   - research questions that agents can resolve;
   - prototype questions requiring a runnable artifact;
   - human product or architecture decisions;
   - operational prerequisites.
5. Record dependencies and identify the current unblocked frontier. Continue
   independently ready accepted work with its existing owner; hold only work
   that depends on an unresolved prerequisite.
6. Run independent research questions in parallel.
7. Resolve one human decision at a time.
8. Store each answer once and link to its original source from the map.
9. Add newly visible decisions as earlier uncertainty clears.

Stop when the path is clear enough to produce a specification and verifiable
implementation slices. Do not implement the destination inside wayfinding.
