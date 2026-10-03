# KEN-21: Revalidation grill completion

## Routing and authority

Work kind: `test`. Discovery found the requested source and live adoption already delivered. Remaining work is focused deterministic regression coverage and a read-only acceptance audit; no additional workflow behavior is proposed.

Root source: KEN-21 task body, created 2026-09-30T08:03:11Z. Preserve its acceptance criteria and boundaries. This plan does not supersede or cancel any criterion.

Current portfolio authority was independently read from original user events in Session `98351c2e-c7e4-4151-9e17-0bc65f9f4289`: event sequence 6, step `ee35e58e-ba40-45a1-bca2-d73ef1983c18`, authorizes task start/parallel supervision and safe PR merges; sequence 43 of that step permits omission of Appsome CI wait for Kent-only changes. These are not approval of this concrete preview or new live effects. No Appsome changes or CI runs are proposed here.

Original human approval sources:
- Session `76fb9180-ca43-4edd-9aef-26f3cc5e795d`, native answered Question at `2026-09-30T11:01:08.161Z`: question proposing ordinary graph adoption/default with candidate and OSM-105/106 isolated; answer “Да, применяй”. Exact question and answer are retained in `build/kent-workflow/task-supervisor-v2/ordinary-effect-human-approval.json` in the Kit primary workspace.
- Same Session, native answered Question at `2026-09-30T13:55:56.21Z`: question proposing only empty candidate deletion and its nondefault link after human removal of old tasks; answer “Да; 105/106 удалил, удаляй пустой candidate”. Exact receipt: `candidate-retirement-human-approval.json` beside the preceding receipt.
- These original human sources, not the agent-authored September 30/October 1 task comments, authorize historical effects. The earlier September 29 preservation decision concerned the old candidate, not the later ordinary adoption.

Reviewers must verify the exact proposal/acceptance pairs and historical effect receipts. Native Question export uses timestamps rather than question IDs; Session plus exact UTC answer timestamp is the verifiable locator. Do not manufacture an ID.

## Explicit scope boundary

Included root: KEN-21 only. Included behavior: `plan_review_revalidate`, `plan_contract_continue_revalidate`, `plan_contract_verify_revalidate` and their common authoritative `workflowkit/delivery.py::plan_revalidation_prompt`, with `contracts/plan-contract.md` defining material-change triggers and unavailable-eligibility blocking.

Related evidence: Kit primary `build/kent-workflow/task-supervisor-v2/ordinary-appsome-rollout.md`, `ordinary-applied-readback.json`, `ordinary-effect-human-approval.json`, `candidate-retirement-status.md`, `candidate-retirement-human-approval.json`, `candidate-retirement-result.json`, and retained immutable Kit/Appsome runtime roots identified below. Relationships supply evidence, not extra issue scope.

Required dependencies: existing Kit Python unittest suite and supported read-only Kent graph/project APIs; published Appsome builder and existing activation for source/runtime identity readback. No dependency, schema, SDK, enum, domain routing, runtime or tooling upgrade. Appsome adoption is an already-delivered prerequisite, not a new cross-project writer slice.

Deferred/out of scope: SDK rollout, other portfolio tickets, source activation or updates, workflow copies, migration, restart, probe tasks, task lifecycle interventions, pending decision changes, lock rewriting, resource cleanup, Git merge/push/publication and Appsome product implementation.

## Observed baseline on October 3, 2026

