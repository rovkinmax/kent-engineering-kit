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
