# Workflow Contract

Platform-neutral generator contract. Projects supply build, test, runtime,
source-control, issue-tracker and release procedures. Nodes receive their
role, edge prompt, context manifest and selected procedures rather than this
entire maintainer contract. [Global governance](../global/AGENTS.md#authority-and-decisions)
owns Kit/workflow change approval.

## Lifecycle

- Kent owns lifecycle, execution target/root and resolved commit. Project
  artifacts own only implementation progress, not mirrored lifecycle state.
- Writers complete code/test/config/documentation and deterministic-check
  steps. Standards, Specification, Gate, Smoke, Compliance, PR, CI and Cleanup
  remain workflow-owned even if a legacy plan lists them as writer checkboxes.
- `needs_user_action` is a resume gate for a real decision/external action,
  not acknowledgment of waiting or a technical problem with a capable recovery
  owner. Confirmation restarts the stage; required resolution evidence must
  exist first. Post-Join recovery reruns fresh read-only branches as declared.
- Gate may route a resolved technical blockage to `retry_verification` for a
  fresh verification cycle; this is not successful completion of the blocked
  cycle or a substitute for `needs_user_action` when a real decision/action is
  required. Eligibility requires evidence of the original blocker, resolution
  by an existing capable authorized owner, changed prerequisites and
  settlement of prior owned processes/effects. Preserve Task/root, accepted
  scope, attempt/spend limits and current delivery context. Keep historical
  BLOCKED reports unchanged and rerun all enabled read-only branches through
  Join; old PASS evidence cannot satisfy the new cycle. If the owner, evidence
  or settlement is unavailable, retain the capability gap instead of polling,
  creating a new owner or inventing a product Fix.
- Runtime interruption is not a business decision. Reconcile effect state and
  use the smallest supported retained/new-session recovery, preserving values,
  execution identity and fan-out/Join. Requeueing alone does not prove startup.
- Transition keys, Script stdout, prompts and parameters form one revisioned
  interface. Apply complete approved graphs atomically.
- `wont_do` requires cancellation authority and a `closure_reason`.
- `done` means the approved endpoint was actually evidenced, or explicitly
  authorized report-only completion. Review-ready PR, merged, released and
  full business acceptance are distinct endpoints.
- Report-only/read-only/frozen runs perform no tracked/staged repair; preserve
  candidate findings for an authorized repair revision/owner.

### Execution-history and compatibility policy

Live updates follow the [authoritative compatibility policy](workflow-update-compatibility.md).
Source publication does not activate graphs or reload retained instructions.

## User

Global instructions own language, original authority and question style.
Transition summaries state the actual decision/action, reason and next result;
reports belong in their declared structured carriers.

Task-scoped correction belongs in `fix_context`, not instructions for the user
to implement it. External dependencies block only the stage requiring them.
Out-of-scope failures retain their boundary and capable recovery owner; no
question asking to absorb unrelated scope.

## Writer session policy

- `continuous` retains/compacts writer context; `fresh_per_slice` creates one
  writer per ready implementation step or independently verifiable Fix group.
- Non-writer approval/recovery loops retain their declared continuity.
- Handoffs preserve worktree, immutable baseline, accepted artifact, original
  decision references, findings, checkpoint and evidence.
- Fix deduplicates reports into a dependency-ordered bundle. Continuous Fix
  resolves compatible groups before verification; fresh Fix carries remaining
  findings through `continue_fix`. No symptom-per-session or bookkeeping-only
  loop.
- Proven earlier-step regressions within the full approved Task/file boundary
  remain repair work. Step budgets do not exclude them. Baseline debt, foreign
  changes and unproven target-only failures are not absorbed.
- Policy changes require compatibility with retained writers.

### Complexity-selected Implement

`coder_selection` defaults to `disabled`; `complexity` requires continuous
writers. [Plan Contract](plan-contract.md) binds the typed task choice and
reviewed digest to protected role/thinking values; agents do not choose models.
Only first Implement entry uses native `previous_node` selectors; later routes
carry task/digest. Start requires no existing writer, continuation exactly one.
Drift returns to retained Plan; replacing a writer needs explicit recovery.
Fix and mandatory gates remain unchanged.

## Portable parameters

`workspace_path` is the canonical repository/managed root, never an artifact
directory. `fixed_point` is the immutable task baseline, not a moving merge
target. `work_kind` is selected once from the project profile. An explicitly
planless flow uses literal `plan_path=not-applicable`; required values are
non-empty.

Canonical keys:

- `workspace_path`, `plan_path`, `work_kind`, `spec_path`, `fixed_point`,
  `changed_files`;
- `verification_report`, `review_report`, `standards_report`,
  `compliance_report`, `review_context`, `fix_context`;
- `verification_status`, `standards_status`, `spec_status`;
- `smoke_report`, `smoke_rationale`, `smoke_scope`, `blocker_reason`;
- `pr_url`, `branch_name`, `merge_strategy`, `pr_report`, `ci_report`,
  `ci_contract`, `delivery_context`, `merge_report`, `publication_report`,
  `pr_base_branch`, `ci_preparation_diagnostic`;
- `closure_reason`, `cleanup_report`, `evidence_context`, `pr_head_oid`,
  `pr_base_oid`, `cleanup_mode`, `cleanup_session_id`.

Edges declare the exact required subset and additional protected protocol
fields. Prompts may not require undeclared outputs.

## Delivery continuity

Enabled profiles preserve `delivery_context` as a compact closed
`workflow-delivery-context-v1` or `workflow-delivery-context-v2` JSON string.
Legacy profiles keep v1 bytes unchanged. Current target-family profiles use
v2: pre-PR contains `schema` and `phase=pre_pr`; post-PR adds observed
`pr_url`, `branch_name`, exact `pr_base_branch`, `merge_strategy`,
`pr_feedback_cursor`, and only actually produced `ci_contract`/`ci_report`.
There is no task ID in this packet. `pr_base_branch` is the exact GitHub
`baseRefName`, not the task branch or an inferred default. Re-observe and
compare it before reporting PR success, merge, publication or cleanup. A
target change requires applicable existing owner authority and fresh PR
observation. When authorized, `post_pr_target_revalidated` changes only the
target in the v2 packet and preserves CI packet bytes and retry state; it adds
no graph edge or authority.

Preserve nested producer packet strings byte-for-byte. Plan initializes
pre-PR; acceptance excludes transport from the plan digest. Plan/branch/writer/
verification/Gate/runtime continuations carry it only where declared.
Gate uses current dispatch-scoped delivery state, not historical verifier
outputs. Source-CI entries unwrap the declared packet into their deterministic
watcher contract; profiles without source CI retain their flat observation
inputs. Merge/publication/Cleanup use their declared result contracts.

Source-CI v3 requires the current `prepare_ci` contract and a freshly observed
native Script Task/Workflow graph. Its `ci_prepare_failed` carrier is exactly
`workspace_path`, `task_short_id`, and `ci_preparation_diagnostic`. The
existing CI monitor adds only `retry_preparation` with
`workspace_path`, `task_short_id`, and `delivery_context`, plus
`preparation_needs_user_action` with
`workspace_path`, `task_short_id`, `ci_preparation_diagnostic`, and
`blocker_reason`; the latter retains its approval and self-recovery semantics.
These are existing-node routes. CI-off profiles receive neither route.
Legacy source-CI profiles retain their declared legacy carrier and routes.
Protocol selectors derive from current profile plus freshly observed native
graph, never caller-supplied graph data.

Producer packets are opaque and reconstructed through qualified helpers,
never retyped from narrative. Agent-owned context may be synthesized in the
project's envelope using bounded report references where allowed. Do not impose
Markdown or indiscriminately duplicate reports.

An explicitly supplied graph-declared `review_context` supersedes historical
same-key values during packing. This narrow derived-context rule grants no
generic values-win precedence: all producers still require complete identity,
version/digest, field and tamper validation.

GitHub provider retry restrictions live only in the existing producer binding
field `github_retry_constraint`; they are not graph fields, business carriers
or report data. Its canonical closed DTO is either
`{"schema":"github-provider-retry-v1","provider_host":"github.com","kind":"not_before","not_before_unix_ms":<integer>}`
or the same schema/host with `kind:"unknown"` and no deadline. Merging retains
the greatest deadline; `unknown` absorbs all constraints. Duplicate handoffs,
PR/head changes and delivery remapping cannot reset or clear retained state.
Before every GitHub API provider child, use the qualified provider-read guard
with exact incoming commentary footer refs and actual native Agent/Script
identity. The guard authenticates full producer ancestry; an active deadline
or unknown timing means no request. Do not mine reports or ledgers for refs,
or persist raw headers, bodies, debug output or credentials. Existing local
retry budgets and delays remain unchanged; this restriction adds no sleep,
retry budget, global limiter or automatic recovery. It does not blanket-block
Git transport operations such as fetch, `ls-remote` or push, whose existing
independent authority checks still apply.

## Context

Five manifests (`plan`, `implement`, `review`, `smoke`, `delivery`) declare
required/conditional/forbidden reads. Edges carry Task values, not catalogs.
Evidence is append-only and sanitized; use genuine inherited identity through
the qualified project command. Unknown/damaged evidence supplies no consent,
effect success or retry budget.

Record actual checks, artifacts, decisions and instruction reads. Telemetry
is optional: preserve truthful available measurements, use null/absence when
unknown, and do not invent model/compaction counters or mandatory agent
bookkeeping to estimate them.

A configured `prepare_cleanup` owns final ordinary event/seal. Other nodes
retain their append responsibility; retries reconcile existing evidence rather
than duplicate events. Without the helper, the project's declared cleanup
procedure owns append/seal and prior-success validation. Freeze root/branch/
reports before leave. No secrets, transcripts or raw authenticated payloads.
See [evidence recovery](evidence-recovery.md), including sealed-state invariants.

## Branch identity

Task short ID remains lifecycle/checkpoint identity regardless of Git branch.
`task` keeps it; `jira` derives `feature/<KEY>` from exact source/body Jira
identity; `github_issue` derives `issue-<number>` only for the current repo.
Ambiguity blocks before mutation; related/dependency keys do not select a branch.
No usable external identity retains the Task branch.

The branch Script follows Plan and preserves its values. Existing desired
branches are ambiguous ownership, never automatic reuse. Report actual
`git branch --show-current`; downstream nodes must not infer it from Task ID.

## Release source/publication

Projects declare selected-source validation, release builder, publication
procedure, credential source and effect approval. Source bindings/specs/
manifests validate selected bytes, not runtime readiness or publication rights.
Schema-3 package publication is approval-gated against exact merged source.
Schema-4 source/graph admission remains distinct from live activation.
Use [source-binding interfaces](release-source-bindings.md) and actual executable
schemas; do not infer an effect from successful source checks.

## Execution targets

Generated workflows declare an explicit policy from the profile/per-kind
override: `ask-on-first-execution`, `none`, `head`, `default-branch` or
`ref:<revision>`. Normally Delivery asks, Canary uses source HEAD, maintenance
uses default branch, and release/hotfix selects an exact revision.
`none` deliberately uses the source workspace, including non-Git/local jobs.
Start/approval/move overrides do not mutate policy.

Dispatch verifies canonical native root before fan-out; invalid paths use the
metadata-correction route. Project verification wrappers own execution details.

## Work-kind routing

Every Delivery profile declares work-kind description and project-relative
Plan/Implement procedures. Supported task-body `work_kind` is authoritative;
otherwise classify conservatively and resolve real ambiguity. Read only the
selected phase. Continuation guards restore accepted work kind where declared;
other edges preserve their explicit carriers.

## Fan-out constraints

Branches are read-only leaves, report structured failures directly to Join and
emit a stable completion contract. Only post-Join Gate selects repair/runtime/
delivery/user action. Early Standards emits `standards_status/report`; final
Compliance owns `compliance_report`. Retained graphs keep their proven interface.

Review against the immutable task baseline; moving target compatibility is
separate. Require merge/replay evidence for target-only loss/conflicts.
Analyzer debt is blocking only when task-introduced/worsened or when an explicit
absolute-clean policy creates a genuine contradiction.

Every enabled report must pass obligations due at the current approved stage.
Deferred evidence retains owner and due milestone. A review-ready PR may retain
external `UNVERIFIED` under exact authority, without full acceptance, merge/
release or Task Done. Changed milestone/authority requires fresh review;
Gate cannot rewrite old BLOCKED. This applies without final Compliance or PR.

## Active feedback and recovery

Steer active owners; comments are durable context, not control. Product decision
changes update the authoritative plan with original human sources before
implementation. [Plan Contract](plan-contract.md) owns revalidation.

Use supported retained/new-session recovery and capable infrastructure owners;
never manually move an active Task to evade cleanup, borrow identity or treat
source prompt edits as loaded instructions.

Fix/Smoke checkpoints contain next action, completed/remaining work and mutation
ledger through the qualified helper. Reconcile Git, Task, resource and effect
state before skipping or repeating work. Unknown effects block replay; existing
budgets and restrictions survive all identity/context changes.
Mobile procedures conditionally use [Smoke](mobile-smoke-contract.md) and
[Script lease](mobile-script-lease-contract.md) contracts.

## Final compliance

Enabled PR-producing workflows route Gate/runtime readiness through a distinct
read-only Compliance attestation. It checks final authority and evidence, not
duplicate general reviews. `ship_pr` carries `compliance_report`;
`needs_changes` returns to single-writer Fix and fresh verification.
Substantive accepted-plan discrepancies use exposed `replan` before edits.
`needs_user_action` and cancellation retain their approval gates.

`repair_evidence` is only for named ignored packaging artifacts after underlying
source/check/runtime decisions are valid. It permits no rebuild, install,
resource reacquisition or acceptance rewrite and returns to Compliance.

## PR

`pr_merge_strategy` is `auto`, `merge`, `squash` or `rebase`. Prepare PR resolves
it via the project adapter from repository capabilities/target rules/queue
policy and carries the result. Ambiguous `auto` needs a real decision.
Generic mergeability is not method feasibility; GitHub rebase requires
`canBeRebased=true`. Conflicts may be diagnosed by isolated forced replay,
not task-branch mutation.

History rewriting requires exact authority, preserved old remote head,
final-tree proof and force-with-lease. No direct protected-branch push or
automatic method fallback.

CI may retry only an exact job on unchanged PR head when bounded first-party
evidence proves infrastructure cancellation or an unambiguous test-execution
failure after tests began. Test failures include assertion/fixture/device/
transport/timeout/service-5xx failures. Preserve run/head/job/attempt and every
failure fingerprint. Retry at most twice after the original, then return each
attempt to the deterministic watcher.

Never automatically retry user-cancelled/superseded runs, ambiguous jobs,
pre-test failures, analyzers, compilation/configuration/dependency resolution,
packaging, publication or release. Retry allowance grants no other effects.

Deterministic watchers observe checks/feedback/head/base/method together and
wake only for material changes. Pending state is not user action. A confirmed
merge goes to Cleanup, including exposed `pr_merged` recovery from stale Fix;
never repair on the delivered branch for a late check. Exact PR/branch/merge
proof remains required. With target-family continuity enabled, every fresh
PR read must also match the carried `pr_base_branch`; a missing or changed
target is not success evidence.

Every GitHub API read must first pass the inherited provider-read guard.
Preserve the existing watcher retry allowance and delay; provider timing
restricts the next API request without creating a new retry or sleep policy.

Same-repository completed issues use the provider's closing reference.
Cross-repository/partial/follow-up links do not claim closure.

## Smoke policy

`disabled`, `conditional` or `required` comes from the profile. Conditional
Gate evaluates runtime impact, acceptance and uncertainty at the approved
milestone. Bypass needs positive no-runtime-impact proof; missing resources
never waive required Smoke.

For conditional or required Smoke, `smoke_wont_do` routes to `wont_do` only
with cancellation authority and after owned work is settled. The outcome is
approval-gated and carries only `closure_reason`, which records the cancellation
source and settlement or qualified-retention references without secrets.
Stop new testing, preserve checkpoint/evidence, settle owned processes, runners,
fixtures and shells, restore state, and verify release through the project
adapter. Safe retention is allowed only when existing project authority and
contract qualify it, with evidence of the resource, ownership, status and safe
state, a responsible owner and a next action. No default waiver, inferred TTL
release, or direct Task move to Cleanup is permitted.

An interrupted Smoke owner may continue only through a supported, authorized
safety-only route to settle or safely retain its resources. Cancellation or
observation alone does not authorize ordinary runtime replay or resumption of
stopped work. Unknown ownership/cleanup or unavailable capability remains an
explicit recovery gap. Preserve the native approval gate; do not fabricate or
answer a pending approval, or repeat the same product cancellation question
merely to recover its reference. Cancellation is not Smoke PASS, delivery
readiness or cleanup of unrelated resources.

Allocate runtime/deterministic/hybrid evidence explicitly. Runtime covers
rendering, navigation, integration, restoration and liveness; passing pure
deterministic internals need no runtime repetition absent an end-to-end rule.
Do not demand reset, special fixtures or test-only semantics merely to repeat
them. Required reports are non-empty and audited. Smoke Lab preserves this
policy while disabling PR/CI and uses experimental labels.

## Role resolution

Profile roles: Plan=`orchestrator`; Gate=`gate` or orchestrator;
Implement=`implementation`; Fix=`fix`; Smoke=`qa`; PR/Cleanup=`release`;
CI/Waiting PR=`ci`. Effective configuration must actually provide every enabled
role and tool capability. Role prompts own behavior; Kent configuration owns
models, reasoning, tools and delegation.

Each assigned node owns its transition directly, without a duplicate same-role
child. Standards/Spec/Compliance are independent read-only leaves. Writers
must not duplicate them; standalone review may use callable specialists only
when no active workflow owns that pass.

## Workflow retirement

Preview deletion, classify attached history and preserve required work/results.
Active Tasks must finish or have explicit cancellation authority; replacement/
migration and possible terminal-history loss need applicable approval.
Do not move Tasks across incompatible graphs or infer deletion authority from
source retirement. Use supported operations and preserve unknown outcomes.

## Task-owned cleanup

Cleanup is resource-owning and report-first; it never deletes its own managed
root. Emit actual branch (including no-PR/report-only), canonical workspace,
Task/Session, mode and declared PR/merge/preflight fields after settling owned
shells and supported leave. Janitor independently verifies release.

Never delete primary, dirty, ambiguous or unrecoverable state. Merged-PR cleanup
requires fresh provider proof and local HEAD equal/ancestor of exact PR branch
head; same-repository remote deletion additionally requires its unchanged OID.
No-PR/report-only preserves its declared published-recoverability proof;
closed-unmerged work remains.

Only validated terminal deletion with absent path/registration is success.
Scheduled/partial/unknown outcomes preserve state and return to capable Cleanup,
not an automatic destructive retry. See [worktree contract](worktree-contract.md).

## Project adapter boundary

Projects supply contract/profile, executable verification, phase procedures,
context manifests, optional idempotent setup and required runtime/delivery/
release adapters. URLs, secrets, wrappers and policy remain project-owned.
`required_adapters` names executable dependencies; `kit_managed_adapters` is the
explicit synchronized subset. Validate paths before approved writes; no
automatic migration or activation.

Schema/versioned profile, release and command interfaces are defined by the
loader and [example profile](project-profile.example.toml). Platform details
stay in adapters: mobile resources/evidence are conditional dependencies, not
universal requirements.

Issue relations discover relevant evidence/dependencies, not added scope.
Inspect linked sibling implementations when relevant to an actual requirement/
gap, record immutable sources and adopted/rejected differences, and keep root
authority separate. Do not impose universal sibling reconnaissance.
