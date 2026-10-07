# Improve Task Supervisor

## Authority and baseline

The original request is the human message in Session
`3ebff256-590b-47f7-8ec5-df1c4ce0f8d3` asking to investigate supervisor Session
`1210e71e-e23e-49f6-9917-e5037bddda95` with Grill and significantly improve
the existing role.

The accepted source preview is assistant event `127` in the requesting
Session. SHA-256 of its exact UTF-8 content without an added newline:
`5f9990e0fda38f49c48b460919ee8adeb53e9ae3b85aa1d08aa0e30ce8fb102d`.
Independent same-hash PASS receipts are the final reports of:

- Architecture Designer Session `6fe5735c-e148-47c7-9d71-7f53bd094d7b`,
  assistant event `60`;
- Researcher Session `2bd8b1d4-35f8-41c7-99e4-e703cf68b8c7`,
  assistant event `74`.

Grill Session `f3658734-87a4-494e-ab2b-800619160103` supplied the earlier
design critique, not either formal PASS receipt.

Human approval is native Question `call_AuVZ9xIN7gzgpc0NvJZrxgrM`, answered
in requesting Session event `152`. The answer approves implementation,
requires updating the existing role without versioned naming, and requests
a PR and merge followed by normal Kit rollout. It changes the proposed plan
path to `.todo/task-supervisor/plan.md`; no new role or versioned artifact is
introduced. Delivery is an expanded effect scope and requires its own
reviewed preview before effects. Manual installation/restart is excluded.

The expanded delivery preview is requesting Session assistant event `208`,
SHA-256 `3f0f7fef12762e0918f942f878cfc993d584a45ea945b59aa37098e6321d374f`.
Its two independent PASS receipts are Architecture Designer event `89`
and Researcher event `107` in the same reviewer Sessions listed above.
Post-review human approval is native Question
`call_G2FJB2DKU0lepERkpzRGmeaI`, selected option 1 in requesting Session
event `242`. It authorizes task-branch publication, checked integration and
head-bound rebase merge only; installed adoption remains ordinary Kit rollout.

The immutable source baseline is
`ad7d7c8ad512c3c2b8d40fd0f0befed852e54035`. Development uses Kent-managed
worktree `8110bf4c-9125-4abe-ac94-d302ff971552`, branch
`improve-task-supervisor`. The moving merge target is not this baseline.

## Findings and accepted design

The original supervisor Session supplies the incident evidence:

- Human events `6`, `42`, `89`, `111` establish the requested outcome,
  routine decision delegation, start authority and overnight operation.
- Assistant `429` duplicates an owning Session's pending decision;
  human `497` identifies the resulting supervision pause.
- Observer commands `595`, `685`, `695`, `756`, `791`, `831` inspect only
  OSM-113. Of 46 sleep-observers through the investigated window, 15 inspect
  one Task or only part of the selected set. This does not mean no complete
  scans occurred.
- Human `1720`, `1721`, `1774` authorize selected PR merges, waive CI waiting
  only for Appsome, and prefer rebase.
- Assistant `1554`, `1760`, `1841`, `2010` asks the human to arrange recovery,
  supply an operator, replace an incapable operator, or authorize an
  alternative route after a confirmed no-op.

The role owns a fair whole-selection observation loop, explicit operational
discretion, deduplicated escalation and outcome-based completion. It may
consult a bounded read-only Grill child outside Workflow, enact already
authorized selected-PR merges, and use a project-qualified native recovery
route for an interrupted selected other Task. These are narrow exceptions,
not product execution, general Git writes or arbitrary Task movement.

Existing governance, actor restrictions, human stops, ownership, targeting,
effect reconciliation and resource safety remain binding. Installation is
an outcome to verify, not an implicitly authorized operation.

## Closed source scope

- `agents/task-supervisor.md`;
- `contracts/role-contract.md`;
- `README.md`;
- `docs/SUPERVISOR-DECISION-CLOSURE.md`;
- supervisor tests in `tests/test_workflowkit.py`;
- `tests/fixtures/supervisor-decision-closure.json`;
- new `tests/test_task_supervisor_contract.py`;
- new `tests/fixtures/supervisor-operational-cases.json`;
- this plan.

