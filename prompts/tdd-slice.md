# Objective

Implement one behavior from `$ARGUMENTS` through a red-green vertical slice.

# Process

1. Identify and confirm the public test seam.
2. Add one focused behavior test with an independent expected result.
3. Run it and observe the intended failure.
4. Implement only enough behavior to pass.
5. Re-run the focused test.
6. Run the nearest project compile or integration check.
7. Stop after the requested slice unless the user asks to continue.

Avoid tests coupled to private methods, internal call counts, or implementation
structure.
