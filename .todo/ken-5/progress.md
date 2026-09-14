# KEN-5 progress

## Plan

- [x] Read the Plan manifest and required contracts.
- [x] Inspect task authority, PR5, KEN-3 evidence and KEN-4 scope.
- [x] Select `feature` and obtain the direct human architecture choice.
- [x] Write scope boundary, design, dependency inventory and bounded preview.
- [x] Obtain first independent read-only PASS on the frozen preview hash.
- [x] Append Plan evidence: sequence 1,
  `cdb1c95989a32b89813ac7cdb3b7059b810f95d74f97e8ed817034c919584c09`.
- [x] Audit preview/hash, authority, evidence owners and acceptance; prepare
  the separate Plan Review handoff. Kent owns the actual transition state.

## Writer steps (only after both reviews and explicit approval)

- [x] Capture pre-production red/baseline and early wrapper/helper fixture
  evidence. Added `tests/test_kit_delivery_slice.py` baseline fixtures and
  recorded the old direct-child plus configured-wrapper sequence in
  `comparison.md` and the ignored
  `build/kent-workflow/KEN-5/step-1/baseline-sequence.json`: the counting
  validator ran twice, both outcomes passed, and the old child emitted no
  source/environment identity. The focused real-wrapper and helper/admission
  tests passed in isolated roots (2 named methods, 2.880 seconds). No
  production source, native Cleanup, or task identity was changed.
- [x] Implement bounded source/environment identity and child logging. Added
  the standard-library-only `.kent/scripts/kit-verification-identity.py`
  reader and integrated before/after identity lines into
  `.kent/scripts/workflow-compile-verify`. The reader hashes bounded Git
  source inventory (tracked, deleted and nonignored untracked files, modes and
  in-root symlink targets), selected Python/tools, platform and hashed
  effective environment values; it blocks on missing HEAD, unsupported or
  external source symlinks, and source/environment identity uncertainty.
  Existing compile/wrapper fixtures now use committed disposable Git roots
  and copy the identity reader. Focused identity, mutation, environment,
  strict-output and real-wrapper tests passed (12 tests). An earlier
  configured full run before these fixture corrections is retained under
  `build/kent-workflow/KEN-5/step-2/` and failed only on six stale test
  assumptions; no final full-suite acceptance is claimed here.
- [x] Update the closed procedure/contract/reference surfaces and regressions.
  Implement, Fix and Prepare PR now assign the complete fresh
  `./scripts/validate` run to the configured verifier, require matching
  report/log and identity references, and route uncertainty to fresh
  verification. The project contract is the normative ownership/identity
  source; Implement, Review and Delivery context manifests and README point
  to it while preserving mandatory full Gate reports and terminal retention.
  Added contract regressions for ownership, identity fields, references and
  unchanged graph-sensitive governance surfaces. Focused documentation,
  contract, graph and identity tests passed (10 tests).
- [x] Capture after-slice comparison and targeted integration verification.
  The same disposable counting fixture now measures focused identity/protocol
  checks plus one real descriptor-launched wrapper run: one full validator
  invocation after versus two before, with two matching identity lines retained
  in the wrapper log. The after capture is
  `build/kent-workflow/KEN-5/step-4/after-sequence.json`; the targeted matrix
  covers the real wrapper, helper/admission, frozen-recovery/replay,
  conflicting-proof, command-closure and unchanged graph/spec checks and
  passed 7 named methods in 6.754 seconds. `comparison.md` records units,
  before/after columns, raw artifact references, null native handoff metrics,
  and the limitation that the fixture is not an end-to-end latency claim.
- [x] Finish writer bookkeeping and hand off to the sole full-suite owner.
  Revalidated the accepted preview hash, fixed point, frozen v2 spec digest,
  comparison artifact and current source/environment identity. The handoff
  identity is retained at
  `build/kent-workflow/KEN-5/step-5/writer-handoff-identity.json`; the writer
  has not run another full `./scripts/validate`. All five writer-owned steps
  are complete. Final full validation, read-only Standards/Gate review,
  delivery and terminal acceptance remain workflow-owned.

The workflow owns independent review, full verification, Git delivery and
terminal cleanup. They are not writer implementation steps.

## Fix: VG-IDENTITY-01 (2026-09-14)

- [x] Reconcile the single Gate bundle against immutable baseline
  `c506961e6498530fcb43636412e18a699c9f0f3d` and the unchanged approved
  preview. The identity reader is task-added; neither finding is baseline
  cleanup. The older missing-report finding is superseded by Gate's retained
  report/log `854fdbcee0dfb2a589d7612fa9e99f37fad0074093d862fc27483fcbe8ada4b8`.
- [x] Reproduce tool mutation before repair and fail closed on changed
  executable bytes, inode, mode, removal, symlink replacement and transient
  byte restoration. Metadata is checked around both bounded hashes and the
  version probe; a second hash independently detects byte changes. Actual
  pre-fix output capture was unbounded before truncation, contrary to the
  incoming summary: the repaired probe bounds combined output while reading,
  rejects overflow/timeouts and reaps its process.
- [x] Reproduce invalid TMPDIR acceptance before repair. Unset, empty,
  external, nonexistent, non-directory, looping, wrong-owner, non-0700 and
  externally redirected workflow roots cannot yield an identity. Genuine
  contained private directories retain basename-independent identity.
  Direct fixtures now establish the same private TMPDIR contract as the
  real wrapper; no production bypass or environment override was added.
- [x] Run the final focused identity/child/wrapper/workflow matrix:
  **50 tests passed in 28.694 seconds**, retained in
  `build/kent-workflow/KEN-5/fix/final-focused-matrix.log`
  (SHA-256 `ac455cc700ab466aac6e361b55c18b34a3c441ab0ba50d4a026c34484ddf1b9d`).
  The separate two-test wrapper/helper-admission check passed in 3.447 seconds,
  retained in `build/kent-workflow/KEN-5/fix/after-wrapper-helper.log`.
  Python syntax compilation, child shell syntax and `git diff --check` pass.
- [x] Preserve reproduction and intermediate failures under the same ignored
  Fix directory. Corrected test-only assumptions about selected-Python PATH
  precedence and wrapper log-safety precedence: the child preserves validator
  failure, while the real wrapper blocks unsafe TMPDIR/log retention even
  when validation fails. Subsequent fixture comparison captures use unique
  directories so the original after-sequence evidence is not overwritten.
- [x] Prepare fresh-verification handoff. Fix changed only the identity reader,
  its two approved test files and this append-only progress section.
  All existing unrelated writer changes and the frozen plan remain intact.
  No full `./scripts/validate`, Standards, Gate or Smoke stage was duplicated
  in Fix; the previous report is historical, not reusable after these edits.

The configured verifier now owns the fresh complete source run after this
bookkeeping, with a retained typed report/log and matching identity lines.
Standards/Gate acceptance remains downstream and is not claimed by these
focused checks. There are no remaining compatible writer findings or external
blockers; no Git, live workflow, installation or consumer effects occurred.
