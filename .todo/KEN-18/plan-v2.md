# KEN-18 — Complexity-selected coder, isolated planner

## Authority and closed scope

Work kind: `feature`. Root source: KEN-18 task body. Immutable baseline:
`22c7e1821a194d7e867dde81ddfd10187c8486c3`. This is the current authoritative
preview. `.todo/KEN-18/plan.md` remains immutable historical preview v1, raw
SHA-256 `b841dd0be0bc1365337849577e706e67552be5e3b2fb528a8fb9541eceae4cec`.
Its first PASS is historical only; second review did not PASS.

Human authorities:

- Complex model supersession: Session `76fb9180-ca43-4edd-9aef-26f3cc5e795d`,
  user event 11424, step `aedab5d1-5537-40d8-b61a-f63e89fa0806`, 2026-10-01:
  Sol 6.1 replaces Astra for complex coding only.
- Execution: parent Session `98351c2e-c7e4-4151-9e17-0bc65f9f4289`, user seq6,
  step `ee35e58e-ba40-45a1-bca2-d73ef1983c18`; restart completed user seq73,
  step `a32c2570-cc4b-42bd-bdfd-017879058d5e`. Human seq277 in that latter
  step requires preservation of moving remote-target changes on integration.
- Original native Questions in Plan Session
  `631ec574-ebda-486a-b9c3-164978e63ea1`,
  step `ce70c0d5-493f-4350-ab34-a9675e72dee2`: seq36
  (`call_iLwR88uAN8trnlTDwArHU074`) conservative complexity classifier;
  seq41 (`call_NXWHY9mf0MGTFdOl9wQpAhDr`) fail-closed invalid selection and
  retained Session; seq84 (`call_UFev24a8DUTwhVg3cZeLBHIa`) two configuration
  coder roles sharing one prompt and a planner role, superseding the source
  no-new-roles policy only within this scope; seq161
  (`call_lq7W5OgFtRiYyWNyahYvERHg`) selection applies only to Implement,
  Fix remains existing Sol/medium with its own retained Session.
- New native Question in the same Plan Session, step
  `f3bae920-6e6e-4742-b9b4-82e8c2ea9f6b`, tool-completed seq264,
  `call_5sDJlzY48QKmdSWFdb1iyZnt`: option 1, separate Plan and first Implement,
  preserve both Sessions thereafter. Original question explicitly explains
  loss of full Plan conversation for the coder; approved plan/parameters supply
  its necessary context. This supersedes v1's shared compacted first handoff,
  not continuous writer ownership, approvals or any effects boundary.

Agent comments/reviews reference these originals; they are not consent.
The original Questions and accepted options are recoverable through native
`kent questions list` and Session events; do not ask for them again.

Included: source-only opt-in typed classification, native role/thinking
transport, isolated Astra planner, retained continuous Implement, deterministic
qualification, readiness and rollback documentation.
Related evidence: scoped source and installed implementation-worker allocation
observed 2026-10-03 is gpt-6-luna/xhigh, not requested gpt-5.6-luna. Current
default Plan is Sol/low, not Astra/medium. Kent 2.8.0 supports protected serial
selectors and workflowkit already defines typed selector purposes.
Excluded: KEN-8/11/21/13 changes, Kent runtime/SDK patches, installed config,
frozen v1–v5/task-backed graph mutation, consumer edits, live canaries,
activation/restart, publication or merge. Portfolio readiness does not merge
issue scopes. Preserve unrelated dirty product SDK primary.

## Approved product behavior

- Simple means local work with an understood solution and deterministic checks,
  without public-contract, workflow-graph, migration, concurrency or security
  changes. Everything else, including uncertainty, is complex.
- Simple Implement: `gpt-5.6-luna`, xhigh. Complex Implement:
  `gpt-6.1-sol`, medium. Plan and Plan Revalidation: `gpt-6-astra`, medium.
  No substitution of gpt-6-luna for gpt-5.6-luna.
- Plan owns complexity and rationale. Both independent reviews and human
  approval cover the choice. Missing/invalid/stale selection blocks writing
  and returns to reviewed planning; no fallback or skipped mandatory gate.
- Plan and Implement have distinct Sessions. First Implement is new_session;
  subsequent Implement steps and post-revalidation continuation keep its
  original identity/model/effort. Reclassification with a retained writer
  requires re-review/approval plus a separate human replacement decision;
  this task adds no automatic replacement path.
- Fix retains existing fix-worker Sol/medium behavior and its own continuity.
  No complexity override on Fix edges or generic orchestrator allocation.
