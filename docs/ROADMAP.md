# Roadmap

## Later Iterations

- Add Kent-native monotonic task-authority revisions, durable steer event IDs,
  transition compare-and-swap, and node entry/resume guards. Until then the kit
  uses an ignored normalized plan snapshot and does not infer authority order
  from private storage.
- Revisit broader SDK absorption, naming cleanup, deduplication, and stronger
  version coupling only after the current workflows have accumulated real
  usage evidence.

Current baseline: **Kent 2.6.1, released August 13, 2026** (Kent 2.6.0 was
released August 12, 2026). The upgrade establishes the generator,
documentation, and operational baseline. Project adoption uses new non-default
workflow revisions; it never rewrites task-backed graphs or moves existing
task records between revisions.

The kit is intentionally iterative. Stabilize real delivery first; normalize
names, remove experiments, and introduce a strict compatibility version only
after the composition is proven across projects.

## Shipped Foundation

- Platform-neutral global authority, safety, session, worktree, and context
  rules.
- Canonical role prompts with model/tool/delegation policy in Kent config.
- Profile schema 3 with work kinds, execution targets, capabilities, role
  mapping, context manifests, procedures, and adapters.
- Generated Engineering Delivery, Canary, and Smoke Lab workflows.
- One-writer Implement/Fix slices with deterministic verification fan-out,
  independent Standards/Specification reviews, Join, Gate, optional Smoke,
  final Compliance, PR/CI/merge waiting, and task-owned Cleanup.
- Deterministic Script nodes for verification dispatch/reporting, branch
  identity, CI waiting, merge waiting, and post-session Janitor cleanup.
- Append-only evidence ledger and ignored resumable Fix/Smoke checkpoints.
- Shared MCP, Jira, mobile lease/evidence, revision-preflight, worktree, and
  GitHub merge-strategy adapters.
- Approval-gated post-merge package publication for projects that declare it.

Project defaults promoted after Kent 2.6.1 candidate validation:

- Appsome Engineering Delivery v25;
- Puber Engineering Delivery v20;
- Osome SDK Engineering Delivery v12;
- Osome Slack Reader Engineering Delivery v3.

Previous defaults remain linked for rollback. Existing tasks remain frozen on
their recorded workflow revision.

On August 13, 2026, every linked workflow across the four rollout projects was
round-tripped through complete graph inspect/apply on Kent 2.6.1. Every apply
returned `unchanged` without a version increment, and every retained workflow
passed execution validation. Identical replacement UUIDs were intentionally
not created: taskless compatible workflows remain in place, while task-backed
graphs remain frozen until a real semantic delta requires a new revision.

The completed transport Canaries, taskless SDK v11 and Slack Reader v2
revisions, and Puber v19 were retired after fresh impact previews. Puber
Backlog tasks PUB-61/PUB-62 were preserved as PUB-63/PUB-64. Appsome Backlog
tasks OSM-62/OSM-63 were preserved as OSM-66/OSM-67 on v25. Retirement
evidence is recorded in
`docs/workflow-retirements/2026-08-13.md`.

## Active: Context Diet v1

### Instruction ownership

- Global `AGENTS.md` owns only universal invariants.
- Role prompts own reusable role behavior.
- Generated prompts own dynamic task inputs, exact transitions, and
  edge-specific mutation authority.
- Project `AGENTS.md` owns repository-wide gotchas.
- Project contracts contain workflow-relevant project deltas only.
- Context manifests own per-node read budgets and exclusions.
- Project skills/procedures own conditional platform and domain detail.
- Maintainer contracts are normative generator sources, not runtime preload.

The current candidate removes repeated CI, baseline, reviewer, writer, Smoke,
and merge-policy prose from runtime layers. Prompt and document byte budgets
are covered by tests to prevent silent regrowth.

### Evidence tooling

- `context.files_read` excludes the separately recorded manifest path.
- Evidence append is idempotent per Kent run, so provider recovery returns the
  original event instead of adding a near-duplicate.
- Evidence and checkpoint writers fail immediately when started without JSON
  input instead of waiting forever on interactive stdin.
- Metrics retain instruction bytes, repeated reads/questions, verification
  loops, and nullable model-call/compaction counters.

### Project adoption

- Appsome, Puber, SDK Generator, and Slack Reader project contracts are reduced
  to actual local deltas.
- Android/KMP project skills are compact indexes over lazily loaded recipes.
- Slack Reader loads its consumer skill only for public CLI, schema,
  authorization, download, and error-contract changes.
- Kit-managed checkpoint/evidence scripts are synchronized to all four
  projects.

Rollout requires commits containing the project instructions/scripts and new
workflow revisions. It does not require a Kent restart.

## Active: Delivery Reliability

