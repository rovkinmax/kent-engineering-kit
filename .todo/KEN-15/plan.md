# KEN-15 — Authorized mobile login recovery

Work kind: `bugfix`.
Immutable baseline: `edaf9cd5ae4f81a05a75d531925f7117eb69bef5`.

## Authority and scope boundary

The unchanged native KEN-15 body (task
`task-ff917dcd-2eb1-4d37-93b6-a4f76f927694`) owns acceptance.
This plan does not supersede or narrow it. The project contract, repository
AGENTS.md and `.kent/commands/plan.md` own source/effect boundaries and governance.

Included root sources: KEN-15 and its OSM-79 incident references, archived
`task-951ab8f5-c04c-4ee2-bed8-aced207cd5c8`; Smoke Session
`6208da2f-0c4d-48c6-b8ac-750d39adb09a` event 595; Session
`083b01fa-7c27-4a1a-8bba-ef83071a8f46` events 284 and 447.
These incident observations are task-body references, not independently
replayed raw incident records or transferable credential authorization.
No historical credentials were retrieved, and none may enter this plan,
fixtures or evidence. The actual submitted normalized value, disappearance
cause and backend rejection remain unproved. The later browser/Advisory Join
issue is not a login failure.

Related evidence: native comment
`76fb9180-ca43-4edd-9aef-26f3cc5e795d` (September 30, 2026) reports delivered
Appsome conflict reconciliation at
`23d3fb739d15313e31629162ebabdd314430e58d`. Read-only inspection of that exact
Git blob confirms delegation to the canonical Smoke procedure and no
unconditional every-run reinstall/login-consent rule. Current Appsome source
HEAD observed as `2b52c2c69d8e553f02ed810f3441d6d6c2c5e14d`; its canonical
procedure still lacks the concrete full-value replacement login protocol.
This is agent evidence of source state, not new human scope authority.

Dependencies: delivered KEN-14 owns MCP outcome proof; consume its existing
`kent-mcp-result-v1` contract without changing bridge flags or provider
interpretation. Delivered KEN-6 owns lease recovery; leave it unchanged.
OSM-84 `ee6a4b6ee4b03902a9d84aa99faa599ce05fdbed` and OSM-86
`e622e7fe77` own Appsome package isolation, not this issue.
PUB-72 `task-660a05bc-27f8-4c72-be0f-1b0f5f86fd38` is related Done work,
not an audited Puber implementation or an included repair.

Deferred/outside this writer: consumer application implementation/rollout,
native core, installers (including uncommitted edits), account provisioning,
credential storage, actual login, MFA approval, device install, lease effects,
graph/configuration/role identity or tool changes, installed adoption and restart.
The explicitly listed role-prompt behavioral delegation is included.
No SDK/schema/dependency upgrade or generated-contract adaptation is needed.
No free-form provider/status routing or new runtime state type is introduced.

## Preview: closed Kit source slice

Production edit boundary:
- `contracts/mobile-smoke-contract.md`: canonical portable login-recovery
  procedure, referencing existing outcome and lease sections.
- `agents/runtime-smoke-tester.md`: short instruction to follow that canonical
  procedure; do not duplicate provider/platform-specific recipes.

Test boundary:
- `tests/test_mobile_login_procedures.py`: replay runner and contract assertions.
- `tests/fixtures/mobile-login/recovery-cases.json`: closed synthetic cases.

Planning artifacts under `.todo/KEN-15/` are bookkeeping, not runtime tooling.
Graph delta: none. Generated commands, adapters, profiles and schemas: unchanged.
Rollout: source-only Kit delivery through existing workflow gates; no consumer
activation, symlink refresh, live workflow mutation or installed adoption.
Rollback: a separately authorized source revert of this closed slice.
Restart impact: none; existing locked Sessions are not refreshed.

## Concrete procedure to specify

1. Reconcile the existing checkpoint and prove the exact eligible target using
   the existing lease/target contract. Inspect current state narrowly. A proven
   authenticated destination skips credential entry entirely.
2. Resolve original applicable authorization for task, account, environment and
   action. A valid existing scope is reusable; consent does not prove current
   authentication. Revoked, explicitly expired, absent or out-of-scope consent
   blocks credential input. An unavailable authorization source is unknown,
   not proof of expiry. Never infer invalid credentials or expiry from UI errors.
3. Before input, prove target and focused phone control.
   Before any secret entry, prove a project-owned secret-safe input/readback
   path, including transcript, logs and temporary capture behavior. Sanitized
   MCP output alone does not prove secret-safe transport. An absent path is a
   capability finding before input, not an automatic demand for human login.
   Derive the expected
   country-code/number normalization from the project-owned input contract.
   Require an explicit full-value replacement/clear, including any existing
   prefix. Never append a full number to a prefilled prefix. When country code
   is a separate control, verify both fields' roles and the combined normalized
   value against the applicable authorized input, without persisting it.