- Add callable `implementation-simple`, `implementation-complex`, sharing
  existing implementation-worker prompt/tool policy, and `complexity-planner`
  reusing default behavior. Keep implementation-worker and consumers unchanged.
  Allocate planner only to opt-in Plan/Revalidation, not Branch Resolution.
- Support opt-in only with continuous writer policy. Reject fresh_per_slice
  plus opt-in; disabled opt-in behavior is unchanged.

## Bounded production preview

Closed proposed production source set:
`config/subagents.toml`, `docs/MODEL-POLICY.md`,
`workflowkit/profile.py`, `workflowkit/delivery.py`,
`workflowkit/model.py` only if typed validation needs extension,
`templates/project/workflow-plan-contract`,
`templates/project/workflow-branch-identity`,
`contracts/workflow-contract.md`, `contracts/plan-contract.md`,
`.kent/workflow-profile.toml`, `.kent/project-contract.md`,
`.kent/workflows/kit_development.py`,
new `.kent/workflows/kit-engineering-delivery-v6.spec.json`,
relevant `tests/test_workflowkit.py` and existing plan-contract test modules.
Generated regular command copies are updated only through reviewed existing
schema-3 synchronization, then supported bootstrap/parity; bootstrap's
update=False alone rejects modified copies. No deletion/manual-copy bypass.
Different production surfaces require revised bounded preview.

Graph delta, enabled policy only:

- Plan/Revalidation use configured complexity-planner Astra/medium.
- `plan_contract_implement` and, where branch identity is enabled,
  `branch_identity_implement` use new_session/immediate_source on first entry,
  native previous_node protected assignee/thinking values from validated
  accepted selection. No compact handoff from node:plan.
- Start is legal only before any Implement writer was retained. Existing
  writer plus start must block, not create a second writer.
- Implement continue edges retain continue_session/previous_target_or_new,
  but must reject unexpectedly absent retained writer. Native assignment
  preserves the retained role; validate accepted role/effort/settings drift.
- `implement_needs_user_action` uses continue_session/immediate_source, with
  its existing explicit approval, instead of compact_and_continue_session
  which re-establishes configured fallback. On resume the retained Implement
  checks accepted selection and effective settings before any production edit.
  Fix recovery and disabled-opt-in recovery remain unchanged.
- All revalidation edges continue retained node:plan, which now never served
  as Implement. After unchanged selection reapproval, route continue returns
  to retained Implement rather than creating a new writer.
- Add Accept and branch-retry `revalidate` edges to existing retained Plan
  Revalidation, carrying existing plan-change report and route context.
  No new nodes, fan-out selectors, approval removal or Fix topology changes.
- Opt-in v6 is a source candidate only. v1–v5 snapshots and live UUID unchanged.

Dependency/cross-module inventory: native serial selectors/role eligibility;
new-session versus retained-session semantics; model registry/provider
availability, reasoning and budgets; profile typed opt-in; Plan Review digest
carrier; Plan Contract snapshot; branch retry transport; generated parity;
source config allocation. No dependency upgrade, free-form domain routing,
runtime patch or consumer adaptation is authorized. An unavailable exact model
or unobservable session settings is a concrete prerequisite blocker, not
permission to widen scope.

## Typed selection and review binding

One fenced JSON object in the authoritative plan uses closed schema
`coder-selection-v1`, exactly `schema`, `task_short_id`, `complexity`
(simple|complex), and non-empty `rationale`. Duplicate/unknown fields and enum
values fail. A single typed mapping owns role and effort; no agent-selected
model/role string becomes independent authority.
Plan Review emits reviewed normalized-plan SHA-256 through the human
approval-bearing transition unchanged. Accept checks current normalized bytes
before creating the same existing snapshot, including selection and task
identity. Post-review plan/rationale changes must reject. Raw preview review
hash and normalized contract digest are different identities.
This is cooperative digest binding, not native authentication or atomic
task-comment/approval ordering.

Missing/unknown/wrong-task/malformed selection or reviewed-digest mismatch:
Accept emits revalidate to retained Astra Plan. Existing check-node digest
drift routes remain. Branch retry revalidates accepted digest and carries
protected fields; drift emits its previewed revalidate edge. Unsafe/unreadable
snapshot or filesystem failure interrupts/blocks with actionable evidence,
never fabricated selection/approval. Start with an existing writer, continue
without expected writer, or retained configuration drift blocks before writing
and records a concrete recovery decision; no forced completion/relocking.

## Native evidence and complete Session cycle

