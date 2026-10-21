# Prepare PR

Require passing current-stage verification/review evidence and explicit Git
delivery authority. Source-only approval grants no publication. Read actual
branch, HEAD, immutable baseline and current target; never derive the branch
name from Task ID. Follow `.kent/project-contract.md` for report/log identity
and freshness; Delivery does not repeat routine full validation.

When authorized, commit/push only approved Task changes and prepare its PR.
Do not commit unrelated changes, push main, merge or bypass checks. Preserve
installed primary and consumer/global state. Task-scoped corrections return
through Fix and affected verification; new scope/effects need approval.

Use profile `rebase` policy and existing strategy/method checks. Verify
repository/ruleset permissions around publication; an existing PR needs
GitHub `canBeRebased=true` before claiming rebase feasibility. Unknown state
requires readback. Method selection is not merge authority.

Return exact branch, PR URL, head/base OIDs and evidence through declared
carriers. Retain later acceptance obligations with owner/milestone: a
review-ready PR may still have external UNVERIFIED work and is not full
business acceptance, merge/release or Task Done. No Kent CI monitoring stage
is added.

For explicit report-only qualification, create no commit, push, PR, tag or
release. Prove no tracked/staged writes and exact HEAD equality with a current
published branch tip; retain that branch through qualification. Use authorized
`report_only`/`no_pr` carriers and proceed to owned cleanup.
