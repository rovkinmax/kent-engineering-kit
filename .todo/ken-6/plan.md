# KEN-6 — Recover mobile lease ownership

## Authority, classification and scope boundary

Work kind: `bugfix`. Immutable fixed point: `2930f492af941eaeecc63e7824e9eb621dff3c80`.
Root source: KEN-6, native Task `task-b6672872-8aaf-4dc9-ba9c-dfab7393a5b4`,
original task body read through `kent task show KEN-6` in Plan Session
`b963504e-2b3c-4d1f-9942-4339d084bd55`.

Included: shared adapter ownership admission/recovery; shared Smoke checkpoint
guidance; deterministic adapter/procedure-contract fixtures. Project-specific
procedure source is delivered separately in OSM-111 and PUB-83. No changes to
device selection, authentication or app policy.

Project delivery boundary is authorized by the native Question in that Plan
Session answered at `2026-10-01T06:23:43.002Z`, available through
`kent questions list --session b963504e-2b3c-4d1f-9942-4339d084bd55 --json`.
Exact selected answer: "Включить версионные патчи процедур и их тесты в Kit;
применение в проектах — отдельный rollout".
This packaging choice was superseded by the later native Question in parent
Session `76fb9180-ca43-4edd-9aef-26f3cc5e795d`, answered at
`2026-10-01T06:24:21.539Z`: "Отдельные source-задачи/PR Appsome и Puber,
связанные с KEN-6". The full proposal/answer was verified via
`kent questions list --session 76fb9180-ca43-4edd-9aef-26f3cc5e795d
--max-handoffs 1 --json`. Only packaging is superseded, not safety or acceptance.
No Kit-hosted consumer patches or snapshots will be produced.

OSM-111 and PUB-83 were created in backlog, not started. They own project
procedure source edits and executable project examples after KEN-6's contract
is delivered. Native cross-project dependency registration was attempted with
short ID (not found in consumer project) and exact native Task ID
(`project_mismatch`); Kent cannot enforce this cross-project dependency.
Their task bodies and this plan therefore record the prerequisite explicitly.
Do not treat their mere existence as implementation or rollout proof.

Incident sources, each a separate fixture:

- OSM-78, archived Task `task-bda4e999-7417-4aac-a362-d4185cd47c2d`;
  Sessions `db4a4a49-e7ac-4a55-9582-f1e01438ebd6` events 206/350 and
  `a4d3279d-6563-42b1-85ba-a2149b547c9a` events 384/397:
  unknown ownership/no checkpoint, subsequent TTL acquisition and release.
  The original token-loss cause is not established.
- PUB-70, `task-9a413c72-b9db-4e47-80b5-4fd6fe1d7d02`;
  Session `8eb4cef4-198c-4cf1-abef-6732d12789c9` events 55–76/127/364:
  returned acquisition token, unknown ownership during resume, external release,
  then identity-correct acquisition/release. Human comment
  `comment-e969b76e-18c9-4f16-aa84-21f11b70253f`, operator timestamp
  `2026-08-24T14:00:43Z`, was recovered with `kent task comment list`:
  release applied only to malformed ownership; reacquire with actual PUB-70
  identity/current Session, preserve package/auth state. Its device permission
  is historical and grants KEN-6 no live action.
- OSM-79, archived Task `task-951ab8f5-c04c-4ee2-bed8-aced207cd5c8`;
  Session `6208da2f-0c4d-48c6-b8ac-750d39adb09a` event 63 and
  `d4cf9553-a16e-421f-a712-d1f23f2a1b10` event 257:
  bare `acquire` token parsed as `acquire-any` records, then unknown ownership.

Original Session event bodies were not re-read in this Plan; the human-authored
KEN-6 body supplies their incident interpretation. Fixtures must not claim
to reproduce unestablished historical causes. Startup agent comment about
OSM-109 is corroboration only; no added incident repair or live cleanup.

Related/deferred: KEN-14 owns mobile action-outcome proof; KEN-15 owns authorized
login recovery and conflicting login instructions; PUB-72 owns package
preservation. No dependency on their implementation is needed to fix ownership.
No SDK/schema upgrade, app implementation, login, credential storage, actual
lease acquisition/release, installed adoption, consumer rollout, live Workflow
mutation, restart, commit or push is authorized by this plan.

## Discovery and dependency inventory

