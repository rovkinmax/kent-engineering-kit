# KEN-18 — Complexity-selected coder

## Authority and scope boundary

Work kind: `feature`. Root source: KEN-18 task body. Immutable baseline:
`22c7e1821a194d7e867dde81ddfd10187c8486c3`.

Complex coder supersession: human Session
`76fb9180-ca43-4edd-9aef-26f3cc5e795d`, user event 11424, step
`aedab5d1-5537-40d8-b61a-f63e89fa0806` (2026-10-01), quoted in task body.
Execution authority: parent Session `98351c2e-c7e4-4151-9e17-0bc65f9f4289`,
user seq6, step `ee35e58e-ba40-45a1-bca2-d73ef1983c18`; restart completion
user seq73, step `a32c2570-cc4b-42bd-bdfd-017879058d5e`.
Task comment `comment-a896a204-f633-4ccd-baf2-e5a52209f96e` is a pointer,
not substitute human authority.

Classification, failure/continuation, and role decisions are original native
Question answers in Plan Session `631ec574-ebda-486a-b9c3-164978e63ea1`,
step `ce70c0d5-493f-4350-ab34-a9675e72dee2`: tool-completed seq36
(`call_iLwR88uAN8trnlTDwArHU074`), seq41 (`call_NXWHY9mf0MGTFdOl9wQpAhDr`),
seq84 (`call_UFev24a8DUTwhVg3cZeLBHIa`). Each accepted option 1; original
questions and selected text are retained in the Session events.
Seq84 explicitly supersedes the source policy prohibiting additional coder
roles, only to permit two configuration roles sharing one implementation
prompt and a planner role. It does not authorize installed effects.
Seq161 (`call_lq7W5OgFtRiYyWNyahYvERHg`) in the same Session/step approves
complexity selection only for Implement; Fix keeps its existing Sol/medium
role and separate retained Session. This narrows coder-selection scope only.
Fresh-target safety authority: parent Session above, human seq277, step
`a32c2570-cc4b-42bd-bdfd-017879058d5e`: preserve concurrent target changes;
parent owns the exact fresh-base merge audit.

Included: source-only complexity classification, accepted-plan selection
binding, native role/thinking transport, continuous writer preservation,
deterministic qualification, rollout/rollback documentation.
Related evidence: source/installed allocation inspected 2026-10-03;
both use implementation-worker `gpt-6-luna`/xhigh, not the task's
`gpt-5.6-luna`. Current default Plan uses Sol/low, not Astra/medium.
Kent CLI is 2.8.0 and exposes previous-node role/thinking selectors;
workflowkit already has typed ParameterSpec purpose and EdgeSpec selectors.

Excluded: KEN-8/11/21/13 changes, Kent SDK/runtime implementation, installed
configuration, frozen v1–v5/task-backed graph mutation, consumer source edits,
live canaries, activation, restart, merge and publication. Shared parents and
portfolio readiness do not merge issue scopes.

## Approved behavior

- Simple: local change with understood solution and deterministic checks,
  without public-contract, graph, migration, concurrency or security changes.
  Any other case or planner uncertainty selects complex.
- Simple coder: `gpt-5.6-luna`, xhigh. Complex: `gpt-6.1-sol`, medium.
  Planner: `gpt-6-astra`, medium. Never substitute gpt-6-luna silently.
- Plan owns a typed simple/complex selection and rationale in the authoritative
  plan. Review and explicit approval cover it. Plan Contract validates against
  accepted plan/task identity before emitting protected role/thinking outputs.
  Intervening reviews/scripts must not independently choose a coder.
- Missing, invalid or stale choices block writer launch and return through
  existing plan revalidation/blocked routes. No fallback and no mandatory-gate
  bypass. Unknown values are errors, not new routing strings.
- Implement/Fix continuations preserve retained writer Session/model/effort.
  Reclassification requires plan re-review/approval and a separate human
  decision before replacing an existing writer Session. Do not silently relock
  or switch an active Session. Verification/review/delivery routes are unchanged.
- Additional configuration roles reuse existing implementation prompt and
  writer tool policy. Planner reuses default behavior; no new role prompt,
  hook, model dispatcher or parallel writer is introduced.
- Keep existing implementation-worker and other consumer allocations unchanged.
  Add `implementation-simple` and `implementation-complex` callable selectors,
  plus `complexity-planner`; only opt-in Plan/Revalidation uses the planner.
  Branch Identity Resolution retains its existing orchestrator.
  Fix retains fix-worker Sol/medium; no complexity override on Fix edges.
- Initial supported policy is continuous writers only. Explicitly reject
  opt-in with fresh_per_slice; disabled opt-in preserves existing behavior.

