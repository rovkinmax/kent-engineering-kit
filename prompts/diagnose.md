# Objective

Diagnose `$ARGUMENTS` through a tight, reproducible feedback loop.

# Process

1. Produce one agent-runnable command that exercises the exact symptom and can
   return a failing verdict.
2. Run it and minimise the scenario until every remaining element is load-bearing.
3. Rank falsifiable hypotheses supported by the observations.
4. Test one variable at a time with targeted instrumentation.
5. If repair is authorized and a correct public seam exists, add a failing
   regression test before the fix.
6. Apply the smallest root-cause fix only within existing repair authority.
   Diagnosis alone authorizes no source mutation.
7. After an authorized repair, re-run the focused test, original reproduction,
   and applicable project verification.
8. Remove temporary instrumentation and state the confirmed cause.

If reproduction is unavailable, continue useful bounded read-only diagnosis.
Separate established facts from hypotheses and name the missing access or
artifact; do not claim a confirmed cause or repair.