Graph delta is zero. Model/tools/config, other role prompts, installed
symlinks/primary checkout, Workflow/Task state and consumer pins are unchanged.

## Verification and rollout

Use focused static contract/scenario checks and one current complete
`./scripts/validate` source run. Static checks bind decisions to the shipped
contract; they do not execute an LLM or qualify native recovery. Document a
separate controlled canary for later normal rollout.

The requested delivery uses the existing repository and a task-branch PR
into `main`, with rebase merge after applicable checks. No force push,
direct-main push, branch deletion, unrelated changes, installation or
restart is included. Publish/merge effects wait for the expanded delivery
preview's two independent reviews and exact approval.

Normal Kit rollout owns installed adoption. Existing Sessions retain locked
instructions; a new supervisor Session is needed after that rollout.
Source rollback is a corrective patch/commit, not reset or history deletion.
Any post-merge rollback is a forward revert through normal delivery.

## Work status

- [x] Investigate original Session and affected surfaces.
- [x] Resolve design with Grill.
- [x] Obtain two same-hash source reviews and human approval.
- [x] Update role, reusable contract and documentation.
- [x] Add consistent static scenario/negative checks.
- [x] Run focused checks and baseline full source validation.
- [x] Review and authorize expanded PR/merge delivery scope.
- [ ] Publish PR, verify integration and merge through repository policy.
- [ ] Audit final source/merge evidence and unchanged installed state.

## Evidence

No runtime recovery, merge or deployment is established by the source
preview or its reviews. Append implementation and delivery evidence here
without replacing the original findings or acceptance records.

- Five new focused contract checks passed on the first source slice.
- The existing supervisor-focused checks initially found one stale README
  phrase after its rewrite; the precise matching-Approval condition was
  restored. This failure did not involve a live Task or installed role.
- Seven focused supervisor checks subsequently passed.
- The baseline complete source validation passed: 1004 tests, one skipped,
  followed by all MCP, Jira, Sentry, resource-lock and mobile-audit checks.
  Later source bookkeeping/contract clarification and moving-target integration
  require fresh final verification; this result is not its substitute.
- At this source snapshot, publication/merge and final installed-state audit
  are still pending. Their later receipts belong to the requesting Session
  and ignored delivery reports, not a fabricated pre-publication success here.

## Delivered original slice

The prior status above is the historical pre-publication source snapshot.
PR46 subsequently merged by rebase at
`06613498984a081eb9cf2c1a121563e6c1dee62b`; its tree matches the verified
original source tree. The final macOS verifier passed 1014 tests/one skip;
PR/main Linux CI passed 1014 tests/two skips. Detailed post-publication receipts
remain in the requesting Session and ignored delivery artifacts. Installed
adoption was not performed or inferred from those checks.

## Five-Session follow-up authority

The same requesting Session's original human event491 accepts assistant490's
useful validation-only no-op boundary and asks to study OSM-105/106/108/109/110,
combine delivered and pending changes, then set a goal. Human497 requires PR
delivery; human569 requires Grill.

The architecture choice is native Question
`call_ZcctM3bkq6ADLSF00SFJbcUD`, answered option1 at requesting event708.
It selects one bounded standalone diagnostic mode in the existing runtime role,
with project compatibility required and no Appsome edits. It is not the source
approval. The Russian Session goal was set after the five-Session study and
combined synthesis.

The frozen combined preview is the exact full-byte artifact
`build/kent-workflow/task-supervisor/combined-preview.md` retained in managed
worktree285, SHA-256
`0df54b5e48d4e042019e72907c2cc9e5e370742eeca4793b1a7a2d239214ba38`.
Combined Grill Session `f3658734-87a4-494e-ab2b-800619160103`, assistant167,
supplies substantive criticism and dispositions, not either formal PASS.
Independent same-hash PASS receipts are:

