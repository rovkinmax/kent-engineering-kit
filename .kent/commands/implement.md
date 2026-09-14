# Implement

Verify approved preview, both same-hash PASS receipts, human approval and
plan-contract input before editing. Work only in the selected managed root
and approved file set. Preserve existing unrelated changes and the immutable
task `fixed_point`; do not mirror Kent lifecycle in project metadata.

The writer is continuous. Implement ready writer-owned steps, updating only
their progress and appending evidence as each completes. Do not add review,
verification, PR, CI or cleanup checkboxes as writer prerequisites. Hard bugs
require deterministic reproduction before production edits. Use authoritative
template sources and explicit approved materialization for generated files;
never patch generated copies.

Run affected unit tests and focused production-shaped checks from this
checkout, including the configured wrapper and any applicable terminal-helper
admission fixtures. The configured verifier owns one fresh full
`./scripts/validate` run after writer bookkeeping; the writer does not
duplicate that full run. Carry the accepted preview, verification
report/log and before/after identity references through the existing
handoff. Missing or mismatched identity requires fresh verification.
No `--installed-state`, installs, global changes, Android/device operations
or consumer rollout. Report failures truthfully.
Pass finished work to existing verification dispatch; deterministic checks
and Standards review are read-only and return through Join/Gate.

For explicitly report-only/no-repair authority, perform inspection and
verification only: no tracked or staged writes, including plan progress.
Report candidate defects for repair under separate authority.