## Bounded preview

Proposed production file set:
`config/subagents.toml`, `docs/MODEL-POLICY.md`,
`workflowkit/profile.py`, `workflowkit/delivery.py`,
`workflowkit/prompts.py`, `workflowkit/model.py` only if typed validation needs
extension, `templates/project/workflow-plan-contract`,
`templates/project/workflow-branch-identity`,
`contracts/workflow-contract.md`, `contracts/plan-contract.md`,
`.kent/workflow-profile.toml`, `.kent/project-contract.md`,
`.kent/workflows/kit_development.py`, a new
`.kent/workflows/kit-engineering-delivery-v6.spec.json`,
and relevant tests in `tests/test_workflowkit.py` and existing plan-contract
test modules. Generated regular command copies are materialized only by
the existing authorized source bootstrap, never manually edited.
Discovery proving a different production file set requires revised preview.

Graph delta: opt-in typed complexity policy; configured Astra planner;
existing serial first-writer edge consumes native `previous_node`
assignee/thinking purposes from the graph-owned contract output. Existing
branch-identity stage must carry/validate the same accepted selection when it
intervenes. No new node, no fan-out selectors, no approval removal. Existing
continuous writer topology (first compacted handoff from Plan; retained writer
on loops) stays intact unless deterministic native qualification disproves
compatibility, in which case stop and revise preview rather than improvise.
New v6 candidate leaves v1–v5 snapshots and live workflow UUID unchanged.

Dependency inventory: Kent native serial selector capability and role
eligibility; model registry/provider availability and context budgets;
profile typed opt-in; generator/graph serialization; Plan Contract snapshot
and branch identity carriers; source/config allocation; generated parity.
No SDK/schema upgrade is authorized. Absent native capability or unavailable
exact model is a dependency blocker, not permission to patch Kent runtime.
Consumers remain unchanged unless separately scoped and approved.

### Selection binding and failure routes

One fenced JSON object in the plan, schema `coder-selection-v1`, contains
exactly `schema`, `task_short_id`, `complexity` (simple|complex), and non-empty
`rationale`. A single closed mapping owns role and reasoning. Plan Review
produces the reviewed normalized-plan SHA-256; its approval-bearing transition
carries that digest unchanged to Accept. Accept compares current bytes under
existing checkbox normalization before snapshot creation and stores selection
in that same snapshot. A change after review but before Accept must reject.
This is cooperative digest binding, not native approval authentication or
atomic task-comment ordering.

Missing/unknown selection, wrong task, reviewed-digest mismatch or malformed
selection: Accept emits a previewed `revalidate` edge to retained Plan, with
existing plan-change report carriers. Add this edge to v6 (no new node).
Unreadable/unsafe snapshot or filesystem failure interrupts/blocks execution
with actionable evidence; it never fabricates selection or approval.
Branch identity retry validates accepted digest and carries protected fields;
mismatch takes a previewed revalidation edge to retained Plan.
Existing drift-check routes remain unchanged. No mandatory gate is skipped.

### Session matrix and native evidence

Primary native source: respawn-llc/kent v2.8.0, commit
`cacd0adea0a6f727d3e4dc528f2018c7c499d4c1`,
`server/workflow/target_agent_execution.go`, `selector_applicability.go`,
`target_agent_selection.go` and `target_agent_execution_test.go`, inspected
read-only through GitHub API. Native retained execution resolves retained role,
not static node fallback; configured effort can be re-materialized from that
role. Qualification must assert stable effort and reject configuration drift,
not claim Kent itself locks effort forever.

- First Implement: existing compact handoff from Plan; selected simple/complex
  role, xhigh/medium. Compaction may retain Session ID; assert actual settings.
- Implement continuation: previous_target_or_new retains Implement. Validate
  role/effort against snapshot. On an authorized first entry with no writer,
  protected selection initializes it. Unexpected missing writer on subsequent
  continuation blocks rather than silently replacing it.
- First Fix: previous_target_or_new starts existing fix-worker Sol/medium;
  no inheritance of Implement Session. Repeated Fix retains its own Session.
- Invalid-workspace recovery: existing separate fix-worker recovery remains,
  no complexity selection and no concurrent writer.
- Reclassification with retained Implement blocks for the already-approved
  separate human Session-replacement decision; no automatic replacement.

## Writer-owned implementation checklist

- [ ] 1. Before production edits, capture bounded red fixtures for absent
  complexity propagation, invalid/stale rejection, exact model/effort mapping,
  first-writer and retained-writer behavior. Record baseline and commands.
  Also establish native compatibility from read-only primary docs/runtime
  tests and exact model metadata; no live canary. Unknown capability blocks.