Pinned native source: respawn-llc/kent v2.8.0, commit
`cacd0adea0a6f727d3e4dc528f2018c7c499d4c1`.
Second review Session `7b0a2c3c-3c0e-4202-a552-b1c3990bdfd0` demonstrated
v1 shared-session conflict from `server/workflowrunner/starter.go`,
`server/workflow/session_policy.go`, and
`server/workflowstore/current_node_completion.go`: serial compaction reopens
the same Session; continued Plan reads current coder assignment, not historical
Astra role. This is a resolved architecture defect, not deferred compatibility
work for the writer. Human seq264 approves its new-session solution.

`server/workflow/target_agent_execution.go` additionally re-materializes
configured effort for retained role; do not claim perpetual native settings
immutability. Qualification validates effective settings before writes.

Required cycle for both simple and complex:

1. Plan P starts Astra/medium.
2. Independent reviews/approval leave P untouched; first Implement starts I
   via new_session/immediate_source. Assert I != P and exact coder settings.
3. Implement checkpoints and revalidation return to retained P via node:plan.
   Assert P remains Astra/medium; I remains retained and is not run concurrently.
4. Same-choice re-review and human approval use continue route to resume I.
   Assert I identity/model/effort unchanged, P still Astra/medium.
5. A changed choice never replaces I automatically; stop for explicit decision.
6. Implement I → needs_user_action → exact blocker approval → I uses
   continue_session/immediate_source. Assert same identity, role/model/effort
   for both choices and recheck accepted digest/settings before writing.

First Fix and repeated Fix preserve existing separate fix-worker routes.
Invalid-workspace recovery remains existing Fix recovery. No parallel writer
is introduced. Source fixtures model actual adapter and native lifecycle
boundaries, not just serialization fields. No live canary is authorized.

### Context and validation matrix

- Accept → first Implement: outputs workspace, plan path, work kind, unchanged
  delivery_context, accepted snapshot path/digest and explicit task identity.
  Native protected role/effort are emitted only on the direct target edge.
  Fresh writer reads accepted snapshot and the authoritative plan containing
  original review/approval evidence pointers. Snapshot/plan identity and digest
  must match before writes; no Plan conversation is needed.
- Accept → Branch → Resolution → Retry → Implement: ordinary carrier contains
  the same snapshot/task/digest reference, not native protected selectors for
  a non-Agent target. Branch success maps validated selection into protected
  selectors only at the Implement boundary. Resolution/retry restores inputs
  from the same task-bound snapshot and rejects drift; it cannot choose roles.
- Accept/Branch → Revalidation: carry workspace/plan/work-kind, unchanged
  delivery_context, intended route/context and existing plan-change report.
  Retained P reads current authoritative plan plus task evidence; original
  accepted snapshot remains evidence of prior choice, not new consent.
- Reapproved continue → retained Implement: carry existing workspace/plan/
  work-kind/delivery context and validated snapshot/task/digest reference.
  Validate expected retained writer; same selection resumes I. Changed choice
  or lost writer is not authorized creation. Verify/fix_continue preserve
  their existing review_context/fix_context carriers without role selection.
- Implement recovery: current I and its retained accepted snapshot reference
  are authoritative; carry existing blocker/work-kind/delivery context.
  Recovery prompt directs re-reading task-bound snapshot and human acceptance
  sources and checking effective identity/settings before any edit. It cannot
  bypass digest/selection validation merely because no Script guard ran.

Guards use existing task-bound snapshot and supported native read-only Session
metadata, not CLI display-name matching or a new authority store. Source
qualification establishes the exact metadata contract before production edits;
CLI task-session listing alone exposes role/identity, not complete model/effort.
Unsupported readback blocks prerequisite qualification. Runtime uncertainty
never authorizes model substitution, database edits or speculative guards.
First-isolated-writer prompt is distinct from fresh_per_slice wording: next
steps continue I, not create another fresh writer.

## Writer-owned ordered steps

- [x] 1. First writer action: capture pre-edit red fixtures and native
  production-shaped baseline Plan→Implement→Revalidation→approval→Implement
  regression fixtures for
  both choices, including identities/model/effort and same-choice continuation.
  Capture failing assertions against current source; the successful new cycle
  belongs to step 6, not a pre-edit green requirement. Record commands/logs.
  Establish supported exact model
  metadata/budgets and read-only settings/retained-session readback contract.
  Unknown prerequisite blocks before production edits. Test fixture edits
  may precede red capture; production edits may not.
- [x] 2. Add typed opt-in policy/closed selection and accepted task/digest
  binding in existing contract, not separate authority registry. Enforce
  continuous-only policy and preserve disabled-opt-in consumers.
