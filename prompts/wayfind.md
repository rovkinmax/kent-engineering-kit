# Objective

Map a large or uncertain effort described by `$ARGUMENTS` without prematurely
turning unknowns into implementation tasks.

# Process

1. Define the destination and explicit scope boundary.
2. List decisions that can already be stated precisely.
3. Separate them into:
   - research questions that agents can resolve;
   - prototype questions requiring a runnable artifact;
   - human product or architecture decisions;
   - operational prerequisites.
4. Record dependencies and identify the current unblocked frontier.
5. Run independent research questions in parallel.
6. Resolve one human decision at a time.
7. Store each answer once and link to it from the map.
8. Add newly visible decisions as earlier uncertainty clears.

Stop when the path is clear enough to produce a specification and verifiable
implementation slices. Do not implement the destination inside wayfinding.