- Managed root: `/Users/rovkinmax/.kent/worktrees/kent-engineering-kit/KEN-21`; immutable task baseline `22c7e1821a194d7e867dde81ddfd10187c8486c3`. Do not substitute a moving merge target.
- Appsome default is `28850ebc-392a-4dcc-a40e-e6245eab9c83`, native version 8. Fresh complete `.graph` readback equals retained `ordinary-applied-readback.json` exactly.
- All three named edges exist and carry substantive-change grill before freeze/formal review, explicit unavailable-effective-eligibility blocking, and non-triggers for bookkeeping, waiting and identical repeated failures. Each continues the retained `plan` Session; graph edits do not refresh its locked eligibility.
- Candidate `7fa6b69f-c8bb-4130-8ba0-da11fd4eabe4` is absent from fresh native inspection and Appsome workflow listing. OSM-105/106 are not found in the Appsome project. Historical retirement receipts state that the human removed the tasks, followed by approved empty candidate deletion; do not reconstruct deleted history from absence alone.
- Prior adoption used Kit `7734db4443ba860f7fb17bf6986dc2bd9c4831c2` and Appsome `573c0622fb6a` runtime roots. Installed activation is `/Users/rovkinmax/.kent/backups/appsome-ordinary-20260930/activation.json`; read only. `ordinary-bound-check.py` documents the original builder/activation parity procedure but assumes the old v7 preimage: do not execute it unchanged against v8.
- Existing `tests/test_workflowkit.py::test_planning_grill_and_decision_carrier_prompts` covers two prompt variants but does not bind each named graph edge or explicitly assert retained-ineligible-lock blocking. `tests/test_delivery_continuity.py::test_shared_lifecycle_edges_carry_delivery_context` covers the named paths' lifecycle carrier, not the grill requirement.

## Bounded change preview

Closed production-edit set: empty. Closed test-edit set: `tests/test_workflowkit.py`, `tests/test_delivery_continuity.py`. Plan/evidence artifacts: this file and ignored `build/kent-workflow/KEN-21/` reports. Do not edit normative contracts, generator, generated files, Appsome sources or installed roots.

Exact graph delta: zero nodes, groups, edges, prompts, parameters, settings, IDs or defaults. Already-delivered ordinary graph remains untouched; old candidate will not be recreated.

Rollout: add regression checks to Kit source only, through existing writer/review/verification stages. Re-read Appsome live default and source/runtime identity for acceptance audit without effects. Governance approval covers only these test files and audit, not any live update.

Rollback: if test changes are rejected, revise/revert only task-owned test hunks through normal source delivery; no live restore exists or is needed. Never restore the deleted candidate or stale graph. Restart impact: none.

Safety disposition: no changed producer/consumer contract or reachable live path, so there is no new live compatibility effect. Retained ineligible locks remain incompatible with substantive grill; they must block rather than be declared upgraded. The historical excluded candidate cohort was separately retired with human authority; future default paths use the delivered policy. New native drift or unproven identity stops qualification and returns to planning, not an automatic rollout.

## Acceptance and evidence ownership

- Exact source/live delta: Implement owns a compact audit report binding the three source edge keys, generated prompt variants, live graph/default, full graph digest, and historical approved graph equality.
- Retained execution compatibility: Implement owns negative regression assertions for every revalidation path: unchanged `continue_session`/`node:plan` routing plus mandatory unavailable-leaf blocker and no bypass/review-success claim. Record old cohort's approved retirement separately from future policy. Tests establish instruction/graph contracts, not guaranteed LLM obedience or a live lifecycle run.
- Source/runtime identity: Implement owns read-only normal builder regeneration using existing pinned Kit/Appsome roots and activation. Pinned Kit is `7734db4443ba860f7fb17bf6986dc2bd9c4831c2` at `/Users/rovkinmax/.kent/worktrees/kent-engineering-kit/planning-grill-runtime-7734db4443ba`; pinned Appsome is `573c0622fb6ae857aaac46d440dce3719b551b09` at `/Users/rovkinmax/dev/android/AppsomeAndroid/.kent/worktrees/provision-source-573c0622fb6a`; activation SHA-256 is `4c0f56d49750d793ff88530e21b6657e3d1a715a24ba3b498e060281236703e2`. Verify clean root identity, actual builder/helper/manifest bytes and activation safety before use. Execute `.kent/workflows/builders/appsome_delivery_v28.py::build_workflow(APP, KIT, activation)` in an isolated child with bytecode disabled, validate its spec, and project it through the pinned `workflowkit.graph::plan_workflow_graph` against fresh v8 native inspection. Apply only the historical approved Janitor absolute-path projection to the resulting document. Compare complete `.graph` to fresh native `.graph` and historical approved `.graph`; verify UUID/version/default separately, excluding only the outer `expected_version` envelope from graph parity. Separately verify the current task baseline's three generated prompt contracts; do not substitute current Kit bytes for historical pinned runtime bytes. Record failures without provisioning, materialization or installed-root repair.
- Focused deterministic coverage: Implement owns new tests and a pre-edit red evidence capture; verifier owns the one fresh full `./scripts/validate` run after writer bookkeeping.
- Historical authority/retirement: Implement owns a compact reference audit with exact Session/timestamp/question-and-answer pair, same-hash review receipts, native effect result and candidate absence; do not copy broad Session logs or secrets. Include `ordinary-adoption-preview-draft.md`, `ordinary-effect-compatibility.json`, both original ordinary PASS receipts (`ordinary-grill-pass.log`, `ordinary-research-pass.log`, with additional compatibility context in `ordinary-compatibility-review.log`), apply/default results, and approved readback. Preserve ordinary task dispositions (terminal, proven never-started, unknown-history with impact-scoped compatibility); do not infer never-started from empty current lists. Separately bind candidate retirement preview hash, both reviews, human receipt, zero-impact prerequisite and delete/post-portfolio result.
- Scope preservation: independent review checks no production/Appsome edits, no lifecycle/resource/approval mutation and no invented provenance.