- CI waiting and merge waiting remain deterministic Script nodes.
- CI Monitor may rerun one exact unchanged-head UI/instrumentation/connected/
  smoke/unit job up to twice after the original attempt when tests actually
  started. Assertion, fixture, emulator/device, transport, timeout, and external
  `5xx` failures are reproducibility candidates.
- Analyzer, compiler, configuration, dependency, package, publication,
  release, pre-test, user-cancelled, superseded, ambiguous, and exhausted
  failures are not automatically retried.
- Every attempt and failure fingerprint remains evidence; a later green result
  does not erase the earlier transient failure.
- Pending CI or an unchanged open PR never becomes an approval request.
- Post-merge late failures never return the delivered branch to Fix.
- Provider-visible output recovery may still finish a node without a final
  answer. Checkpoints preserve completed work and evidence append is idempotent
  for the Kent run; automatic transition finalization remains an upstream
  reliability follow-up.

OSM-53 completed the first live validation of the three-attempt UI-test policy.
The current rollout additionally preserves retry history across watcher loops,
restores fresh-session context/evidence contracts, and recovers same-task Smoke
leases when acquire output is lost before checkpoint persistence.

## Iteration C: dynamic routing and speculative compaction

Iteration C is an opt-in experiment on top of the Kent 2.6.1 baseline. The
upgrade exposes the primitives; it does not make them current defaults. The
experiment must be evaluated in a new non-default workflow/configuration
candidate and must not rewrite a Task-backed graph revision.

### C1. Dynamic assignee and thinking routing

Kent 2.6 can choose the next Agent's assignee and thinking level from the
previous node. The candidate policy is restricted to these eligible nodes:

- **Plan** — planning/orchestration role and an allowlisted thinking range;
- **Implement** — implementation role and an allowlisted thinking range;
- **Fix** — implementation role and an allowlisted thinking range;
- **Review** — Standards, Specification, or Compliance read-only role when that
  review is enabled, with its own allowlisted thinking range;
- **Smoke** — QA role and an allowlisted thinking range.

Join, Gate, Script watchers, publication, Cleanup, and other operational nodes
are not dynamic-routing candidates. A candidate must declare, per eligible
node, both an assignee allowlist of `agent_callable` roles and a thinking-level
allowlist supported by Kent. The policy must also define:

- deterministic fallback to the configured profile role and thinking level when
  the selected value is absent, unknown, disallowed, or unavailable;
- a reason code for every fallback, with no silent coercion or inferred role;
- an audit record containing node, candidate, selected values, allowlists'
  version, fallback reason, session context mode, and outcome;
- no project-specific role or model credential in a graph document or task
  artifact.

Continuation and compaction constraints are strict: `continue_session` and
`compact_and_continue_session` retain the existing session's established
assignee and thinking policy unless the experiment explicitly proves a new
session boundary. A new-session selection cannot invalidate an authoritative
handoff or change the work kind. Fan-out branches may select independently
only from their bounded allowlists, must preserve their stable branch output
contract, and must not alter Join/Gate semantics. Approval-waiting loops retain
their session and do not spend a new routing decision while the external
approval is pending; approval delay is measured separately from model work.

Candidate set:

1. **Control** — current configured role and thinking policy, no dynamic
   selection.
2. **Routing-A** — dynamic assignee only; thinking remains configured.
3. **Routing-B** — dynamic assignee plus thinking level from the allowlists.

Canary sequence:

1. Generate and locally semantically preview a new non-default workflow; run
   graph validation and the Engineering Canary.
2. Run the candidate in Engineering Smoke Lab, including fan-out, Join,
   approval, continuation, and recovery paths where enabled.
3. Run one bounded real task per candidate in one project, then expand to a
   second project only if the audit and quality gates pass. Existing tasks stay
   on their frozen revisions.

Record at least these metrics by node and candidate: model/token cost, number
of model calls, wall-clock and active-agent speed, queue/start latency, review
findings, verification loops/retries, recovery/interruption rate, user
corrections, and escaped defects. Report cost, speed, and quality together;
a speed gain is not acceptable if review or verification quality regresses.

Acceptance for Routing-B requires 100% valid allowlist resolution or an
explicit fallback audit, no unassigned/unknown agent, preserved continuation
identity, preserved fan-out/Join and approval behavior, and no statistically
meaningful quality regression against Control. Promote only when the measured
sample shows a meaningful cost or speed improvement without a quality loss;
otherwise retain Routing-A or Control. Roll back by restoring the prior routing
configuration and restarting Kent, leaving workflow and Task revisions
untouched. Any invalid selection, missing audit, continuation identity loss,
fan-out mismatch, approval bypass, or material quality regression is an
immediate rollback condition.

### C2. Speculative workflow compaction

Kent 2.6.0 adds topology-aware speculative compaction through the service
setting `workflow.pre_compaction_tokens` (the documented default is 70% of the
model context window). Test it independently from dynamic routing so a cost or
quality change has one attributable cause.

