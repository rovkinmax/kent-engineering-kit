# KEN-5 bounded development-slice comparison

## Measurement boundary

This record compares one disposable, source-only fixture slice. It is not a
claim about native Kent handoff latency, Linux behavior, GitHub checks, or
terminal cleanup. The fixture initialized a temporary Git repository, used a
counting `scripts/validate`, invoked the checkout child directly, and then
invoked the configured descriptor-launched report wrapper. The validator
counter is the measured unit for full-suite ownership. Targeted test methods
are counted separately and are never relabeled as full validation.

- Fixed point: `c506961e6498530fcb43636412e18a699c9f0f3d`
- Execution date: 2026-09-14
- Host platform: macOS (`darwin`), local existing Python 3.14
- Source identity before implementation: unavailable; the old child emitted no
  source identity.
- Environment identity before implementation: unavailable; the old child
  emitted no environment identity.
- Native Cleanup and task identities: not exercised or mutated.

## Before implementation

| Measure | Before | After |
| --- | --- | --- |
| Full validator invocations | 2 | 1 |
| Targeted wrapper/helper checks | 2 named test methods | 8 named test methods |
| Preview review rounds | 2 independent PASS reviews | 2 independent PASS reviews |
| Explicit source approvals | 1 | 1 |
| Protocol blockers/retries | 1 initial preview BLOCK, resolved before implementation | 1 initial preview BLOCK, resolved before implementation |
| Native handoffs | unavailable (`null`) in this fixture | unavailable (`null`) in this fixture |
| Native elapsed time | unavailable (`null`) in this fixture | unavailable (`null`) in this fixture |
| Direct child elapsed time (seconds) | 0.6978774999734014 | not run in after harness |
| Configured wrapper elapsed time (seconds) | 0.48923466610722244 | 1.601975625148043 |
| Identity lines retained in wrapper log | 0 | 2 matching stderr-only lines |

The controlled full-validation sequence was:

1. `./.kent/scripts/workflow-compile-verify` — exit `0`,
   `{"transition":"passed"}`.
2. `./.kent/scripts/workflow-verify-report` with
   `{"workspace_path":"<temporary fixture>"}` — exit `0`,
   `verification_status=passed`.

The counting validator recorded exactly `2` invocations. The direct child
stderr contained the fixture validation log but no `source_sha256` or
`environment_sha256` identity lines. The full machine-readable capture is
retained at
`build/kent-workflow/KEN-5/step-1/baseline-sequence.json`; the focused test
capture is at `build/kent-workflow/KEN-5/step-1/baseline-fixture.txt`.

Early production-shaped checks, run before production edits:

```text
python3 -m unittest -v \
  tests.test_kit_development_workflow.CompileVerifierTest.test_real_report_wrapper_descriptor_launch \
  tests.test_kit_development_workflow.CleanupPreparationTest.test_actual_preparation_to_janitor_admission_and_tombstone_replay
```

Result: 2 named test methods passed in 2.880 seconds. These tests used
isolated temporary roots; no native Cleanup or live task resource was used.

## After implementation

The after harness ran a focused identity/protocol check, then one real
descriptor-launched wrapper invocation. Its counting validator recorded
exactly `1` full validator invocation; the wrapper returned `passed`, retained
two matching identity lines in its content-addressed log, and exited `0`.
The focused identity check took `0.09512570803053677` seconds and the wrapper
took `1.601975625148043` seconds. The machine-readable capture, including
source and environment identity digests, is retained at
`build/kent-workflow/KEN-5/step-4/after-sequence.json`.

The after targeted matrix ran 7 named methods in 6.754 seconds:

```text
python3 -m unittest -v \
  tests.test_kit_development_workflow.CompileVerifierTest.test_real_report_wrapper_descriptor_launch \
  tests.test_kit_development_workflow.CleanupPreparationTest.test_actual_preparation_to_janitor_admission_and_tombstone_replay \
  tests.test_kit_development_workflow.CleanupPreparationTest.test_retry_after_append_before_seal_uses_same_final_event \
  tests.test_kit_development_workflow.CleanupPreparationTest.test_matching_archive_and_prepared_source_recover_without_overwrite \
  tests.test_kit_development_workflow.CleanupPreparationTest.test_conflicting_or_missing_preparation_proof_is_not_rebuilt \
  tests.test_kit_development_workflow.KitDevelopmentWorkflowTest.test_profile_and_full_command_closure \
  tests.test_kit_development_workflow.KitDevelopmentWorkflowTest.test_exact_graph_and_approval_delta
```

All 7 methods passed in isolated roots. No native Cleanup or live task
identity was used. The 2 -> 1 full-run change is a controlled ownership
measurement, not an end-to-end latency claim: native handoffs and elapsed
time remain `null`, and targeted checks are reported separately. The final
configured verifier remains the sole owner of complete source validation.