4. Observe replacement and normalization locally with the narrowest safe path.
   Retain only checks such as target/focus/replacement/normalization proven,
   failed or unknown; never retain number/code, hashes of those low-entropy
   secrets, secret-bearing UI trees/screenshots, argv traces or response bodies.
   Missing safe replacement/readback capability stops submission.
5. Submit only after these prerequisites pass. Apply the same target/focus and
   replacement proof to code entry. Code input/submission is not authentication.
   Verify intended authenticated navigation using the known destination and
   error schema plus interaction evidence required by KEN-14, or another
   already-authorized bounded observation with equivalent state proof.
6. Classify separately: proven authenticated; explicit unauthenticated state;
   unknown destination; failed assertion; targeting/input prerequisite failure;
   genuine missing consent; external-only challenge. A false destination
   assertion alone does not establish unauthenticated state. Invalid-code UI
   observation is a symptom, not backend-cause evidence.
7. Permit one phone submission/code request and one OTP submission for the
   current authorized login attempt; these are separate steps, not two retries.
   Use the existing checkpoint/external-action ledger to retain sanitized intent
   before each possible external submission and its proven or unknown outcome.
   Resuming a Session or reopening a form does not reset these budgets.
   After interruption or lost response, observe the current state before
   proceeding; never replay an unknown submission automatically.
   A failed targeting
   observation permits at most one reinspection/replan before any input.
   Never automatically resubmit credentials/OTP after rejection. Bound each
   observation by the existing project timeout/action budget. Exhaustion with
   unknown evidence is a finding, not a fabricated external prerequisite.
   Stop immediately on MFA/security challenge requiring external completion;
   do not bypass it. Ask only for actual missing consent/external action, not
   repetition of safely executable authorized work.

## Required evidence and ownership

The Implement writer owns a pre-edit red capture as its first step: introduce
the new synthetic fixtures/tests before either production file changes, run the
focused suite against the unchanged production contract and retain named
missing-procedure failures under ignored `build/kent-workflow/KEN-15/`.
Capture exact command, exit status, named failures and pre-edit digests of both
production files. Import/setup failures do not satisfy the required red result.
No production edit is permitted until that red result is captured. Evidence
capture failure is writer-owned remediation, never a request for human approval.

The same writer owns green focused output and a coverage matrix mapping each
task criterion to fixture IDs and contract clauses. Replay must use no device,
MCP server, account source, network or lease adapter. Use synthetic opaque input
tokens and model prefix/replacement/normalization observations, not real
telephone numbers/codes. Contract assertions bind the replay's prerequisites,
ordering, attempt bounds and outcomes to the actual normative procedure;
passing a test-only model alone is insufficient proof of agent adherence.
For each critical rule bind fixture ID, expected ordered trace, prohibited
action and exact normative clause. Contract assertions must reject in-memory
mutations removing clear, normalization, no-replay or destination-proof rules;
also verify the role delegates to the canonical procedure. This is source
regression coverage, not qualification of actual agent/runtime behavior.

Required cases: existing prefix cleared before replacement; separate country
code; failed target; unavailable target/focus; failed or unavailable clear and
normalization proof; failed semantic assertion; unknown schema/error state;
successful authenticated navigation; code-entry-only false success; valid
authorization reused without another question; explicitly expired/absent,
revoked and scope-mismatched authorization; unavailable source; external-only
challenge; attempt-budget exhaustion and secret-free retained summaries.
Record representative KEN-14 transport/processing/assertion failures and
nonsemantic success signals without changing its provider contract.
Also cover normal phone-then-code flow, interrupted submission/unknown outcome,
budget persistence across Sessions and sanitized output with unsafe secret
input/readback capture.

The workflow verifier alone owns the fresh complete `./scripts/validate` run.
The independent Standards node owns source review. Neither is a writer step.
Existing unittest discovery picks up the new focused suite; no validator edit.

## Required external project dependency — acceptance remains open

The Appsome project owner must deliver the concrete project-owned adaptation in
its canonical `.kent/commands/smoke-test.md`: actual supported target/focus,
full-value clear/replacement and country-code controls, secret-safe readback,
known authenticated-destination/error semantics, and bounded retry/challenge
handling. `testing-rules.md` already delegates there; do not repeat its delivered
conflict fix. Do not invent MCP capabilities from tool names or process exit.
Project-local replay/source checks must bind these concrete capabilities.

This is a required external dependency, not deferred acceptance or optional
follow-up. Consumer edits remain outside this Kit writer boundary.

The authorized operation is now native OSM-115,
`task-e62fe1d1-1b2a-424e-84ca-e411cf5f17af`, Appsome project
`project-f2d9ab33-8209-41fd-beac-475ff124e1c3`, Workflow
`28850ebc-392a-4dcc-a40e-e6245eab9c83`. Native readback proves running Plan
owner Session `f1f848f2-a242-407f-abae-f352661e855d`, node
`100df27b-3cf5-4dfe-b7d1-3b86994df01f`, managed root
`/Users/rovkinmax/.kent/worktrees/appsomeandroid/OSM-115`, exact source
`532797e49a6b37f0da295d06150f3217297bee16`.

