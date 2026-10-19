# Kent Upgrade Runbook

Use this runbook when upgrading the Kent execution set or when Kent changes
workflow persistence, graph authoring, runtime continuity, or Script completion
contracts.

## Kent 2.9 to 2.10 preparation

This section describes the reviewed compatibility boundary, not authorization
to install or restart Kent. Source verification alone is not upgrade readiness.
The earlier 2.6 baseline below remains historical guidance.

The inspected upstream revisions are 2.9.0
`8497311f451dee6a23039bc2c00311eadf387688` and 2.10.0
`9c7e96c39e8029f8a04118a9603902b6c75e942c`.
The exact tree comparison covers 894 changed files; source and test inspection
is not execution of every changed path.

### Compatibility gates

- Kent 2.10 removes `KENT_RUN_ID`. Updated Kit writers use genuine Session and
  Step UUIDs. Readers accept historical v1 and new v2 evidence without changing
  historical bytes or hashes. GitHub Actions run IDs are unrelated.
- Activate a qualified complete reader/writer bundle before engine upgrade.
  New helpers write v2 even on Kent 2.9. Old readers cannot be left reachable
  after this activation; file-copy order alone does not establish a safe
  boundary. Inventory retained execution roots, project-local helper copies,
  producer packets, Cleanup procedures and locked Session instructions.
- Each affected retained execution must either finish naturally with its
  existing bundle or receive a separately approved compatible deployment.
  Preserve dirty files, pins, pending Questions/Approvals and task-fixed storage
  modes. Do not force-stop Tasks or rewrite history to satisfy this gate.
- Source publication, shared Kit installation and engine restart do not refresh
  copied project helpers or retained Session instructions. Future execution
  source pins also need explicit qualification.
- `kent session-id` changes from a bare UUID to labeled Short ID, UUID and Log
  path lines. Durable owners use the UUID, never the numeric selector.
- Protocol 204 becomes 213; upgrade CLI/TUI, service and Desktop as a matching
  set. Native Task/worktree CLI JSON projections inspected for this release
  did not require a decoder change. The UI In Use indicator is not proof of
  process quiescence or safe cleanup.

### Backup and operator update boundary

1. Record exact executable versions/hashes, configuration revision, active
   Task/Session inventory, graph exports, retained roots and pending decisions.
   Verify the consumer compatibility gates above before scheduling the update.
2. Establish an explicitly approved consistent backup boundary. The operator,
   not an agent hosted by the service, stops the service and relevant clients
   when safe. Preserve resource-owner checkpoints and handle active Scripts
   through their supported procedures. Do not copy a moving SQLite database
   and Session tree and call the result a consistent snapshot.
3. Back up the persistence metadata and Session artifacts together, including
   applicable SQLite sidecars, plus the old executable set and configuration.
   Preserve credentials through an operator-managed secure backup; never paste
   secrets into evidence. Record manifest hashes and permissions, and verify
   restoration in an isolated location before treating the backup as usable.
   There is no assumed `kent backup` command.
4. The operator installs the matching 2.10 execution set and restarts it.
   Record the actual migration result and re-read native state before allowing
   affected executions to proceed. Metadata migrations 99 and 100 add nullable
   Session short IDs and connection consumption; existing Sessions are not
   backfilled with short IDs. Requalify schema/executable-bound operation plans;
   old hashes are not authorization for the changed installation.

### Qualification after update

Verify the preserved Task/current-node/pin and pending-decision inventory,
locked Session configuration and connection identity. Test a fresh disposable
Session and approved retained continuation, including Session/Step evidence,
producer consumption, recovery/retention and Cleanup paths. Confirm that
continuing an active ordinary Session steers and observes it; acknowledgement
does not prove terminal success and does not grant Task lifecycle authority.

Include multi-workspace model dispatch and post-compaction reopening:
workspace metadata is now a required request-preparation dependency. A workspace
catalog/reminder does not authorize changing execution root. Check interactive
Question/Approval handling and keyboard input without answering real pending
decisions as a test. Record which checks were actually executed and which
remain source-only or fixture-based.

Existing scalar connection configuration needs no migration. Connection sets
use recent consumption for new allocation; retained connection identity remains
significant. Optional nano naming is excluded from a mandatory upgrade:
enabling it requires a provider/data/cost decision. Invalid nano configuration
may block startup even when naming is disabled.

### Recovery limits

Migrations are forward-only. New workspace messages can be unreadable by 2.9
even though the event-log header version is unchanged. Replacing only the
binary is not a supported downgrade. Likewise, reverting only Kit helpers
after the first v2 write leaves incompatible readers.

An authorized restore must recover the consistent pre-upgrade metadata and
Session artifacts with matching executables/configuration. First inventory and
reconcile any post-upgrade external effects; restoring local state does not
undo publication, Git or other external actions. Preserve post-upgrade evidence
separately. Do not blindly replay an ambiguous effect or silently discard new
execution history.

## Historical approved baseline

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

Native graph projection normalizes edge prompt templates using Kent's Go
`strings.TrimSpace` semantics before review and hashing. Readback still requires
exact equality; it never normalizes an unexpected postimage into success.