`templates/project/emulator-resource-lock.sh` currently creates runtime/guard
directories at startup, acquires with `task_id=unknown` by default, and preserves
two different stdout contracts. `resume-owned` already checks a nonempty Task
and existing token under the resource guard. Token `resume` already works and
can recreate a missing resource; do not describe this as a missing feature.
Existing release compares tokens; `status` redacts them.

Current tests deliberately acquire without identity, use `task-1` synthetic
labels, and test concurrent guards, expiry replacement and token release.
Examples in both project procedures do not initialize ownership. Puber blocks
partial checkpoints outright; that conflicts with evidence-based recovery of a
known resource with lost token. Appsome lacks the complete lease recovery branch.
Current Plan environment exposes actual `KENT_SESSION_ID`, but no
`KENT_TASK_ID` or `KENT_TASK_SHORT_ID`. Never assume Kent exports those values.

Native Task JSON exposes `summary.id` and `summary.short_id`.
`workflow-branch-identity` already consumes `_kent.task_id` and obtains the
short ID from native Task readback. Use the same native mapping; do not invent
engine environment values or route Task identity by unrelated Jira labels.
Shared checkpoint schema has Task short-ID binding and extensible stage data;
no schema upgrade is planned. Smoke generator guidance lives in
`workflowkit/delivery.py`; no generated live workflow needs updating here.

Consumer inputs observed on 2026-10-01:

- AppsomeAndroid HEAD `2b52c2c69d8e553f02ed810f3441d6d6c2c5e14d`,
  `.kent/commands/smoke-test.md` SHA-256
  `5af0b5053f28ebedefd629dd74f4e1ff932aa759227214b0aa83a1b07ae18bcd`.
- Puber HEAD `18d3e34dae2df2e2ab78f4208ec6cc0a15711ab9`,
  same relative file SHA-256
  `1bdee0c7b3822a7bde60db56664fa5d89895117e74d2b082e457004bf988b6ea`.

Only these procedures were read; no credentials or test-account files.
These hashes document discovery only, not source authority for later consumer
edits. OSM-111/PUB-83 independently re-read their source and scope. Kit fixtures
model the relevant adapter contracts without copying project procedures.

## Design and safety contract

1. Resolve actual Task native ID/short ID using native Task JSON and the actual
   Session via native Session context/`kent session-id`. Generated workflow
   instructions use the real rendered `{{.TaskShortId}}` to request this lookup.
   Adapter-specific ownership input is not a claim that Kent exports a variable.
   The procedure establishes `KENT_TASK_ID` explicitly as the verified short ID
   for adapter calls, with actual Session metadata. Checkpoint stores the native
   ID, short ID, resource and token together; handoff references that checkpoint.
   Stage data names are `task_native_id`, `task_short_id`, `lease_owner_id`,
   `lock_resource`, and `lock_token`; existing top-level `task_short_id` remains
   authoritative. Old checkpoints without native fields can be enriched through
   native readback matching the existing short ID. Conflicting fields block.
2. Reject empty, `unknown`, malformed/multiline ownership before acquisition
   creates directories, guard files, tokens, changes existing locks, or attempts
   TTL reclamation. Apply the same admission to `acquire-any` and internal
   `__locked try-acquire`. Do not trust a public precheck as the guarded check.
   Preserve valid existing metadata and the token-matched release path.
   Accepted Task input is uppercase short ID `[A-Z][A-Z0-9]*-[0-9]+` or native
   `task-<UUID>`; current Session is UUID (hexadecimal 8-4-4-4-12, case-insensitive).
   Whitespace, multiline, empty and sentinel identities are invalid. Syntax is
   not authentication; procedure readback establishes the native mapping.
   Existing metadata requires exactly one nonempty token, one valid Task field,
   and one resource equal to the requested resource. Duplicate required fields
   fail closed. Historical Session metadata is not ownership: an old unknown
   Session alone does not disqualify a valid proven Task/token/resource lease.
3. Canonical new ownership is the verified Task short ID. Native-ID and short-ID
   aliases may be treated as the same Task only when native readback proves the
   pair; never infer aliases from spelling. To recover a legacy native-ID lease,
   pass the verified stored native ID as the adapter ownership input for recovery,
   retain that identity in the checkpoint, and use it consistently until release.
   Do not silently rewrite a foreign or malformed owner to the current short ID.
   Actual identity lookup belongs to the procedure; the portable lock adapter
   need not depend on a live Kent server or add a new resolver executable.
