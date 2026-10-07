# KEN-17: Terminal evidence admission

Work kind: `bugfix`. Status: proposed; no production edits authorized.
Immutable task baseline: `ad7d7c8ad512c3c2b8d40fd0f0befed852e54035`.

## Authority and scope boundary

Root source: KEN-17, native Task
`task-c7a9ab07-2bad-4976-9ced-06a1075f9df1`, original task body, retrieved
2026-10-06. This plan implements that body; it does not supersede it.
The task's one-time OSM-90 recovery is incident context, not source approval.
Related evidence: OSM-90 retained Cleanup Session
`a24862b9-1704-405b-8305-3c349411d3d6`, event 272, and its recorded final
ledger hash in the task body. No live reads/replays or history repair are
needed for portable fixtures. Agent comments dated 2026-09-14 identify
the subsequent bytecode side effect and retry instruction mismatch; they
are investigation pointers, not additional human scope authority.

Included: producer-to-terminal storage integration, strict terminal admission,
validated original successful-seal recovery, concrete blocker instructions,
and read/admission bytecode cleanliness. Dependencies are the existing
Plan Contract producer, project-owned preparation opt-in, evidence ledger,
runtime validators, Janitor, source materializer and configured verifier.
No SDK, dependency, schema, enum, status, action, identifier or carrier change.
Keep existing typed request/report/marker contracts through all boundaries.

Excluded: KEN-4 deletion-result/postcondition work (native Task now Done;
reuse its landed behavior, do not rewrite it), KEN-7 tool-read loops,
KEN-9 release ownership, KEN-21 rejected evidence recovery, KEN-22 delivery
continuity, native-core changes, device/token ownership tools, CI expansion,
OSM-90 replay, consumer changes, activation/install/restart, remote effects,
manual state repair and a replacement terminal protocol. Sharing Janitor
does not merge these scopes. Any unrelated findings remain separate.

## Discovery and changed baseline

- The original incident describes a restrictive Janitor blocking the normal
  `plan-contract.json` producer output and seven ad hoc resources.
- Current baseline differs: commit
  `1873f0811f9748af921b3f3e9791ec6020880354` introduced opaque-file deletion.
  `_EvidenceAdmission` now opens private regular arbitrary files and permits
  their unlink without payload validation. Existing tests expressly accept
  `payload.bin`, temporary files and an opaque plan cache. This is not the
  original eight-entry failure and cannot be represented as unchanged code.
- Kit's existing preparation helper already retains/verifies the accepted
  six-field plan cache, retires that exact cache before seal and preserves
  unknown resources. Its producer-to-Janitor and completed/tombstone replay
  tests pass at this baseline (two focused tests, 2026-10-06).
- The helper's runtime inventory currently excludes otherwise supported
  referenced CI archives. A completed receipt with the active task directory
  still invokes the idempotent `seal` command on retry.
- `workflowkit/delivery.py` only suppresses the ordinary transition append
  for nonempty `prepare_cleanup`; missing/empty profiles still mandate an
  append on every Cleanup attempt. That is the incident's recovery seam.
- Janitor dynamically imports its runtime sibling without disabling bytecode.
  Dirty guards must stay intact; fix the source read side effect.

These changes are proposed under KEN-17's explicit strict-negative and recovery
requirements. Do not broaden opaque admission to solve producer integration.
No claim of prior approval of this proposal or of superseding other tasks.

## Exact production preview

Closed editable source set:

1. `templates/project/workflow-task-janitor`: reject unknown names in both
   terminal v2 and legacy retirement, before rename/sentinel/deletion effects;
   cover managed legacy admission in `_admit_managed_runtime_parent`, not
   only legacy unlink. Validate legacy task-directory entries before native
   deletion; missing v2 support/marker is not itself a blanket legacy ban.
   preserve existing descriptors, locks, bounds, no-follow, single-link,
   ownership/mode, Git-ignore, CI-reference and drift checks. Disable import
   bytecode writes before loading the sibling. Keep KEN-4 postconditions.
