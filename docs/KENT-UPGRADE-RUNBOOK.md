# Kent Upgrade Runbook

Use this runbook when upgrading the Kent execution set or when Kent changes
workflow persistence, graph authoring, runtime continuity, or Script completion
contracts.

## Approved baseline

The approved baseline is **Kent 2.6.1**, released **August 13, 2026**. Kent
2.6.0 was released **August 12, 2026**. Upgrade the CLI/TUI, background
service, and Desktop together; a mixed set is not a supported protocol
boundary.

Kent 2.6.1 also repairs legacy workflow provenance. Preserve the original
workflow source and version evidence during migration; do not accept a
hand-authored or inferred provenance value as a substitute for immutable
proof.

## Before upgrading

1. Check the installed versions of the CLI/TUI, service, and Desktop. Stop
   duplicate or stale service processes before installing the matching 2.6.1
   set.
2. Record every active Task, Current Node, execution target, resolved commit,
   worktree, Git branch, pending question/approval, and dirty path. Use
   `kent task watch` or `kent task wait` for task-level observation and
   `kent run watch` or `kent run wait` for a known Session; these commands are
   observers, not replacements for node-owned recovery.
3. Export every live Workflow and create a verified Kent database backup.
   For each workflow, also capture `kent workflow graph inspect <uuid>` as the
   complete graph document.
