# Prepare PR

Confirm passing current verification/review evidence, approved scope and
explicit Git delivery authority. Source-only approval is insufficient.
Inspect actual branch name, HEAD, immutable task baseline and current merge
target; never reconstruct branch identity from the Task ID.

Before preparing delivery, confirm the current typed verification report and
retained log reference, plus matching before/after source and environment
identity references for the exact source being delivered. Optional narrative
handoffs reference the accepted preview and verifier artifacts rather than
copying their complete contents. Missing, tampered or mismatched identity
requires fresh verification through the existing Implement/verification path;
do not run a second routine full validator in Delivery.

When the applicable future workflow explicitly permits delivery, commit and
push only the task branch and prepare its PR. Never commit unrelated changes,
push directly to main, merge a PR, or bypass repository checks. Preserve the
installed primary and consumer/global state. Integration changes requiring
repair return through Fix and fresh verification.

Report exact branch, PR URL, head/base OIDs and evidence via generated
parameters. Existing GitHub automation may run naturally; this flow does not
add a separate Kent CI preparation/watch/monitor stage.

The human-selected Kit PR policy is `rebase`, as declared in the profile.
Use the existing strategy resolver and delivery-role method checks; do not
fall back to another method when policy or PR state conflicts. Before and
after publishing/updating the PR, verify applicable repository/ruleset
permission; for an existing PR, GitHub `canBeRebased` must be true before
declaring rebase feasibility. Unknown state needs fresh readback, not an
assumed PASS. A confirmed blocker follows the existing delivery recovery
gate. The selected method is a delivery requirement, never merge authority.

For explicitly approved report-only qualification, create no commit, push,
PR, tag or release. Prove no tracked/staged writes and exact HEAD equality
with a current published branch tip; preserve that branch through qualification.
Use the existing `report_only`/`no_pr` disposition and carriers, with explicit
authority and retained report evidence, then proceed to owned cleanup.
