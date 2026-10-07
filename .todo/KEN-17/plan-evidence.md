# KEN-17 Plan evidence

## Planning checklist

- [x] Read Plan manifest and required contracts; classify as bugfix.
- [x] Inspect root Task authority, incident pointers and KEN-4 overlap.
- [x] Establish immutable baseline and current producer/helper/consumer behavior.
- [x] Write explicit scope boundary before implementation planning.
- [x] Draft exact closed file preview, ownership, dependencies and evidence order.
- [x] Integrate bounded grill critique before freeze.
- [x] Freeze preview; obtain one independent read-only same-hash PASS.
- [x] Retain preview/hash/first receipt in Task evidence.
- [x] Append required Plan evidence.
- [ ] Hand off to separate Plan Review.

## Bounded discovery checks

2026-10-06, source baseline
`ad7d7c8ad512c3c2b8d40fd0f0befed852e54035`:

`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest`
with these selected tests:

- `tests.test_kit_development_workflow.CleanupPreparationTest.test_real_sealed_plan_snapshot_is_admitted_by_janitor`
- `tests.test_kit_development_workflow.CleanupPreparationTest.test_actual_preparation_to_janitor_admission_and_tombstone_replay`

Result: 2 tests PASS. Actual normal producer and helper integration exists for
the Kit opt-in profile. The direct-sealed-plan test proves baseline acceptance,
not adequate external retention or strict negative admission.

Second bounded run:

- `tests.test_workflow_runtime.WorkflowJanitorTest.test_v2_opaque_tombstone_entries_are_deleted`
- `tests.test_workflow_runtime.WorkflowJanitorTest.test_v2_managed_retention_preserves_opaque_bytes`

Result: 2 tests PASS. Current arbitrary private regular files can be retained
and deleted; the original strict failure is no longer the current implementation.
These are discovery controls, not post-change verification or red evidence.
No full validator run, installation, native Task replay or consumer effect.

History pointer: `1873f0811f9748af921b3f3e9791ec6020880354`
introduced opaque deletion. Its parent
`b185756afad9683becda84799ecf209124a47450` contains the actual
`unexpected tombstone entries` strict consumer; bounded read confirmed the
original four-name/validated-CI admission seam and terminal-chain validator.
The future writer can extract exact source bytes from this Git object into
a disposable fixture; no production checkout or live logs are needed.

Tooling notes: initial searches referenced nonexistent directories/globs
(`lib`, `workflows/fragments`, `templates/project/commands`,
`templates/workflows`, `scripts/test*`) and nonexistent documentation
(`contracts/evidence-ledger.md`, `contracts/project-profile.md`).
Corrected by repository file inventory and explicit known paths; these were
read-only discovery misses, not verification failures. One short background
poll was rejected; wait with the supported longer interval.

## Governance receipts

Grill Session: `ace4e0c4-cd46-4d60-a315-9d0125f4144b`.
No production edits, approval or formal review claimed yet.

## Grill dispositions

Critiqued draft hash:
`268b53b7d1195ca01051b7012106caa227fcb1c9f98ade83459d496122264d8c`.
The critique is not an independent PASS receipt.

- Accepted materialization finding: bootstrap uses `update=False`. Preview now
  specifies the existing scoped synchronizer API with `update=True`, baseline
  target checks and closure readback. No builder/synchronizer source expansion.
- Accepted no-helper validation finding: named existing read-only ledger/runtime
  primitives, original retained proof obligations and a separate actual
  no-helper recovery fixture with zero append/seal. Native provenance remains
  caller-owned, not authenticated by synthetic test IDs.
- Accepted managed-legacy gap: separate inner-directory admission before native
  deletion and both legacy positive and strict negative fixtures.
- Accepted exact interruption gap: inject after durable seal and before completed
  receipt write; validate retained tombstone chains on completed reuse.
- Clarified historical reproduction with the actual pre-opaque Git source,
  not an imitation strict predicate. Current-source regressions stay portable.

All dispositions remain inside root task authority and the closed preview.
No product decision, new effect or source edit was introduced.

## First independent preview review

Frozen preview: `.todo/KEN-17/plan.md`.
Raw ScopeHash:
`e9038fa5abbca3dd4b0d6acbc441ad4096163e5da11c5c99ad9dc2b8bbb0d4f6`.
Reviewer: `architecture-designer`, Session
`09a581b9-e03e-426c-bc7c-f4e0f0879cc5`, 2026-10-06.
Verdict: PASS; hash verified before and after inspection. No blocking findings.
Native reviewer response in that retained Session is the original receipt.
Authority, 11-path boundary, pre-edit red ordering, actual historical source,
strict managed legacy/v2 admission, helper/absent-opt-in recovery, identity and
privacy boundaries, scoped materialization and no-live-effects were reviewed.
No production writes, effects, delegated reviews or approvals.

Reviewer caveat: an initial direct v5 renderer-equality check failed. The
reviewer then verified the existing intentional historical v5 baseline and
allowed G1/G2 delta; corrected baseline check passed. Do not regenerate v5
to erase that pre-existing difference. Current v6 parity and command closure
passed in read-only inspection. Full suites/validator were not run.
Two initial reviewer runtime-path searches failed and were corrected via
`PROJECT_COPIES`; no evidence or authority impact.

Durable Task pointer:
`comment-2de7aea5-7aed-4537-ad28-b892c89d98c9` retains authority,
ScopeHash, preview/evidence paths, first receipt identity and pending gates.

## Completion audit

Root authority/scope/dependencies are in the plan's first section; file,
graph, rollout/rollback/restart preview follows discovery. Producer/consumer
and sealed-retry red-first requirements are the writer's first step. Strict
negative/privacy/partial/native-identity fixtures and evidence owners are
explicit acceptance mappings. Current permissive baseline and missing-opt-in
append defect are distinguished from the original incident. KEN-4 is Done
and its landed postconditions are preserved. No product/API/UX/safety decision
remains unresolved. Implementation, full verification, second review and human
approval are not claimed complete.

Plan ledger receipt: sequence `1`, event hash
`7894924c94ca3f077a1a3be0df4e454744256f8984c21f1f64c73d87984573f0`.
Append succeeded without deduplication. On recovery reuse this receipt; do not
append another ordinary event for this same Kent run.
