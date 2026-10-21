# Objective

Implement one behavior from `$ARGUMENTS` through a red-green vertical slice.

# Process

1. Identify and confirm the public test seam.
2. Add one focused behavior test with an independent expected result.
3. Run it and observe the intended behavior failure, not an unrelated setup
   error. A missing new public symbol may be a legitimate red when the test
   expresses that required interface.
4. Implement only enough behavior to pass.
5. Re-run the focused test without weakening its expected behavior to pass.
6. Run existing affected tests and the nearest project compile or integration
   check on the final edited state.
7. Stop after the requested slice unless the user asks to continue.

Avoid tests coupled to private methods, internal call counts, or implementation
structure.