- Architecture Designer `6fe5735c-e148-47c7-9d71-7f53bd094d7b`, assistant147;
- Researcher `2bd8b1d4-35f8-41c7-99e4-e703cf68b8c7`, assistant168.

Subsequent human approval is native Question
`call_qh05j4cVPj9uDdb1Fo7xlNMn`, answered option1 at requesting event767.
It authorizes the nine-file combined source scope, verification, task-branch
publication, follow-up PR and checked head-bound rebase merge. This does not
authorize installation/restart or production Task/runtime qualification.
The older strict preview430 and its reviews were never approved and are
explicitly superseded, not implementation authority.

The immutable follow-up baseline is merged PR46
`06613498984a081eb9cf2c1a121563e6c1dee62b`, separate from moving main.
Development uses managed worktree `fa55387d-63ed-45bd-8bda-87a9bc061016`,
branch `improve-task-supervisor-followup`; PR46 is not amended.

## Follow-up evidence and dispositions

Exact ordinary Task Supervisor Session identities and relevant events:

- OSM-105 v2: `12c86154-aa16-4b7b-89f3-4b54f86d4096`.
  Events640/757/769 establish unavailable subscription rather than missing login;
  human782 and relay833 establish accepted alternative evidence. Credential
  protection and human stop996/final1005 remain correct.
- OSM-106 v2: `fcb712f0-c52f-4604-b762-3a6dba5d881d`.
  Question749/correction770/775 projected supervisor Git restrictions onto
  a capable owner. Question790/answer791/API868/894/question903/admission912
  show an initially invented billing gate; the user's later accepted condition
  remains binding. Human904/final912 stopped Task messaging correctly.
- MBL-783 aka OSM-108: `31ae9b23-91b1-48b4-9551-feb8a2a2779a`.
  Events34/54/58/59 show a known-failing startup recommendation, already
  addressed by PR46. Report gaps768->1924 include active monitoring followed
  by cancellation; another2297->2811 active gap was about87 minutes.
  Events2811/2820/2829 conflate independent manual/provider prerequisites.
  Executions2865/3147/3407 actually run;3449/3561 explain prerequisite-ready
  edges are not diagnostic. Human3574's diagnostic permission does not alone
  override the old child prohibition.
- MBL-825 aka OSM-109: `a24cd60d-a942-494b-9d07-a82249972e8a`.
  Repeated lease capture failures at2160/2218/2222 and cleanup Questions
  1104/1238/1248/2352/2409 are one cause across lease IDs. Event2192 records
  a guessed run-inspect command creating a real Session. Events2783 and
  3274/3285/3308/3348 distinguish transport/wrapper/schema errors from semantic
  UI proof. Event3254 distinguishes current Task HEAD from pinned source.
  Human4884 waived Chinese verification, not the failed UI CI retained at
  5097/5218. Data resets and closed-file expansions remain real decisions.
- MBL-891 aka OSM-110: `ef6df271-c8d7-4966-8a7b-8f47d1c6e391`.
  Human85/160/240 grants CI/preparation authority, not unlimited runs.
  Events737/747/748/759/775 show a false PR-vs-diagnostic operator choice;
  owner/replan alternatives remain preferred. Events3146/3151/3156/3165/3180
  establish waiting ownership, rejected same-node move and actual alternative
  continuation. Human2812 requires native Question UI. Events1711/1758/1976
  distinguish validation failure from actual measured tests and exact-source
  preflight. Later trigger/controller/branch effects and run caps remain real
  scope limits.

The combined investigation and redacted independent researcher reports are
retained in requesting evidence and the ignored study record in worktree285.
Historical human-only start boundaries, security rules, genuine missing identity
and explicit stops are not retroactively relabelled defects.

Grill dispositions accepted:

- Preserve PR46 and add useful, attempt-qualified no-op classification; one
  Resume invocation, one qualified native alternative after a no-op, no third
  automatic route. Actual effects/provider requests spend the shared budget,
  unknown reserves it; failure/cleanup do not reset it. Existing matching
  qualification is reusable, current settlement is not assumed.
