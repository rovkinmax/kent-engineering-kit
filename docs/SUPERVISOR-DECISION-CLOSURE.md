# Decision closure and executable replanning

## Operational supervision

The current `task-supervisor` is an operational coordinator: it scans the full
selection, gives each actionable incident a bounded slice, verifies settlement
and continues while authorized work or an actual producer of progress exists.
Its behavioral contract is [`agents/task-supervisor.md`](../agents/task-supervisor.md);
native targeting, settlement and resource handoff are described in the
[task-supervision reference](../skills/kent-engineering-kit/references/task-supervision-operations.md).
This document records the ownership boundary and qualification limits rather
than duplicating that role's command policy.

The supervisor resolves facts, original human decisions and delegated
operational choices. It may steer an owner, enact an already-authorized
approval, use a configured helper, or continue a human-selected idle ordinary
Session that is not Workflow-owned when actual identity, authority, role,
depth, ownership and project procedure permit it. It never continues a
Workflow-owned Session with `kent run --session`. A helper or operator adds
capability, not authority.

Product and repository changes remain with one qualified writer per root. An
original human decision may authorize a bounded local preservation/checkpoint
commit by the implementation/fix writer after exact files, accepted plan/node,
single-writer ownership and project restrictions are verified. This does not
authorize rebase, push, merge, publication, installation or unrelated changes.

Retry is cause-based: settle the prior effect, require a changed verified
precondition or a materially new diagnostic hypothesis, and remain within
existing run/spend/risk limits. Unknown effects block replay; replacement
helpers, new approval IDs and compaction do not reset budgets. Native recovery
is limited by actual project-qualified semantics preserving source/root/context,
pending-object settlement, human judgment, review/approval, fan-out/Join,
resources and cleanup. Waiting remains ownership; an edge or CLI help is not
semantic proof.

Configured Grill, runtime diagnosis and selected ordinary operators use the
role's bounded interfaces and current project procedures. They do not bypass
human-only decisions, create a parallel lifecycle or qualify an unsafe route.
Installation and restart remain separately authorized effects; Done and merge
do not establish installed adoption.

The October 7, 2026 investigation and exact authority/review pointers are
retained in [the implementation plan](../.todo/task-supervisor/plan.md).
The acceptance scenarios in
`tests/fixtures/supervisor-operational-cases.json` and the contract checks in
`tests/test_task_supervisor_contract.py` are static, not execution of an LLM,
Task recovery or remote merge. They additionally reject contradictory former
blanket rules and bind the observation order and effect preconditions to their
actual sections. A read-only walkthrough has the same boundary: it is design
evidence, not live behavior, native recovery, remote merge or uninterrupted
monitoring.

### Five-Session follow-up

The plan retains original Session/event locators for OSM-105/106/108/109/110,
the combined Grill dispositions, frozen preview hash, two independent review
receipts and subsequent human approval. Existing PR46 is preserved.

This historical record is not the current retry policy. Current retry is
cause-based and settled: reconcile the prior outcome, require changed
preconditions or a materially new hypothesis, and preserve the existing
run/spend/risk limits. No universal numeric quota is current policy, and
unknown settlement, failure cleanup, compaction or a new approval ID does not
reset the incident. Native recovery remains available only where the actual
project route proves its semantics and preserves ownership and gates.

The follow-up also records report deadlines, catch-up after a gap, diagnosis
versus readiness routes, token-capture evidence, CLI grammar, semantic outcomes,
source-pin/current-head distinctions, independent provider conditions and
headless/user-stop boundaries. Those factual clarifications remain useful, but
the role prompt and installed reference are the current policy source.

Kit-only source publication does not prove Appsome's standalone checkpoint/
identity procedure compatible with diagnostic mode. No Appsome adapter edits,
production operations or runtime canary are included. The runtime role remains
shell-enabled and patch-disabled: permitted project commands must support any
required persistence, otherwise eligibility fails before resource acquisition.

### Qualification after normal rollout

This is a future, separately authorized qualification procedure, not performed
by source validation or by installing a prompt:

1. After ordinary Kit adoption, start a fresh interactive supervisor Session.
   Verify the installed prompt digest and effective model/tool/depth settings;
   existing Sessions keep their locked instructions.
2. For controlled decision exercises, supply the acceptance scenarios through
   a harness with tool effects disabled or enforced fixture-only CLI responses.
   Check the actual action trace: whole-selection fairness, factual/delegated
   answers, no duplicate gate, permitted Grill/helper use, selected idle
   non-Workflow operator routing, single-writer preservation, local checkpoint
   authority and truthful outcome. Do not treat a default-role trace, including
   OSM-118/119 history, as task-supervisor qualification. Instruction-only
   claims that a real shell is sandboxed are insufficient.
3. Qualify native recovery separately in an isolated disposable Kent persistence
   root/project with an explicitly approved test graph and Tasks. Exercise
   retained interruption, diagnosed invalid input, missing Session, settled
   effects, pending approval, deliberate stop and competing ownership. Read back
   native execution/context, current authority, actual route semantics and
   preserved group/cleanup obligations. Do not recover production Tasks for
   this test or fabricate their actor identities.
   For waiting reentry, also prove settlement of the exact old pending object,
   rejection of a late answer and preservation of mandatory independent review.
4. Exercise merge decision logic with fixture-only provider responses for changed
   head, project-specific CI waiver, queue acceptance and lost response.
   A mock verifies decision handling, not the provider's actual atomicity.
   Any real disposable-repository merge needs separate remote-effect authority.
