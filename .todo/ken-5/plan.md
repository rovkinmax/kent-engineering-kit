# KEN-5: single-owner source verification

## Authority and explicit scope boundary

Root source: KEN-5 (`task-19f72f0b-63c1-46b3-a9f1-6aac84400085`) only.
Work kind: `feature`: change the Kit's source-development procedure to remove
duplicate full validation and add explicit evidence identity, without changing
review or effect authority. This is not an assessment-only documentation task.
Workspace: `/Users/rovkinmax/.kent/worktrees/kent-engineering-kit/KEN-5`.
Immutable task baseline: `c506961e6498530fcb43636412e18a699c9f0f3d`.
A moving PR target is an integration input, never a replacement baseline.

Authority is the supplied KEN-5 task body and the direct human answer to the
Plan Session's `ask_question` on 2026-09-14:
“Один полный прогон в verifier; ранние точечные интеграционные проверки;
без нового кэша исполнения” (option 1).
The question explicitly distinguished this architecture choice from source
approval. Original answer: Session
`f0293ad9-0387-4ce4-bfd5-177c8843380f`, 2026-09-14 16:23:25 +05,
independently readable with `kent questions list --session
f0293ad9-0387-4ce4-bfd5-177c8843380f --max-handoffs 1`; agent pointer
`comment-b666cac4-27f8-4380-b3d5-e9064ee1b7de` is not independent authority.
This plan does not replace or waive the task's outcomes.

Included:
- one owner of full source validation per unchanged development slice;
- early, bounded real-wrapper and terminal-helper fixture checks;
- source/environment identity and explicit invalidation when carrying the
  existing verifier report forward, not a cache that skips verifier execution;
- one coherent preview, existing two independent reviews and explicit approval;
- reference-based handoff evidence and a small before/after comparison.

Related evidence, not additional root issues:
- PR 5, `kent-roadmap-delivery-efficiency`, exact source
  `18a9b66a900742feeac968b112414620bb2d6873`, inspected through Git and
  `gh pr view 5`: OPEN and unmerged on 2026-09-14. Preserve it.
  Its September 3 sample is 21 calendar days, 83 revisions, at least 47
  governance/coordination/installation revisions, not a current global metric.
  Do not adopt its proposed risk tiers or revive its release portfolio.
- KEN-3's task/comment evidence reports a direct Implement validation and a
  subsequent actual configured wrapper validation of unchanged source
  `d9e3faee8d2bdc131cfa3743478a0e686320c11a`, each with 650 tests;
  the wrapper additionally reports five shell suites. Its retained wrapper
  log SHA-256 is
  `8eb0734f3dcd57b6d810865caa5d12952e853c1c41ad106e2562b46b71cd5453`.
  KEN-3 Done is not accepted terminal success. Its final event and incomplete
  cleanup are evidence, not authorization to mutate or replay that task.
- KEN-4 owns the concrete KEN-3 cleanup-postcondition correction. Its body
  independently identifies all four required resource postconditions and the
  false-success defect. KEN-5 does not repair deletion or Janitor admission.
- The human KEN-5 comment dated 2026-09-13T05:02:28Z was actually read:
  KEN-4 owns cleanup, KEN-7 repeated successful reads, KEN-16 accepted-decision
  provenance. The supported comment-list output did not expose its ID.
  This is a disclosed bookkeeping gap, not a claimed exact-ID supersession;
  the KEN-5 body and direct answer above independently bound this plan.

Dependencies: existing Python 3.11+, shell/Git/Ruby tools already used by
`scripts/validate`, the existing configured report wrapper and sibling runtime,
and the current project procedures. KEN-4 may merge independently; integrate
its target delta normally without importing its edit scope. No source change
in KEN-4 is prerequisite for KEN-5's wrapper/admission fixture evidence.
A live terminal-success claim would need KEN-4 and separate live authority;
neither is an acceptance claim of this source-only slice.

Excluded: KEN-4/7/16 implementation; native identity authentication or a native
inspector; shared cleanup/lifecycle redesign; risk-tier or governance-semantic
changes; live pilots; new workflows/revisions/defaults; native core; installs,
restarts, consumer adoption, publication, automatic task starts; custom
executors, result caches, measurement services or persistent monitoring.
No changes to GitHub checks, rebase policy, two-review ordering, required
external retention, terminal identities, seal protocol or no-auto-merge policy.

## Current behavior and design

`.kent/commands/implement.md` currently mandates relevant tests **and**
`./scripts/validate`, then sends work to dispatch. The project-owned
`.kent/scripts/workflow-compile-verify` independently executes that same
validator under the actual report wrapper. The wrapper executes the child by
file descriptor, supplies a replacement environment/private contained TMPDIR,
and retains a bounded content-addressed log. A bare validator PASS does not
prove those launch conditions.