- Waiting reentry is only for an already-resolved technical condition with
  incompatible retained routing. Prefer ordinary answer/approve/owner/replan;
  native pending-object/owner settlement must exclude late continuation and
  preserve every judgment/review/input/source/resource/cleanup obligation.
- Standalone runtime diagnosis is a distinct existing-role mode with exact
  prior runtime authority and qualified project eligibility, genuine identity,
  own permitted persistence/cleanup, no Task checkpoint/lifecycle/Git writes,
  official Smoke PASS, children or extra run/recovery budget.
- Preserve scheduled reports and semantic diagnosis, fixture-check token capture
  before another acquisition, verify CLI/source anchors and separate independent
  prerequisites and owner capabilities. Use native UI for real new decisions.
- Reject generic/default operational children, prompt overrides, new scheduler/
  role/config/graph, fake identity, unsupported token recovery and a claim that
  Kit-only publication qualifies Appsome standalone operation.

The implementation follow-up from the same Grill Session, assistant211, found
three bounded contract ambiguities. They were corrected within approved scope:
explicit child budget/intent handoff and exclusive delegation with action
settlement readback; retained failure forbids repeated Resume but not the proved
no-op native alternative; preflight checks identity/conflicts/procedure while
serialized acquisition establishes ownership before runtime actions. Static
regressions reject the contradictory former wording. No new architecture,
adapter, authority or production effect is introduced by these corrections.
Grill assistant225 verified closure of all three findings in the corrected
source, without claiming formal governance or native/runtime qualification.

## Follow-up closed scope and verification

Nine files: the two existing role prompts, role contract, README, decision-closure
documentation, two supervisor test modules, operational-case fixture and this
append-only plan. Graph delta zero; model/tools/depth/config, generated commands,
Appsome, installed primary and live Tasks remain unchanged. Original Workflow
Smoke requirements remain in their mode-specific section.

Static regressions cover invocation/effect/late-work limits, waiting gate/owner
settlement, compatible/incompatible helper modes, identity/persistence/cleanup,
report deadline restoration, token-parser incidents, CLI grammar, source anchors,
semantic results, independent Stage/provider prerequisites, owner Git capability,
alternative evidence, accepted budgets and real human decisions/stops.
They bind text and modes; they do not run an LLM or qualify native/runtime effects.
The existing validation-only watcher experiment rejects invalid cursor before
observer construction; it does not establish native queue settlement.

Run focused affected checks, then the configured final
`.kent/scripts/workflow-verify-report` with source/environment identity and
content-addressed log, followed by current PR/main CI. Source changes or
moving-target integration require fresh verification when identity is stale.
No production recovery, helper or runtime canary is authorized.
Future separately authorized qualification must exercise pending-object
supersession/late-answer exclusion and standalone identity/persistence/cleanup
with effect-disabled traces or isolated disposable native/runtime fixtures.

Normal Kit rollout owns adoption; existing Sessions keep locked instructions
and new supervisor/diagnostic Sessions require adopted mode text. Appsome's
standalone checkpoint/identity compatibility remains unproved. No manual
install/restart is included. Delivery is the approved PR/rebase path, not
direct-main/force/admin/queue bypass or branch deletion. Rollback is a corrective
source commit or subsequently authorized forward revert, never history reset.

### Follow-up source snapshot status

- [x] Investigate all five Sessions and preserve original authority.
- [x] Combine delivered/pending improvements and obtain substantive Grill.
- [x] Set the requested goal and resolve diagnostic-helper architecture.
- [x] Obtain two same-hash reviews and subsequent source/delivery approval.
- [x] Implement the two modes and coupled safety contracts.
- [x] Resolve the three bounded implementation Grill findings.
- [x] Run 17 focused checks covering 48 static operational scenarios.
- [ ] Complete final configured verification of the committed source.
- [ ] Publish and merge the checked follow-up PR; retain delivery receipts.

Later validation/delivery receipts are append-only requesting evidence and
ignored reports, not a fabricated pre-verification result in this source snapshot.