Candidates:

1. **Compaction-Control** — current behavior with speculative compaction
   disabled or the pre-experiment setting recorded as the control.
2. **Compaction-70** — Kent's documented 70% threshold.
3. **Compaction-60** — an earlier-compaction canary for cyclic or resumed
   sessions; add higher thresholds only if the first comparison justifies it.

The experiment is eligible only where topology indicates a continuation,
fan-out/rejoin, or approval-delay path can invalidate the context cache. It must
not compact merely because a node is large, must preserve checkpoints and
authoritative artifacts, and must not change task semantics. Measure each
candidate on Plan, Implement, Fix, Review, and Smoke when that node has a
continuation or recovery loop; exclude one-shot operational nodes.

Use the same canary sequence as routing, with a separate control and a
recorded configuration/restart event. Compare context/token cost, model-call
count, wall-clock speed, cache/compaction events, interruption and recovery
rate, repeated reads/questions, review findings, verification retries, user
corrections, and escaped defects. Keep approval waiting time separate: a
compaction candidate must not add model turns or alter the approval gate while
an external decision is pending. Fan-out branches and Join must retain their
existing provenance and output contracts, and a compaction continuation must
resume the same worktree/session state or take the declared recovery path.

Acceptance requires a measurable reduction in context/token cost or elapsed
agent time on eligible loops, no loss of checkpoint/evidence continuity, no
increase in interruption/recovery or approval delay beyond the control, and no
material quality regression. Roll back immediately when a compacted session
loses its retained assignee/context, skips a required fan-out sibling or Join,
repeats a completed mutation, loses evidence, increases recovery failures, or
causes a quality regression. Rollback restores the previous
`workflow.pre_compaction_tokens` value (or disables the experiment), restarts
Kent, and leaves frozen workflow revisions and Task records unchanged.

### C3. Configuration and restart boundary

Graph documents, prompts, contracts, skills, and roadmap changes do not
require a Kent restart. A new workflow revision is still required for semantic
changes, and graph apply itself is atomic after confirmation.

Dynamic selection values are workflow graph fields and therefore use a new
non-default workflow revision but require no restart. Agent-role allowlists,
model/reasoning policy, `workflow.pre_compaction_tokens`, concurrency, and
delegation settings are Kent service configuration. Merge an approved config
fragment into the effective `~/.kent/config.toml`, restart the Kent service,
and reopen Desktop before that configuration canary. Record the old and new
values plus the restart timestamp. Existing sessions retain their locked
execution settings; a rollback therefore governs new sessions and explicit
recovery re-entry, while active sessions follow the normal
interruption/recovery contract.

## Next

1. Observe the first real tasks created on the Kent 2.6.1 defaults and compare
   their runtime evidence with the completed transport canaries.
2. Keep active Appsome tasks on their frozen v21/v24 revisions. Start no old
   Backlog record after a lossless replacement exists; OSM-66 and OSM-67 are
   the authoritative v25 records.
3. Retire Appsome v21/v24 only after their running tasks finish; repeat a fresh
   task inventory and deletion preview first.
4. Make `kent-engineering-kit` itself an Engineering Delivery consumer using
   `scripts/validate`, no runtime Smoke, and a normal GitHub PR/CI tail.

## Follow-ups After Adoption

- Keep Appsome's release/version Jira operations project-owned. The shared
  adapter now owns only the reusable exact-target create/edit/comment/transition
  subset needed by Appsome-related repositories.
- After the Sentry adapter has real-task evidence, consider an optional
  approval-gated post-merge status node for resolve/mute. Keep the first
  iteration source-driven and avoid making every delivery query Sentry.
- Observe real instruction-byte, repeated-read, question, verification-loop,
  model-call, and compaction metrics before tuning global context compaction.
- Experiment with a lower compaction threshold only after measurements show
  that late compaction causes avoidable rereads or failures.
- Extend Web, iOS, and IoT profiles from actual repositories rather than
  speculative platform abstractions.
- Normalize experimental workflow names and delete obsolete duplicates after
  the common layout is stable.
- Version the profile/compatibility contract only after the working
  composition and migration policy are fixed.
- Add a supported structured task-comment listing path that exposes canonical
  comment IDs if the installed Kent task-comment feed still omits them. Verify
  the behavior against the Kent 2.6.1 CLI before carrying forward the older
  Kent 2.5 limitation.

## Restart Boundary

No Kent restart:

- role prompt, skill, contract, project documentation, procedure, adapter, or
  generated workflow changes;
- project commits and new workflow revisions.

Kent service/Desktop restart:

- global or project `config.toml`;
- model/reasoning/tool/delegation routing;
- concurrency, compaction threshold, or other service configuration.