The selected design removes the unconditional full run from writer procedures.
Implement and Fix run affected unit tests plus focused production-shaped
checks before handoff. The existing deterministic verifier remains the sole
full-suite owner and executes fresh on every invocation. No cached PASS,
new skip parameter, alternate dispatch, report schema or workflow edge.
Relevant changes after PASS go through existing Fix/verification routing.
The passing report is carried forward, not recalculated at every handoff.

### Early integration and terminal ownership

Use real configured wrapper -> descriptor-launched compile child with a
disposable validator sentinel, and existing real helper -> ledger/seal ->
Janitor-admission fixtures. Exercise regular and report-only accepted plans,
exact frozen recovery, retained readback, missing/conflicting proof and
descriptor/private-TMPDIR behavior. Use fabricated identities only inside
isolated test fixtures, never in this task's runtime environment.

Smallest existing-interface evaluation: keep `prepare_cleanup` as the single
terminal append/seal owner and the existing post-session Janitor as resource
retirement owner. Its private calls into plan-cache/ledger/Janitor internals
are real coupling, but moving them would change shared terminal interfaces
or add an adapter layer. Neither is necessary to remove duplicate verification.
KEN-5 therefore leaves helper, terminal schemas, recovery and cleanup
procedure unchanged and verifies the existing boundary early. Actual Kent
registration/branch absence is KEN-4's independent scope, not proved by
admission fixtures. No broad terminal redesign is proposed.

### Evidence identity without execution reuse machinery

Add a small project-local identity reader, not an executor. The compile child
records a bounded canonical identity line on stderr before and after full
validation. Its existing stdout is still exactly one of passed/failed/blocked.
Source identity covers canonical workspace, HEAD for attribution, tracked
working bytes/modes/deletions plus nonignored untracked source bytes and
symlink targets, and the actual verifier/wrapper/runtime/validator bytes.
Use sorted NUL-delimited Git inventory, not status-display parsing or mtime
as a content identity. Inventory includes the accepted plan/procedures.
Exclude only Git administrative data and generated ignored outputs; document
that ignored external input or dependencies cannot be covered by source hash.
Reject unreadable, unsupported, unstable or oversized inventories, with fixed
bounds (at most 20,000 entries and 256 MiB total hashed source, chunked reads).
Do not follow source symlinks outside the root; external targets make reuse
unproven. No automatic deletion or cleanup of unsupported entries.

Environment identity records OS/architecture, resolved selected Python/version
and executable digest, and resolved validator tool paths/versions/digests
for bash, sh, Git, Ruby, jq and existing `/usr/bin/python3` when used.
Hash relevant effective environment values, without exposing their raw values.
Include the actual report-wrapper replacement-environment contract/digest;
identify TMPDIR by its validated contained/private-directory contract, not its
per-invocation random basename. Tool resolution must follow the actual child's
PATH and fixed wrapper interpreter, not the writer shell's assumed selection.
Do not log secrets, a full environment dump, or broad source listings.
The identity module uses only the standard library and does not execute
validation, skip work, persist state or call Kent.

Use one closed versioned project-local identity object, not a shared runtime
schema: `schema`, `source_sha256`, `environment_sha256`, `head`,
`workspace_path`. Detailed bounded inventory is hashed, not embedded in logs.
Source/environment mismatch across the full run must not produce PASS;
use the existing blocked outcome with a bounded diagnostic. A failing
validator remains failed; identity failure never becomes success.

For downstream use, retain the existing typed verification report/log digest
and compare both identity lines, current source identity and tool/environment
identity. A commit-only HEAD change may preserve working-source identity but
requires explicit recorded attribution to the new commit, not an implicit
clean-HEAD claim. Plan/procedure edits invalidate source identity too.
Missing/tampered log, changed bytes/modes, untracked inputs, tool/environment
drift, platform change, unknown external/ignored dependency state, failed or
blocked results invalidate reuse. On uncertainty run fresh verification through
the existing route. No time-based grace period and no cross-workspace,
cross-task, installed-state or cross-platform acceptance. Independent Standards
review and GitHub checks are never replaced by this local report.

### One approval scope and reference-based handoffs

Keep this one preview as the source of truth for the coherent outcome.
The existing two reviewers and one source-approval boundary remain required.
Task-scoped fixes and bounded editorial/bookkeeping repairs stay within the
approved boundary; a material scope expansion returns to reviewed planning.
Git effects, live pilot and installed/consumer rollout retain their separate
authority; do not bundle them into source approval.

Project procedures/context guidance should pass the preview path/hash, review
and approval references, and existing report/log references through existing
carriers. Replace only optional copies: the writer's completion narrative
should reference its preview and targeted logs instead of reproducing them;
the PR-preparation narrative should reference the accepted preview and verifier
log instead of repeating their entire contents; human approval summaries
should identify this source package and its effects rather than paste reports.
Do not create a second preview/approval dossier for those handoffs.

