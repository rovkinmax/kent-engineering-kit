# Objective

Diagnose `$ARGUMENTS` through a tight, reproducible feedback loop.

# Process

1. Produce one agent-runnable command that exercises the exact symptom and can
   return a failing verdict.
2. Run it and minimise the scenario until every remaining element is load-bearing.
3. Write three to five falsifiable, ranked hypotheses.
4. Test one variable at a time with targeted instrumentation.
5. When a correct public seam exists, add a failing regression test before the fix.
6. Apply the smallest root-cause fix.
7. Re-run the focused test, original reproduction, and project verification.
8. Remove temporary instrumentation and state the confirmed cause.

If no honest reproduction loop can be built, stop and report the missing access
or artifact instead of guessing.