## Ordered implementation checklist

- [x] First writer-owned step, before production or test edits: capture pre-edit red regression evidence using an in-memory/subprocess unittest harness against the unmodified generated spec. The exact same checker must pass the unmodified spec, and every mutant must demonstrably differ and fail its intended assertion, not an import or execution error. Deliberately strip the material-grill or unavailable-eligibility clause from each named edge and demonstrate the proposed invariant checker rejects all six mutants. Also reject a changed retained-session context and an omitted edge. Record expected failures/exit status under ignored evidence. Keep this bounded checker aligned with the final test helper, without adding a framework. This is mutation-sensitivity evidence, not a production-defect reproduction; do not claim baseline production is failing or missing.
- [x] Add parameterized graph-level assertions in the two closed test files using existing profile/spec builders (Kit lite plus existing runtime-v2 CI/no-CI fixtures). Require each exact edge, correct retained context, substantive-change and execution-disproof trigger, grill-before-freeze/review ordering, unchanged non-triggers, explicit unavailable-effective-eligibility blocker, and durable authority handling. Assert real generated graph objects rather than a test-only policy implementation. Retain the mutant checks as deterministic negative tests, with bounded helpers.
- [x] Capture targeted unittest results including all three paths, context mutation, omitted edge and removed eligibility/grill clauses. Explicitly label prompt-policy coverage limits.
- [x] Perform read-only historical authority, current default/graph, candidate absence and pinned normal-builder source/runtime parity audit. Preserve compact digest/reference evidence; verify no effects were performed. Map all five task acceptance criteria to an evidence pointer and limit of proof, distinguishing source instruction-contract checks, independently authorized historical cohort retirement, and unqualified actual model/native enforcement. No live lifecycle run or eligibility of every future Session is claimed. If any baseline claim fails, stop and return to Plan with the concrete discrepancy.
- [x] Update checklist and writer evidence; hand off to existing independent review/verification. Do not execute the workflow-owned full verifier, Git publication or live effects within this writer slice.

No missing agent evidence may become a user implementation request. A test failure or audit gap is writer-owned diagnosis; only a genuinely new product/effect decision returns to the user.

## Planning review checklist

- [x] Manifest-first source discovery and exact historical Question receipts recovered.
- [x] Work kind and explicit scope/dependency boundary selected.
- [x] Fresh native ordinary graph/default and candidate absence inspected.
- [x] Bounded childless grill critique obtained: Session `cc64beef-f040-45a9-a54a-74f0e7d21bb7`, shell 1064, October 3, 2026. All four findings accepted: bound prompt-only proof claims, add positive/mutant controls, make pinned graph parity reproducible with version handled separately, and bind historical compatibility/review/effect chains. Incorporated above. This critique is not either formal independent PASS receipt.
- [x] Preview frozen after critique; SHA-256 and subsequent review status are recorded separately in `.todo/KEN-21/planning-evidence.md` to preserve this file's exact bytes.
- [ ] One independent callable leaf PASS bound to the same hash (receipt/status owned by the separate planning evidence artifact).
- [ ] Separate Plan Review second PASS, then explicit human approval (workflow-owned, not claimed here; future receipts do not mutate this frozen preview).

Delivery context: `{"schema":"workflow-delivery-context-v1","phase":"pre_pr"}`.