Original human authority independently read from supervisor Session
`1210e71e-e23e-49f6-9917-e5037bddda95/events.jsonl`:
- Question `call_K8zp8nXiC6ZgaYO87punuTV2`, proposal event 2444 and actual
  human freeform answer event 2445, `создай и запускай`: create and initially
  start one related Appsome source Task, PR base `release/4.31.0`.
- Question `call_a6jjwDDIIFb2mSKJXZ9q1Skn`, proposal event 2505 and actual
  human option-1 answer event 2506: use exact published source
  `532797e49a6b37f0da295d06150f3217297bee16`.

These decisions authorize the separate operation, not this preview, device
effects or scope reduction. No cross-project native dependency edge is claimed.
OSM-115's own unchanged body explicitly retains KEN-15 acceptance, project
checks, two preview reviews/approval and no-device boundaries.

Lifecycle feasibility: its project contract Runtime Smoke delegates routing to
`.kent/commands/smoke-policy.md`. That policy expressly allows Gate
`delivery_ready` for positively proven runtime-neutral documentation, Kent
assets and tests without production-app changes. This project's source-only
procedure/test slice fits that supported conditional path; OSM-115 owns the
actual post-verification classification and must retain positive evidence.
Plan does not preselect its transition or claim this route has been exercised.
If later evidence establishes runtime impact or a genuine stage incompatibility,
OSM-115 must report it to its supported owner without fake Smoke PASS, device
work or scope narrowing. Other held Smoke decisions do not prove a login or
route failure here.

Required final receipt from OSM-115: exact immutable delivered source revision,
actual PR base `release/4.31.0`, canonical procedure path, and project-local
checks. Inspect the actual delivered diff and provenance: unrelated
`workflow-task-janitor` edits reported in the child worktree must not silently
enter the login slice or acquire authority from this preview. Preserve foreign
changes; resolve their ownership with the child operation, not by reverting
them or expanding Kit scope. Until receipt verification the consumer criterion remains
OPEN even if all Kit checks pass. Kit Implement must verify this dependency
before handing off as fully implemented; if still pending, report the exact
external delivery prerequisite through its supported blocked transition.
No real login or repeated consent is required.

Agent Question
answer `call_0YuEW2XdY7ZEpl9gl2zdbzef` in Plan Session
`fc40031b-434f-4da4-91ef-8ab2bd61ff53` explicitly supplies no new human
decision or permission; it cannot resolve this external delivery boundary.
The later agent relay is also only a pointer; the original human events above
and native operation readbacks, not either relay, resolve ownership authority.

## Pre-freeze critique dispositions

Grill Session `383a4659-b91d-4959-8d80-72c89a0db674` inspected draft
`828190af173b24d29eb69b50c8c8fd0448144cca57b360f5f29effb7a06806ab`.
All recommendations accepted within the same closed source boundary:
separate submission budgets and unknown-outcome recovery; pre-input secret-safe
channel proof; clause/trace binding with in-memory negative mutations;
identified red evidence; explicit unresolved external operation/owner receipt.
This critique is not an independent PASS receipt or human approval.

First independent review Session `22cf7471-0f2c-4cf0-9905-107aef46c1e8`
returned BLOCK on prior hash
`38c2cef60c7f6048828fcbc31cbcc0f81060f66dff734132029f32599432b273`,
because the external operation/owner was missing. This is not a PASS.
Reconciliation supplies original human authority and native OSM-115 owner above,
and corrects the two editorial findings. This substantive authority/feasibility
change requires refreshed grill and first review, then separate Plan Review.
Refreshed grill in the same Session inspected draft
`8fec7281f2599b167bc46e321ad56caf9bac31f812887bba1fb64112ac4cd149`,
independently verified original human events, native child owner and conditional
source-only Gate policy, and found no blocking gaps. Its bounded delivery-diff
provenance recommendation is accepted above. No formal PASS is claimed.

## Ordered checklist

- [x] Inspect governing manifest, profile, procedure and native task authority.
- [x] Select bugfix and inventory delivered dependencies and source conflict.
- [x] Record explicit scope, closed files, evidence owners and external boundary.
- [x] Obtain bounded grill critique and record dispositions before freeze.
- [ ] Freeze preview, record SHA-256 and obtain first independent read-only review.
- [x] Resolve external source-delivery boundary without narrowing acceptance.
- [ ] Separate Plan Review binds second review to the same preview hash.
- [ ] Human approval follows both PASS receipts; no production edits beforehand.
- [x] Writer captures focused pre-edit red evidence, then edits only closed files.
- [x] Writer captures green focused evidence and criterion coverage matrix.
- [ ] Workflow verification and Standards review run through existing gates.
- [ ] Required Appsome source-delivery evidence is verified before full closure.

No implementation, runtime authentication or full validation was performed by
Plan. Historical incident contents were not independently recovered; the
native task body is sufficient authority for synthetic regression requirements,
not permission to reuse OSM-79 credentials in a new task.