Mandatory exception: the unchanged `verification_join_gate` instruction in
`workflowkit/delivery.py` and the frozen v2 snapshot requires full reports in
Gate `review_context`. Preserve that complete report copy, the typed reports,
append-only ledger events and external terminal retention. These required
copies are NOT counted as eliminated duplication. A regression assertion must
preserve the full-report Gate requirement in the unchanged graph. Removing
it would require a separately scoped generator/graph revision, not KEN-5.
Reference local files only while the
root exists; required terminal retention remains a real external readback.
This does not implement KEN-7's reading policy or KEN-16's provenance repair.

## Closed production-edit set and cross-module inventory

1. `.kent/commands/implement.md`: writer-owned targeted checks and full-suite owner.
2. `.kent/commands/fix.md`: same ownership on repair; fresh downstream verification.
3. `.kent/commands/ship-pr.md`: current identity/report-reference checks, no extra
   routine full run and no added Git effect authority.
4. `.kent/project-contract.md`: authoritative local verification ownership,
   identity limitations/invalidation and evidence-reference contract.
5. `.kent/context/implement.md`, `.kent/context/review.md`,
   `.kent/context/delivery.md`: narrow conditional references to that contract;
   preserve stage budgets, reports and cleanup's terminal exception.
6. `.kent/scripts/workflow-compile-verify`: bounded before/after identity
   logging and unchanged full validator invocation/stdout contract.
7. `.kent/scripts/kit-verification-identity.py` (new): project-local read-only
   identity calculation/CLI, not a configured workflow command or executor.
8. `tests/test_kit_development_workflow.py`: adapt existing compile/wrapper
   fixtures for the identity source and assert command/protocol compatibility.
9. `tests/test_kit_delivery_slice.py` (new): focused identity, wrapper,
   evidence-reference/ownership and bounded before/after regression coverage.
10. `README.md`: self-development paragraph only; reference authoritative
    ownership/early-check instructions rather than copy them.

Tracked task artifacts: this frozen plan, `progress.md`, and `comparison.md`
under `.todo/ken-5/`. Ignored logs/fixtures live under
`build/kent-workflow/KEN-5/`; existing ledger owns `.kent/runtime/`.
No other production files are approved by this preview.

No SDK/dependency upgrades, profile schema changes, runtime wire changes,
generated-contract adaptations or generated-copy edits are planned. The new
identity module is checkout-local project-owned source; the compile child's
path-based lookup must use cwd (the canonical selected workspace), because
the child itself is descriptor-launched. Existing profile command closure
must still validate with no registry expansion. Tests that copy the compile
child must copy the new helper explicitly and provide a source/tool fixture
that satisfies the real identity contract. Existing direct-child fixtures
lack Git and wrapper fixtures have unborn HEAD: build a disposable committed
fixture source for positive identity cases, confined to temporary test
repositories as existing tests do. Cover absent/unborn HEAD as blocked cases.
No production bypass or fake successful tool identity is permitted for tests.
These adaptations stay inside `tests/test_kit_development_workflow.py`.
If inspection proves another source
adapter is required, stop for bounded preview revision rather than editing
generated copies or widening behavior.

Preserve existing typed transition/status/report/identifier contracts.
No string routing is added; new identity JSON has an exact closed shape.
No native Kent configuration/role/graph/module changes are required.

Graph delta: **none**, including serialized v1/v2 snapshots and task-backed
graphs. Rollout: checkout-local source changes and deterministic fixture checks
only, then separately authorized normal PR delivery. Rollback: a separately
authorized source revert of this closed change, preserving task evidence;
no rollback is executed by this plan. Restart impact: none. Locked sessions
retain their prompts; no claim that new procedure text hot-reconfigures other
running tasks. A live pilot needs a separate scoped effect approval.

## Ordered writer-owned implementation

Progress checkboxes are maintained in `progress.md`, not in this frozen preview.
Production edits require both same-hash independent PASS reviews followed by
explicit source approval and the normal Plan Contract.

1. **Before any production edit**, Implement owns the red/baseline capture.
   Add test-only regression fixtures; capture that old compile output lacks
   source/environment identity and the old procedure requires a redundant
   writer full run. Capture the old sequence with a counting disposable
   validator: direct full invocation then real configured wrapper invocation.
   Run existing focused wrapper and helper/admission checks in isolated roots
   now, not after a complete feature implementation. Retain command, source,
   environment, exit/result and elapsed-time evidence. This fixture baseline
   must not run native Cleanup or mutate any task identities.
2. Implement the identity reader and child integration, with precise path,
   source/environment bounds and failure semantics. Run focused tests after
   changes. Preserve all existing wrapper safeguards and report schema.
