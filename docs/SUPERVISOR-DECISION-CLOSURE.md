# Decision closure and executable replanning

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