2. `.kent/scripts/workflow-task-janitor`: byte-identical generated copy,
   materialized with the existing synchronizer's scoped
   `plan_synchronization(..., key="janitor", update=True)` followed by
   `apply_synchronization(plan)`. Load that synchronizer through the existing
   builder's `load_synchronizer`; do not run bootstrap's `update=False` or
   a broad CLI update. Before applying, verify the generated target equals
   this task's baseline template bytes and is a safe regular owner-executable
   file. After applying, call `verify_command_closure` and confirm only the
   approved Janitor copy changed. Do not edit builder/synchronizer or delete
   the existing target to bypass their checks.
3. `.kent/scripts/workflow-prepare-cleanup`: retain the exact plan-cache-only
   retirement boundary. Admit CI archives only through the existing bounded,
   canonical, digest/reference-bound runtime validation used by Janitor;
   never treat prefix/name alone as ownership. Check the ledger references
   before any archive/source-retirement effect. Reuse valid completed or
   prepared-and-already-sealed state by fresh chain/final-event/request/report
   validation without calling append or seal. Missing/conflicting proof blocks.
4. `workflowkit/delivery.py`: add sealed-recovery guidance for Cleanup with
   missing/empty preparation opt-in; retain the normal append instruction for
   genuinely unsealed completion and all non-Cleanup nodes. A recovered
   original successful report requires original request/report/marker,
   matching validated chain and operation digests, unchanged original
   identities, actual retained provenance and project validation. No marker
   fabrication or new validation executor. Unknown/invalid history blocks.
5. `tests/test_workflow_runtime.py`: producer/admission, strict negative,
   bytecode cleanliness, legacy and partial-retention regressions; replace
   opaque-delete success expectations with preservation/blocker expectations.
6. `tests/test_kit_development_workflow.py`: real producer/helper/Janitor
   fixtures, CI integration and retry no-append/no-seal effect assertions;
   replace direct opaque-plan admission success with explicit blocker.
7. `tests/test_workflowkit.py`: missing/empty/nonempty preparation prompt
   matrix, fresh/recovery ownership and unchanged graph/carrier assertions.
8. `.kent/commands/cleanup-task.md`: project helper CI/retry ownership,
   concrete blocker/retention requirements and source-only opt-in example.
9. `contracts/workflow-contract.md`: clarify Cleanup-only recovery exception
   for absent opt-in; ordinary unsealed ownership remains unchanged.
10. `contracts/plan-contract.md`: accepted cache terminal ownership/storage
    lifecycle; Plan acceptance still produces the same typed cache.
11. `README.md`: project-owned preparation opt-in guidance and ad hoc artifact
    storage/retention guidance outside restricted terminal runtime.

No other production paths authorized. No new helper/template/executor/ledger,
wildcard, resource sweep, schema, node, edge, parameter or transition change.
The Kit's helper-opted-in generated workflow prompts must remain unchanged;
generic missing/empty-opt-in future Cleanup prompts change only their recovery
instruction. Checked-in graph specs must remain byte-identical. If generation
requires a different checked-in spec or contract change, stop for revalidation.
Source publication is not consumer rollout; no profile is auto-opted-in.

Rollout: source-only review/verification and normal configured delivery,
subject to workflow gates; no install, live graph apply or qualification.
Old locked Cleanup sessions retain old instructions and require separately
approved recovery/adoption, not repeated approval pretending a prompt refresh.
Rollback: a separately reviewed source revert of this bounded delta before
activation; never unseal, reconstruct history or restore retired state.
Restart impact: none for source development; future adoption/restart is a
separate effect gate. No native Run/Step identity changes.

## Intended admission and recovery

Normal runtime owns only ledger, supported checkpoints, validated referenced CI
archives and the active accepted plan cache. Before terminal handoff, the
project preparation owner externally retains/readbacks the accepted plan
bytes and authority references, then retires only that cache using the existing
helper boundary. The terminal consumer admits only ledger/checkpoints/validated
referenced CI archives. A plan cache still present at seal is a concrete
preparation blocker, not permission for Janitor to discard unique evidence.