4. Existing checkpoint resource/token: compare checkpoint Task binding and
   guarded ownership before resume. Wrong token or foreign Task blocks; no
   discovery fallback. Known resource/missing token or lost output: inspect
   redacted status, prove native mapping, then `resume-owned` under the guard.
   No resource persisted: bounded status inventory of project-eligible resources
   can recover exactly one proven same-Task lease; zero goes through normal
   fresh acquisition, multiple candidates block. Sole occupancy is not ownership.
   Missing/malformed owner, empty token or mismatched resource blocks recovery.
5. Preserve existing expiry semantics: acquisition may replace a lease only
   under the guard after its explicit TTL; `resume-owned` remains an exact-owner,
   existing-token operation, not an expiry override or missing-lock acquisition.
   Token `resume` retains its existing absent-lock behavior for an identity-bound
   checkpoint, with fail-closed conflict checks. No shorter TTL, automatic
   unknown-owner cleanup or PID-based ownership recovery.
   Precisely, TTL belongs to the acquiring caller and is not stored in a lease.
   Replacement requires `age > caller_ttl`; equality stays busy. Exact-owner
   resume of a lease not yet replaced is allowed after that age and refreshes
   `created_at`. Resume-first refreshes the lease; replacement-first makes the
   old token/foreign-owner recovery fail. No persisted TTL or new expiry-based
   rejection is introduced.
6. Preserve stdout API: `acquire` emits only a bare token; `acquire-any` emits
   exactly resource/token records. Validate parsed resource/token before any
   device operation and immediately persist the checkpoint. Explicit release
   and trap cleanup must read back the exact resource as `unlocked`; failed or
   locked readback is unresolved cleanup, never reported as released. Do not
   expose tokens in reports, comments, fixture logs or status.
7. Device form factor, physical-device consent, APK preservation, package names,
   auth and login rules remain only in project-owned source procedures.

## Closed production-edit preview

- `templates/project/emulator-resource-lock.sh`: admission and guarded
  identity/resource checks; recovery preserves valid ownership.
- `tests/test_emulator_resource_lock.sh`: valid identities for old cases and
  isolated incident/safety regression fixtures.
- `tests/test_mobile_lease_procedures.py` (new): native JSON/session stubs,
  checkpoint interruption/recovery and synthetic shared-contract fixtures.
- `tests/fixtures/mobile-lease/` (new): synthetic native identity, checkpoint
  and output-contract cases only; no project procedure copies or patches.
- `contracts/mobile-smoke-contract.md`: canonical identity/recovery/readback.
- `adapters/README.md`: adapter ownership requirements and stdout contract.
- `workflowkit/delivery.py`: generated Smoke checkpoint/recovery instruction.
- `tests/test_delivery_continuity.py`: exact generated guidance expectations.
- `scripts/validate`: register new deterministic procedure fixture tests.

No `.kent` command copy, generated graph, global config, consumer primary,
role or workflow revision file is edited. Graph delta: none. New APIs:
no lock CLI verb or stdout change; stricter identity admission is intentional.
Existing unknown leases are preserved and block until normal expiry or
separately authorized owner intervention; valid tokens are not invalidated.

Rollout: source Kit changes only. OSM-111/PUB-83 own separately reviewed
procedure source changes; consumer adapter synchronization and live qualification
remain a separate approved operation after source delivery, with exact
byte/revision preconditions. Rollback: revert the delivered source slice
through normal reviewed source work, not manipulating live leases. Restart:
none for source work; retained Sessions keep locked prompts. Source delivery
does not refresh existing live Session or Workflow instructions.

## Ordered writer plan and evidence ownership

- [x] 1. **First writer-owned step, before any production edit:** add failing
  isolated regression fixtures and capture their pre-edit red output with
  baseline HEAD and adapter hash under ignored `build/kent-workflow/KEN-6/`.
  Separately reproduce OSM-78 unknown/no checkpoint, PUB-70 token returned but
  unknown owner, and OSM-79 wrong parser. Additional synthetic cases prove
  discarded stdout and interruption before checkpoint persistence. Fake adb,
  native Kent Task/Session readback, clock and lock-root inputs; no device work,
  real shared runtime roots or credential reads. Agent-owned missing red
  evidence is repaired by writer, never converted into a user approval.
  Required red cases target actual Kit production: missing/unknown identity
  currently creates a lease; malformed ownership/resource can pass recovery;
  token resume can overwrite foreign Task ownership even with a matching token.
  Capture expected-failure assertions for these changed surfaces. Historical
  characterization cases are separate: OSM-79 parser characterization is not
  a red-to-green consumer fix; OSM-111 owns that production proof.