- [ ] 2. Add typed opt-in profile policy and selection representation; enforce
  closed enum values and accepted task/plan binding. Default consumer behavior
  remains unchanged. Extend existing Plan Contract, not a parallel registry.
- [ ] 3. Configure simple/complex coder selectors sharing one prompt, planner
  Astra/medium, valid explicit context windows and compaction thresholds
  supported by exact model metadata. Do not copy Luna budgets onto Sol or
  assume gpt-5.6 exists because gpt-6-luna is installed.
- [ ] 4. Extend Plan instructions and contract output/native first-writer
  selectors, preserving all carriers through review/approval/branch identity.
  Validate continued-session identity without changing its model or effort.
- [ ] 5. Generate source-only v6 candidate and regular command copies via
  supported bootstrap. Preserve old snapshots; update normative contracts and
  current source policy (historical policy entries remain historical).
- [ ] 6. Capture targeted green qualification and source diffs. Test simple,
  complex, uncertain, unknown/missing/stale, tampered rationale/plan digest,
  both branch-identity paths, start/continue/verify/fix_continue, retained
  Implement/Fix, reclassification rejection, fan-out exclusion, explicit
  model/effort/budget/role eligibility and generated parity. Every modified
  reusable prompt receives a bounded fresh read-only clarity review.
- [ ] 7. Produce readiness matrix for Kit and affected consumer profiles:
  opt-in state, selector/continuity compatibility, configuration prerequisites,
  frozen executions, qualification gaps, rollout and rollback. No live effects.

Writer owns red/green artifacts under ignored `build/kent-workflow/KEN-18/`,
source diffs, deterministic fixture reports and readiness documentation.
Failure to capture writer-owned evidence remains writer remediation, never
a new user approval request. Test-only fixtures may precede red capture;
production changes may not.

## Workflow-owned evidence and gates

Plan owns this preview, question provenance, grill dispositions, raw SHA-256,
first independent PASS receipt and append-only Plan ledger event. Plan Review
owns second independent receipt on the exact same hash. Human approval is
requested only after both PASS and is retained separately, never fabricated.
Implement owns actual acceptance capture. Configured verifier exclusively owns
one fresh full `./scripts/validate` run and identity/log retention for the
unchanged slice; writer owns affected checks, not a duplicate full run.
Standards and workflow Gate remain mandatory; delivery has its own effects.

## Rollout, rollback and restart

Source approval authorizes this source file set only. Separately approved
adoption creates a fresh v6 Workflow UUID after qualification; it does not
migrate frozen Tasks or locked Sessions. Configuration adoption must explicitly
name roles, model metadata/budgets and intended projects. Existing tasks stay
on their original graphs/models. Exact registry unavailability blocks adoption.
Consumer readiness is documented, not activated.
Rollback stops new v6 task admission and restores prior default graph/config
under separate approval; retained v6 executions require an explicit supported
recovery decision, not graph rewriting. Global configuration adoption requires
documented Kent restart for new Sessions; this source task performs no restart.
Before delivery compare immutable baseline with fresh remote target, inspect
replay/tree hunks, preserve target-only changes and rerun affected checks on
integrated bytes. No force push or blind replacement. Parent owns merge audit.

## Acceptance audit

Exact simple/complex model, effort and budgets: steps 1/3/6.
Single writer, continuity and mandatory gates: steps 4/6 and workflow checks.
Invalid/stale/continuation deterministic coverage: steps 1/2/6.
Optional work: only justified existing authorized route, no new skipping.
Rollout/rollback/frozen state: step 7 and separate effect gates above.
Governance: grill, first same-hash review, separate Plan Review, then approval.

## Planning progress

- [x] Read manifest and required authority; selected feature.
- [x] Inspect source/native CLI and scoped effective configuration.
- [x] Obtain four human product decisions with exact original locators.
- [x] Define explicit root scope and dependency inventory before writer plan.
- [x] Resolve bounded grill critique.
- [ ] Freeze preview and obtain first independent same-hash PASS.
- [ ] Append Plan ledger and hand off to separate Plan Review.

## Grill dispositions

Read-only grill Session `b8a19675-cfe7-451c-866d-4352774040ed` supplied critique,
not formal PASS. (1) Added Session matrix and native source evidence, stable
effort checks and human seq161 Fix scope. (2) Added branch template and retry
checks. (3) Previewed Accept/branch revalidation edges, filesystem interruption.
(4) Defined reviewed digest and closed representation with post-approval drift
fixture. (5) Restricted opt-in to continuous policy and separated planner from
generic orchestrator. No live, SDK, consumer or installed scope expansion.