3. Update the named procedure/contract/context/README surfaces as one coherent
   ownership change. Use references to the one local contract; preserve
   review, effect, retention and graph boundaries. Add contract regressions.
4. Run the same bounded fixture development slice after the changes. Compare
   direct-full plus wrapper before versus focused checks plus one real wrapper
   after. Re-run the targeted regression matrix; include actual helper
   admission/recovery tests and unchanged generated command-closure/spec checks.
   Update `comparison.md` and append evidence with truthful limitations.
5. Finish writer bookkeeping and plan progress **before** the final full-suite
   handoff, so tracked changes do not immediately invalidate full verification.
   Supply the preview/hash, source identity, targeted logs and comparison path
   through existing carriers. Do not run an extra writer `./scripts/validate`.

A missing writer-owned pre-edit capture is not a user approval request.
Reconstruct against the immutable baseline in disposable fixtures if safely
bounded, label it reconstructed, and preserve the gap; never fabricate a
historical red run. If reconstruction cannot prove the original condition,
report the acceptance gap through existing workflow review.

## Workflow-owned evidence and acceptance

These are owners, not additional writer checklist prerequisites:

| Artifact/outcome | Owner | Production-edit boundary |
| --- | --- | --- |
| Frozen preview/hash and first independent receipt | Plan + one read-only leaf | Plan artifacts only |
| Second same-hash receipt | Separate Plan Review node | None |
| Explicit approval and accepted normalized snapshot | Existing approval/Plan Contract | Existing task/runtime contract only |
| Red, early fixture runs, before/after comparison | Implement | Test-only before red; then closed set |
| Full actual configured report, log and identity lines | Deterministic verifier | None; ignored output only |
| Independent Standards and Join/Gate decision | Existing workflow stages | None |
| Current delivery evidence and authorized PR | Existing delivery owner | Only separately authorized Git effects |
| External retention/final seal/resource postconditions | Existing Cleanup/Janitor | Unchanged terminal contract; no KEN-5 repair |

Acceptance matrix:
- Full validation invocations: measured controlled fixture sequence **2 -> 1**
  for unchanged source; actual final configured verifier must pass once on
  the finished source. Targeted unit/integration invocations are counted
  separately, never relabeled “free.”
- Wrapper coverage: real descriptor launch, private/contained TMPDIR,
  stderr-only identity, strict child stdout, passing/failing/blocked outcomes,
  log digest verification, and source/tool/environment mutation invalidate PASS
  or subsequent reuse. Missing identity/log and unsupported inputs fail closed.
- Source identity: dirty tracked bytes, modes, deletion, untracked addition,
  symlink/external input, tool replacement, environment/platform change,
  bounds and unstable reads; no source mutation by the reader.
- Terminal coverage: existing actual helper/admission and frozen-recovery
  fixture tests PASS; no claim of live deletion/postcondition qualification.
- Approval/review: one coherent source package, **two** independent preview
  reviews plus **one** explicit source approval; existing Standards and separate
  Git/live/installation effect gates unchanged. No fabricated reduction in
  native approval rounds: distinguish measured task history from expectations.
- Evidence/context: one preview, one typed verifier report with its log,
  references replacing the named optional narrative copies; mandatory full
  reports in Gate `review_context` and ledger/retention copies remain.
  Compare optional narrative bytes/reference count on the controlled slice
  without treating mandatory Gate/runtime records as redundant.
- Protocol blockers, handoffs and elapsed time: record before/after from the
  same controlled harness and report this task's actual observed rounds
  separately. Native handoff/approval latency unavailable to a fixture is
  `null`, not zero or a claimed speedup. Baseline KEN-3 historical metrics stay
  labeled historical and incomplete. No new persistent measurement layer.
- `./scripts/validate` passes through the actual configured verifier; existing
  GitHub checks remain required and Linux results are not inferred from macOS.
- Closed file set, frozen graphs, installed-primary and unrelated resources
  remain preserved. No unauthorized pilot/effect or scope absorption.

`comparison.md` contains explicit units, sample boundaries, raw artifact
references, and before/after columns for full runs, targeted runs, review
rounds, approvals, protocol blockers, handoffs and elapsed time. Retain failures
and retries as additional rows, never rewrite the baseline to improve results.
Do not claim lower end-to-end latency from sentinel timing alone.

Risks: a fingerprint is a cooperative freshness check, not native identity
authentication or hostile same-UID isolation; ignored/external dependencies
may require fresh verification; hashing adds measurable overhead; plan updates
can invalidate receipts; frozen sessions retain old instructions; a test harness
does not prove live workflow latency; unrelated KEN-4 target changes can require
integration verification. Each is explicitly bounded above, not waived.