- [x] 3. Configure shared-prompt coder roles and Astra planner with explicit
  valid model-specific windows/compaction thresholds from supported metadata.
  Do not assume availability or reuse inappropriate model budgets.
- [x] 4. Implement reviewed digest carrier, first new-session selectors,
  branch/retry carriers, revalidation routes, separate planner allocation,
  and existing-writer/missing-writer/drift guards. Retained writer never
  relocks/replaces silently. Fresh Implement reads approved plan, exact
  acceptance/evidence pointers and route context; no dependence on Plan chat.
- [x] 5. Generate source-only v6 and synchronized copies with parity; update
  normative contracts and current policy, preserving historical entries.
- [x] 6. Capture green affected checks: full Session cycle for both choices,
  classifier uncertainty, wrong-task/missing/duplicate/unknown/stale selection,
  rationale/digest tampering, post-review/pre-Accept drift, branch normal/retry,
  start-with-writer and continue-without-writer rejection, unchanged/changed
  reclassification, disabled-opt-in regression, model/effort/budgets/callability,
  Implement blocker recovery identity/settings, fan-out exclusion, existing
  Fix/verify routes and generated parity.
  Modified reusable prompts get bounded fresh read-only clarity review.
- [x] 7. Document readiness matrix for Kit, Appsome, Puber and product SDK:
  eligibility/opt-in, configuration prerequisites, frozen executions,
  qualification gaps and activation/rollback. Ineligible/unchanged is valid;
  do not edit consumers or dirty SDK primary.

Writer owns red/green reports, native/settings fixture evidence and source
diffs under ignored `build/kent-workflow/KEN-18/`; missing agent-produced
evidence is writer remediation, not another human approval.
Verifier exclusively owns fresh full `./scripts/validate` for unchanged slice,
with identity/log retention. Writer owns focused checks only.

## Governance and lifecycle ownership

Plan owns this authoritative preview, question provenance, critique dispositions,
new raw hash and first independent receipt. Revalidation reruns bounded grill
because architecture changed, then exactly one independent first review.
Separate Plan Review supplies second review of the same hash; only then may
explicit human source approval be requested. Prior PASS/NEEDS_CHANGES remain
historical, not upgraded. Implement owns actual acceptance-source retention.
Mandatory verification, independent Standards, Gate and delivery stay intact.
Optional work may use only justified existing authorized routes.

## Rollout, rollback, restart and moving targets

Source approval authorizes only this source set. Separately approved adoption
creates qualified fresh v6 UUID; frozen Tasks/locked Sessions are not migrated.
Config activation must name roles, exact models/budgets and intended projects;
global activation requires documented restart for new Sessions. No activation,
canary or restart occurs here. Rollback stops new v6 admission and restores
prior graph default/config under separate approval; retained v6 Tasks need
explicit supported recovery, not rewritten graphs or model swaps.
Keep immutable baseline separate from fresh remote merge target. Audit replay
and integrated tree hunks, preserve target-only changes, refresh applicable
checks when integrated bytes change. No force target push/whole-file overwrite.
Parent owns independent exact fresh-base merge audit outside this source flow.

## Acceptance mapping and revalidation progress

Exact coder/planner models, effort, budgets: steps 1/3/6.
Separate Astra revalidation and continuous single writer: steps 1/4/6.
Fail-closed selection/continuation/approved digest: steps 1/2/4/6.
Rollout/rollback/frozen boundaries: step 7 and separate effects above.
Governance: refreshed same-hash reviews followed by human approval.

- [x] Read active manifest, required authorities and incoming second-review P1.
- [x] Confirm demonstrated shared-session defect and original human sources.
- [x] Obtain human seq264 isolation decision, revise topology and cycle evidence.
- [x] Resolve refreshed bounded grill; freeze follows independent review.
- [ ] Obtain first independent new-hash PASS and append Plan evidence.
- [ ] Hand off to separate Plan Review; no implementation approval yet.

## Refreshed grill dispositions

Read-only childless grill Session `e7776444-4660-4385-ae78-8e7f80da6c05`
confirmed isolation resolves v1 P1 but identified Implement blocker recovery
as another compaction/role-reset path. Explicitly revised that recovery edge
to preserve mode, added approved-resume settings/digest checks and both-choice
fixtures. Added context/guard matrix, removed nonexistent workflowkit/prompts.py
from file set (prompt functions live in delivery.py), separated first isolated
writer wording from fresh_per_slice and clarified red-before-edit versus green
after implementation. These are bounded corrections carrying seq264/seq41
isolation/continuity authority; no new product choice or live effect.