No generic helper is installed into consumers. Projects may explicitly select
their existing project-owned preparation seam and reference Kit's authoritative
helper as an implementation example. Without opt-in, they remain responsible
for externally retained, byte-verified producer artifacts before sealing.
Ad hoc performance reports/scripts and credential/device artifacts belong in
project-owned ignored private storage outside `.kent/runtime/<task>/` with an
explicit owner and retention lifetime; tokens never enter fixtures or reports.
Unknown resources are preserved. The blocker names logical entries and requires
owner-led classification, external retention/readback and separately authorized
relocation before retry, not blanket deletion or another approval alone.

Successful seal recovery is read/validate/reuse, not ordinary append or reseal.
For the project helper, its frozen request, retained cache, receipt, actual
final-event projection and chain/report bind success. For absent opt-in, the
owning Cleanup must recover the retained original successful request/report/
marker through its existing project validation and retained native sources;
the ledger's existence or a supplied marker alone is insufficient.
The absent-opt-in read-only sequence uses the existing evidence command's
`validate`/`read`, and the configured runtime sibling's
`validate_terminal_chain`, `validate_cleanup_report` and
`validate_terminal_seal_request`. Require terminal chain task identity,
identical original report marker, exact frozen original seal request versus
marker operation-report digests/redaction/retention, and retained final-event
native source readback. Original request/report proof is recovered from retained
Task/Session sources, not reconstructed from the marker. These validators are
existing project primitives, not a new command or executor; Cleanup owns their
bounded invocation and native-source provenance. A missing helper alone does
not block an otherwise proven original successful recovery.
Missing source, conflicting bytes/identity, fabricated history or unproven
success remains blocked. Preserve the original root, branch, mode and report
before leave. Janitor still freshly validates the terminal chain.
Caller readback/native identity authenticity remains caller-owned; do not
claim that nonempty strings or a cooperative local receipt authenticate Kent.

## Writer-owned implementation checklist

- [x] **First, before any production edit:** capture disposable production-shaped
  red evidence. Run the actual Plan Contract producer and ledger seal followed
  by Janitor. Document that current baseline admits the cache/opaque extras
  rather than falsely claiming the old restrictive failure persists. Add red
  assertions for strict unknown/unique plan-cache preservation, missing-opt-in
  sealed retry instructions, zero seal/append on valid helper retry and
  Janitor import cleanliness. Also capture the actual restricted-consumer
  historical mismatch: extract Janitor and its runtime sibling from Git commit
  `b185756afad9683becda84799ecf209124a47450` into a disposable fixture, then
  run the real current Plan Contract producer and ledger seal into its task
  runtime before historical admission. Assert `unexpected tombstone entries`
  includes the exact produced plan cache, with optional synthetic examples of
  all eight incident entries. No historical production checkout or live logs.
  Keep this bounded reproduction evidence separate from portable current-source
  regression tests. If that Git object is unavailable at execution, first
  recover exact source from existing repository history; an inaccessible
  historical source is an agent evidence gap, not a user approval request.
  If a baseline assertion already passes, record that fact and identify the
  remaining reproducible defect; never force a red by breaking production.
- [x] Tighten admission and eliminate bytecode side effects in the authoritative
  Janitor source; materialize only its generated copy using the scoped existing
  synchronizer operation specified above.
  Keep accepted checkpoints/CI archives and partial deletion semantics intact.
- [x] Update the existing project helper CI admission and sealed-state recovery.
  Test prepared/completed, active/tombstoned and interruption boundaries with
  original bytes/identities and spies proving no append/seal for valid seals.
- [x] Update Cleanup fallback instructions and normative lifecycle/storage docs.
  Assert opted-in Kit graph parity and no status/carrier/API expansion.
- [x] Run affected suites and focused production-shaped positive/negative
  fixtures. Record exact red-to-green evidence, unexpected failures and remaining
  limitations; audit this preview's acceptance mapping and diff/file boundaries.
  Hand off through the existing Implement route to configured verification.