- [x] 2. Implement fail-fast admission and guarded ownership/resource integrity,
  leaving release token validation/TTL serialization intact. Run affected shell
  fixtures and both available guard backends; capture any unavailable backend
  explicitly rather than inventing coverage.
  Add deterministic stale-token rejection after completed replacement, exact
  TTL boundary, and ordered resume-first/replacement-first fixture-clock cases.
- [x] 3. Add retained checkpoint/native mapping/procedure-contract fixtures.
  Do not modify consumers or produce consumer patches in Kit.
  Test both output parsers, partial checkpoints, cross-Session recovery,
  no-resource recovery, legacy native identity and ambiguous/foreign candidates.
- [x] 4. Update shared contract/README/generated Smoke guidance and focused
  generation assertions. Register the new fixture test in the validator.
  Run affected shell/Python tests and procedure-contract fixture readbacks;
  store green results referencing the red baseline evidence.
- [x] 5. Writer audit: every requirement maps to a named test or source artifact;
  list unavailable evidence honestly and preserve sanitized evidence references
  in the writer checkpoint/handoff. No full verifier duplication.

Evidence owners: Implement writer owns red/green logs, incident fixture names,
synthetic checkpoint/native lookup fixtures and readbacks. OSM-111/PUB-83 writers
own project source edits/examples under their independent approvals. Plan owns this preview,
critique dispositions and first review receipt. Separate Plan Review owns the
second independent receipt. Human approval follows both PASS receipts on the
same hash. Configured verifier owns the one fresh complete `./scripts/validate`
run and typed report/log after writer bookkeeping. Standards/Gate/Delivery own
their existing stages; writer does not implement or bypass them.

## Acceptance matrix

- Admission: missing/unknown/multiline Task or Session cannot create a lock or
  reclaim even an expired preexisting lease; guarded internal path also rejects.
- Identity: actual native ID/short ID mapping is verified in stubbed native JSON;
  checkpoint and later Session retain it; unrelated aliases never match.
- Incidents: three separately named deterministic cases preserve their distinct
  evidence; OSM-79 asserts the bare-token parsing mismatch explicitly.
- Interruption: stdout discarded and interruption before checkpoint recover
  the exact existing same-Task token; retained checkpoints survive new Sessions.
- Safety: foreign owner, incorrect token, empty/malformed metadata, resource
  mismatch, absent `resume-owned` target and multiple same-Task candidates block
  without ownership mutation. Ordinary busy and guarded concurrent tests remain.
- Expiry: explicit-TTL acquisition replacement retains existing policy; stale
  tokens cannot release a replacement. No recovery broadens reclamation rights.
- Output/release: both stdout contracts parsed independently; successful
  command with locked/failed status never proves release; verified normal
  token release reads back unlocked.
- Delivery: shared contract/fixture evidence and OSM-111/PUB-83 prerequisites
  are retained; no consumer/device/credential/global mutations. End-to-end
  consumer qualification is not claimed by KEN-6 source verification.

## Planning progress and review

- [x] Read manifest and required sources; select `bugfix`.
- [x] Verify native Task source, human PUB-70 comment and current source behavior.
- [x] Inventory cross-module dependencies and obtain project patch boundary.
- [x] Write scope boundary before implementation planning.
- [x] Obtain bounded grill critique and record dispositions.
- [ ] Freeze preview hash and obtain first independent read-only PASS.
- [ ] Append Plan evidence and hand off to separate Plan Review.

Grill receipt: Session `85d2ad3d-29aa-4729-8eb9-98c15d6691af`, draft hash
`bbe3ea48bff5a6ea0bc351d93ba53a45086eb3a5854c4193a462a5039ec98309`.
All five findings accepted: preserve explicit existing caller-TTL/resume
semantics; enrich old checkpoints through verified mapping and lease_owner_id;
close current identity/historical metadata syntax; remove stale patch packaging;
separate Kit production red proof from historical parser characterization and
consumer proof. Dispositions are incorporated above. These clarify the draft
within original identity/safety requirements, not a new expiry policy or scope.
No second critique or implementation approval is claimed.