5. Qualify standalone diagnosis with an explicitly authorized disposable runtime
   procedure. Verify the adopted mode, real child identity, serialized acquisition,
   permitted private persistence, budget handoff, no Task checkpoint writes,
   semantic observations and cleanup. Exercise missing budget, conflicting
   waiting owner, lost token and unfinished processes without production effects.
   Neither role text nor an instruction-only sandbox assertion proves ownership.
6. Retain traces, installed/source digests, failed as well as passed cases, and
   explicit unresolved limits. Unsafe or unqualified project routes remain
   unavailable; neither a green static test nor a critic's advice qualifies them.

The source change has graph delta zero. Normal rollout owns installed adoption;
no direct symlink/primary/config edits or restart accompany source delivery.
Rollback after merge is a forward source revert through normal Kit delivery,
not reset, Task-history deletion or an attempt to refresh old Session locks.

## Existing replanning integration

This source update distinguishes new human decisions from facts, enactment of
prior decisions, technical planning and missing execution capabilities.
`agents/task-supervisor.md` owns the operational behavior;
`contracts/role-contract.md` and `contracts/plan-contract.md` own reusable
contracts. It does not create another authority registry or native scheduler.

## Route and authority

Fix and enabled Compliance expose `replan` to existing Plan Revalidation using
retained Plan context. The producer verifies accepted snapshot identity and
passes canonical plan/work-kind plus exact findings/authority. The planner
persists remaining obligations before ordinary `continue`, which does not carry
transient review_context into the writer. All normal review and project
acceptance gates remain; technical replanning does not impose a new universal
human approval. Source repair or publication is not authorized by replan.

An explicit selected-Task startup request can be enacted only under project
policy and native preconditions. This is not existing-branch adoption, dependency
bypass, locked-target replacement, Task movement or Git authority. Repeated
blockers remain one incident even when approval IDs change. A delivered steer
does not prove decision closure.
Before outsourcing a missing execution step to another Session, compare
supported project-level alternatives. A diagnostic trigger adjustment may be
smaller than a new publisher, but requires explicit treatment of source pins,
run counts and side effects; ordinary PR events can trigger repeatedly. This
comparison is read-only and never grants automatic workflow-source edits.

## Project coverage

Baseline inspection date: October 1, 2026. These are committed-profile source
graph checks, not installed runtime closure or live Task qualification.

| Registered project | Committed source inspected | Baseline nodes/edges | Update relevance |
| --- | --- | --- | --- |
| Kit | `ba4d40a859d6aa94661771b1270e244913abb2b3` | 21/52 | Lite Fix replan; new v5 source candidate 21/53, no Compliance added |
| AppsomeAndroid | `2b52c2c69d8e553f02ed810f3441d6d6c2c5e14d` | 30/86 | Team Fix and Compliance replan, actual provisioning builder tested separately |
| Puber | `18d3e34dae2df2e2ab78f4208ec6cc0a15711ab9` | 31/90 | Both routes; existing fresh-writer/profile contracts retained |
| osome-slack-reader | `9780fb797c766996954e235baebf56a7825527c0` | 30/86 | Both routes; no Android assumptions |
| OsomeAPI-SDK-generator | `252d6efdceec8b7e3a3ee35b2f9ebbc5df8a64b2` | Blocked before generation | Existing runtime-v2 managed-subset mismatch: missing github_observation |
| agent-default | Non-Git registered workspace; no Delivery profile | N/A | Prospective installed supervisor behavior only |

SDK has unrelated uncommitted infrastructure changes; they are not adopted or
repaired here. Its local dirty profile generates 27/72 at baseline, but that is
not proof of committed-source compatibility. Release workflow snapshots and
historical manifests are outside this change.

Updated-generator checks against those same committed profiles produced
Kit 21/53, Appsome 30/88, Puber 31/92 and slack-reader 30/88; each graph validates
with the expected one or two replan routes. SDK reproduces the same baseline
blocker before generation. These checks deliberately use `check_files=False`;
they do not assert project command-closure installation or live compatibility.

## Verification boundaries

Graph tests check real generated edges, declared parameters, retained-context
selection, project review/acceptance reachability, conditional lifecycle packet
preservation and all relevant ingress prompts. Scenario fixtures are **static
policy guards**, not a simulation of model decisions or a claim of live recovery.
Consumer profile checks distinguish clean committed source from baseline
incompatibility. Appsome tests use an explicitly selected Kit with its actual
builder and retain the unbound initial provisioning barrier.

## Activation and rollback

Source PRs do not change installed roles, global config, live Workflow graphs,
Task-owned roots or retained Session locks. New source routes are unavailable to
old locked executions until a separately approved compatible rollout. Before
that rollout, apply `contracts/workflow-update-compatibility.md`: inspect
affected started Tasks, retained producers/consumers, pending approval payloads,
runtime cohort and preserved evidence. Apply complete graphs, never edge-by-edge.

Kit v1–v4 source snapshots remain immutable; v5 is a new source candidate only.
Kit's special future rollout requires its separately approved new UUID.
Appsome pinned activation/provisioning sources are unchanged by this update.
Installed global-role/config activation uses its separately approved installation
and restart procedure; existing Sessions retain old instructions.

Before merge, rollback is a corrective source commit. After authorized merge
or rollout, rollback requires a forward source revert or compatible graph
restore, not deletion of history or reset of worktrees. No headless invocation
provides scheduled wakeup: when it ends for human input, its caller must arrange
continuation. Mandatory source tests do not prove uninterrupted observation.