Workflow-owned work (not writer checkboxes): first independent preview review;
separate Plan Review same-hash second review; explicit human approval; graph
Plan Contract acceptance; sole fresh full configured verifier run through
`./scripts/validate`; independent Standards/Gate; normal delivery/Cleanup.
No commit/push/merge permission is created by this plan.

## Evidence owners and acceptance mapping

- Writer owns pre-edit red and focused green evidence in ignored
  `build/kent-workflow/KEN-17/`, with compact retained Task pointers outside a
  retiring root; production edits cannot precede that capture.
- Writer owns disposable synthetic Plan Contract -> ledger -> preparation ->
  Janitor fixtures. Use `TASK-1`, synthetic native IDs, no real tokens, logs,
  external machine paths or OSM-90 raw artifacts.
- Admission matrix covers active/tombstoned/legacy, known checkpoints,
  referenced valid CI, unreferenced/missing/wrong-digest CI, unknown regular
  files/directories, unique accepted-cache bytes, symlink/hardlink, wrong
  mode/owner, tracked/nonignored paths, rename/content/metadata drift, byte/
  entry bounds and partial-retention interruptions. Unknowns remain untouched.
- Recovery matrix covers matching prepared/completed request and seal, active/
  tombstoned runtime, archive/source interruption, conflicting request/report/
  marker/digest/projection, missing retained proof, fake identity and changed
  bytes. Compare ledger bytes, event count and original Run/Session/Step IDs
  before/after; spy on append and seal invocation.
- Inject a failure after the ledger seal is durable but before the helper writes
  its completed receipt. Retry the original prepared request, freshly verify its
  original final-event/chain/request and complete its receipt without append or
  seal. Completing that frozen request is not a claim that a nonexistent
  completed report was read back. Completed tombstone replay must freshly check
  retained ledger integrity; missing/corrupt chains block. Keep Janitor's own
  independent fresh validation as well.
- Add a separate absent-opt-in production-shaped recovery fixture: original
  sealed request/report/marker and synthetic retained provenance -> existing
  read-only validators -> ordinary Janitor admission. Assert zero append/seal,
  unchanged ledger bytes/native identities and missing/conflicting original
  proof blockers. Prompt matrix alone is insufficient behavior evidence.
- Managed legacy fixtures run without a v2 marker/sibling: unknown or unsafe
  inner task entries block before native deletion and remain intact; admissible
  supported legacy entries still proceed. Exercise legacy unlink separately.
- Privacy checks verify private archives/readback, no opaque payload reads,
  sanitized blocker names and no raw credentials/session dumps in fixtures,
  emitted reports or retained evidence.
- Cleanliness fixture loads real Janitor/runtime from a disposable nonignored
  sibling directory under normal Python settings and compares Git status before
  admission and native-deletion handoff. No `__pycache__` side effect; dirty
  state still blocks. Do not use `python -B` to conceal the production defect.
- Writer runs affected unittest suites using existing Python 3.11+.
  Configured verifier owns the one fresh complete source verification; Plan
  does not duplicate it. No interpreter/dependency installation or global PATH.
- Plan owns scope/preview, critique dispositions and first-review pointer in
  Task evidence and its ordinary Plan ledger event. Plan Review owns the second
  same-hash receipt; Implement preserves actual future human approval. Missing
  agent-produced captures are the responsible agent's work, not a request for
  user approval. This plan records no approval in advance.

## Risks and stop conditions

Current opaque-delete permissiveness is materially different from incident
baseline; tests must make the restored strict boundary explicit. Previously
sealed caches/unknown files may now block rather than disappear, requiring
safe external owner action. No live compatibility is asserted.
Prepared receipt interruptions must not produce duplicate terminal events;
sealed recovery must not invoke even an idempotent seal command.
CI admission must share authoritative validators without widening retirement
to arbitrary payloads. No parent/child ownership inference or free-form
workflow routing. Missing native provenance remains a blocker.
Out-of-set source edits, API/schema/graph expansion or genuinely unavailable
validation capability return to Plan before production changes.