4. Assess the exact proposed update under
   [Execution-history and compatibility policy](../contracts/workflow-contract.md#execution-history-and-compatibility-policy).
   Source edits and an upgrade do not authorize live graph effects.
5. Verify Kent's `worktrees.base_dir`. All automatic and explicit managed
   worktree paths must remain below that directory and must not overlap the
   source Workspace. Persisted worktrees outside the namespace cannot be
   activated or restored until they are moved through Kent's supported
   worktree operation; do not repair them with direct Git worktree commands.

## Graph preview and atomic apply

Kent 2.6's graph workflow separates review from atomic persistence:

1. Run `kent workflow graph inspect <uuid> --json` and save the returned graph
   document as an audit input.
2. Edit a local copy or generate a new document. Compute and review the
   kit-owned semantic diff before calling graph apply.
3. Run `kent workflow graph apply <path|-> --json`. It validates and saves
   non-destructive changes immediately. If it reports destructive impact,
   review that fresh impact and repeat with `--confirm`. Kent applies the graph
   atomically as one operation; it does not expose a partially edited sequence
   of node/edge mutations.
4. Verify the resulting graph under the approved operation's exact postconditions.
   For a new revision, validate it in execution mode, link it non-default,
   and run the managed-worktree canary before any default promotion.

A successful local preview is not a promotion. A successful atomic apply is
not permission to rewrite Tasks. Update and forward-restore eligibility follow
[Execution-history and compatibility policy](../contracts/workflow-contract.md#execution-history-and-compatibility-policy).
For a failed new-revision rollout, restore the previous project default/link
only under the approved recovery boundary; do not move existing Tasks across
incompatible graphs.

## After upgrading

1. Validate every linked Workflow in execution mode. A graph document preview
   is necessary but does not replace execution validation.
2. Treat Transition keys, prompt references, Script stdout, and persisted
   prior-value keys as one versioned contract. Transition keys remain
   workflow-wide unique and generated keys remain source-qualified, for example
   `verification_gate_needs_changes`.
3. Re-export snapshots only after live validation passes. Keep the old graph
   export, database backup, and task inventory append-only as rollback evidence.
4. Check project-owned command implementations before synchronization. Generic
   kit sync must not replace a custom verifier or release wrapper.

## Delivery reliability adoption

This package updates Kit source, adapters and selected Workflow contracts; it
does not require an engine upgrade or authorize a Kent restart. Use the
approved package preview for repositories, publication targets and rollout
effects. Do not apply the new-Workflow/default-promotion procedure above to
an approved same-UUID update.

Before activation:

1. Qualify the published infrastructure closure and installed helper targets
   with `scripts/inspect-delivery-source`. Source tests or a merged PR do not
   prove installed bytes.
2. Capture exact graph, task, retained-execution and pending-decision preimages.
   Build the explicit compatibility plan under
   [the update policy](../contracts/workflow-update-compatibility.md).
3. Establish the approved stable boundary. A natural-stop requirement includes
   shared activation affecting new Sessions; do not force-stop Tasks or settle
   their approvals to obtain quiet. Coordinate starts, resumes and approvals
   during the actual change window.
4. Install the qualified helpers and fixed legacy/R2 routing, then apply the
   complete approved graph through the compatibility driver. Preserve original
   Task identities and decisions. Do not move old Tasks into R2 implicitly.
5. Read back installed bytes, graph, Tasks and approvals. Use a fresh read-only
   Session to verify loaded profile, skill and role sources. Existing Sessions
   keep their locked instructions until the supported refresh boundary.

Stop remaining effects on drift. Preserve the operation journal and reconcile
unknown outcomes before retry. Rollback is a compatible forward restore using
fresh task state, not a reset to an old graph regardless of intervening work.
Keep preimages and verification evidence; do not claim adoption while rollout
is deferred.

For v2 graph changes, construct the complete source receipt first, then the
private installation binding, final native graph, independent compatibility
review and exact plan. `source_admission` binds the receipt and installation
record; the review carries the canonical digest of `source_admission`.
Source inventory allows 512
files, with 8 MiB per file and 32 MiB in total; Task and retention caps are
separate and unchanged. Never trim infrastructure from a receipt to fit a cap.

Final project builders consume `VerifiedSourceAdmission` from the trusted Kit
operation caller. They do not import an inspector from a caller-selected Kit
directory or accept a raw JSON mapping as proof. The Appsome source CLI can
render unactivated snapshots; published native rendering uses its callable
builder after trusted Kit admission. The Kit current snapshot is also
unactivated. Neither snapshot contains the eventual receipt or authorizes work.

Before journal preparation and every new graph effect, the driver rechecks
admission. Recovery first settles an attempted effect read-only; a new effect
still needs fresh admission. A reverse plan must retain the gate and qualify
its runtime sources. Exact restoration of an old gate-free graph is not
supported. Pure metadata-only name/description updates may use explicit null
admission, but execution-target changes require a graph update with the gate.

## Task and Session controls

- `kent task start`, `kent task move`, and `kent task resume` accept
  `--branch-name <name>` when the initial managed-worktree branch must differ
  from Kent's short-ID branch. This is a one-time worktree identity choice;
  never infer it later from the task ID.
- `kent question --task <id>` or `kent question --session <id>` shows the first
  pending question. `kent question answer` accepts a suggested option or
  commentary and is the supported path for answering questions and approvals.
  Use it only for a real human decision or completed external action.
- `kent task resume` confirms durable requeueing before Session/Script startup
  completes. Re-read Task state after a short delay. An immediate
  `interrupted`, `workflow_runtime_start_failed`, or
  `workflow_script_completion_failed` result means recovery failed.
- A Current Node without an assigned or retained Session must re-enter through
  the smallest valid incoming `new_session` Transition with preserved values.
  A failed fan-out branch must re-enter at the fan-out source so sibling and
  Join invariants are recreated; do not start one sibling independently.

## Script stderr and recovery

Kent 2.6 preserves Script stderr diagnostics in the workflow failure and
transcript path. Do not discard stderr or classify a non-zero Script result
from stdout alone. Invalid or unavailable Scripts leave resumable interrupted
work with actionable diagnostics; recover the retained target/worktree first,
then repair only the required runtime infrastructure on that target.

A setup or target-resolution failure preserves choices to retry the retained
target, select another permitted execution target, or inspect and clean up the
retained worktree. Record the selected recovery and its proof. Never delete a
retained worktree just because `kent task start` or `resume` returned before
startup completed.

For Tasks created under older Kent versions, preserve their recorded execution
root, target, branch, and workflow revision. The former Kent 2.5 failure mode
where a project-relative Script could start before a task execution root was
available is historical migration evidence, not a reason to mutate the frozen
Task graph. If such a Task is stranded, use the smallest supported recovery
entry and the retained-worktree procedure above.

## Evidence and approvals

Questions and approvals are reserved for human decisions and external actions.
If an agent failed to retain a required pre-edit red run, reconstruct it only
when bounded and safe. Otherwise record the absence and continue with current
deterministic evidence. Never ask the user to approve the absence of
agent-owned bookkeeping.

## Pull requests

When a task fully resolves an issue in the same repository, add the provider's
closing reference to the PR body, for example `Fixes #51` on GitHub. Use a
non-closing link for partial, cross-repository, or follow-up relationships.