If a historical operation dispatched successfully but its original expected
postimage omitted that native normalization, preserve its plan, review, journal,
effect identity and `ambiguous` result. Do not replay or rewrite it. Explicit
human acceptance may instead bind a fresh full actual-state readback, the exact
enumerated differences and native-source evidence in append-only rollout records.
This accepts installed state; it does not mechanically settle the old operation.
Fresh compatibility-reviewed forward plans use a separate state directory and
the actual source state. Create graph changes only for real intended changes,
such as refreshed source bindings, not to make historical bookkeeping appear
complete. No downstream step may treat the preserved ambiguous journal as a
completed operation.

For composed adoption, validate the full published builder graph. The source
admission frame and the semantic review of changed prompts are complementary;
an exact frame does not approve the surrounding prompt changes.

Native captures now include `kent-native-components-v2` with
`task_execution_bindings_sha256`: all Workflow Task target fields, source
workspace and managed worktree identities, plus referenced workspace/worktree
root mappings. Capture these in the same read-only native transaction, preserving
nulls. Historical root directories need not exist. Do not substitute Git HEAD,
timestamps or the Workflow policy for a Task's stored binding.

When changing future execution policy, review unresolved Tasks explicitly.
Existing null fields can inherit the new default on later resolution; this
consequence needs approval and native semantic evidence. Keep resolved Task
bindings unchanged. Compare Workflow policy against each confirmed journal
stage's owned before/after metadata, without rewriting the original snapshot.

New native prepare and dispatch require fresh v2 evidence and exact-hash review.
An old v1 journal can still settle a known owned pre/post image without replay,
even when full native evidence is unavailable. That settlement does not authorize
another effect: subsequent dispatch or incomplete-operation final qualification
requires v2. Already-complete journals retain owned-only readback. Do not fill
missing baseline fields using current state; preserve old artifacts and obtain
fresh reviewed plans for further effects.

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

`delivery-runtime-publication-v1` retains its exact published-only inventory.
For an explicitly approved private instruction, use the closed
`delivery-runtime-publication-v2` receipt with required `local_inputs` (including
an empty list when none apply). Each entry has exactly `kind`, `root`,
`relative_path`, `sha256`, `byte_size`, and `mode`; the only kind is
`local-instruction`. Bind one regular, owner-owned, non-executable Markdown file
beneath `.kent/skills/` at an already declared project or Kit primary source
root. Paths cannot contain globs, traversal, duplicates or overlaps. Published
and indexed files, symlinks, hardlinks and unsafe modes are rejected. Both
collections share the 512-file and 32-MiB limits; the inspection result counts
and measures both. Inventory, index exclusion, local bytes, size, mode and
identity are rechecked before admission. Locals cannot qualify inspectors,
modules, profile commands or installed helpers, and are neither required nor
inherited at an ordinary task checkout.

Keep instruction contents and private receipts outside Git and public reports.
Check the approved private postimage against its preserved original before
recording its digest; an unexpected extra file is not permission to admit it.
The unchanged `r2-installation-binding-v2` binds the entire v1 or v2 receipt
digest. Preserve prior receipts, bindings and source preimages; publish and
verify compatible consumers before separately authorized primary adoption and
binding updates. This format change has no native graph, profile, configuration
or restart effect.

Source admission qualifies published source plus explicitly bound private
instructions; it does not attest every byte the interpreter may execute. The
local Python interpreter, import environment and owner-controlled derived caches
are trusted. A tampered `.pyc`, or a stale timestamp cache with matching source
size/time, can execute despite successful source admission. `python_cache_tag`
remains interpreter metadata for existing consumers, not evidence of cache
correctness. There is no sandbox or protection against later owner mutation.
The inspector excludes `__pycache__` from source inventory and does not read,
decode, rewrite, remove or regenerate caches. No cache maintenance is required
or authorized by this source-boundary change. Diagnose any concrete stale-cache
execution failure separately; stronger execution guarantees need separate
approved scope.

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

### Native evidence when CLI output is incomplete

A waiting approval without its payload is unknown evidence, not an empty
decision list. Use the reviewed `native_evidence` binding when the CLI cannot
provide the pending decisions or retained execution context needed for adoption.
The reader supports the default local persistence root only. It verifies the
running Kent process and its open database before reading SQLite in read-only
mode. A healthy endpoint or an expected service command alone is insufficient.

The compatibility review binds the descriptor through
`native_evidence_sha256`. Its snapshot binds complete approval records and
branches, original pending Question identities, persisted Session components,
and selected original events. An empty lock after post-completion compaction
requires the retained instruction and compaction evidence; it is not proof of
lost context. Unrelated event append does not change a selected component.

Detailed inspection covers nonterminal Tasks. Terminal state, anchors and
Session ownership remain bound separately, without scanning every terminal
Session journal. Unknown Backlog history stays unknown; it must not be relabeled
never-started. Large journals are streamed under separate file, record and
aggregate limits, without increasing source-receipt or ordinary-reference caps.

Before rollout, demonstrate successful read-only qualification against the
actual pending-decision and retained-Session inventory. Test both unchanged
evidence and changes that must block dispatch. Missing local plan caches and
unrelated journal append must not prevent otherwise valid adoption.
Re-read selected evidence before every new effect. Recovery first settles an
attempted graph effect through owned-state readback; unavailable mutable
evidence blocks a new effect, not reporting the known outcome.

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
