# Cleanup Route Selection

Read the current native assignment, ownership and outgoing transition contract
before selecting a route. Task name, `kind=agent`, R2 storage and the presence
of a profile helper do not select a route: legacy Cleanup is also an Agent.

- Select **Legacy post-session Janitor** only when the native contract assigns
  deletion to a post-session Script Janitor and declares its carrier obligations.
- Select **Agent-owned cleanup** only when the native contract assigns cleanup
  effects to the Cleanup Agent, excludes legacy substitution and declares the
  Agent's own `cleanup_result`.
- Unknown or conflicting route evidence blocks dependent effects; no fallback.

The sections are mutually exclusive. Legacy helper, ledger/seal and carrier
obligations below apply only to the selected legacy route. This document grants
no deletion authority and does not change native transitions or parameters.

## Agent-owned cleanup

### Establish endpoint, authority and ownership

Distinguish a checked PR from verified merged delivery and explicitly authorized
non-delivery/cancellation. Merge is not cleanup permission. Recover the original
human authority and applicable reviews for the exact effect scope; a pending
question, summary, Task status or this procedure is not consent. Verify delivery
from original provider evidence, observing applicable provider-read restrictions.
Do not infer ancestry after a rebase merge; establish recoverability from the
actual accepted delivery and retained source evidence.

Record exact native Task, Session, project/workspace, managed worktree ID,
canonical root, ordinary local branch ref and expected HEAD/OID. Follow
`contracts/worktree-contract.md`. Inspect dirty/untracked state, Git and Kent
registrations, current checkouts, leases, live child Sessions and processes.
Settle children and resources through their real native handles and supported
owner procedures, with terminal/readback proof; an idle parent is not settlement.
Do not stop foreign Sessions or bypass node-owned cleanup with a manual Task move.
Dirty, unknown, foreign or ambiguous state blocks dependent deletion.
Inventory all ignored and runtime materials explicitly, including caches,
generated files and hidden artifacts; Git-clean alone is not sufficient.
Record a disposition for every material class before leave. Dispose of only
explicitly authorized reproducible caches. Preserve meaningful artifacts outside
the root with exact digests and complete readback before destruction. Unknown
materials, incomplete inventory or unapproved disposal block tree deletion.
Revalidate this inventory at the destructive boundary, including ignored files.

### Retain evidence outside the retiring root

Before leave or deletion, use qualified installed storage with genuine current
Task/Node/Session/Step producer identity to save original authority/review/delivery
locators, runtime evidence, child/lease settlement and exact before-state outside
the retiring root. Resolve task-fixed storage through the qualified project
adapter; unknown storage blocks writes. Retain external R2 locators without
copying document history or removing R2 storage. Read the retained record back
through its supported reader and verify exact contents and location independently
of the retiring root. Missing/inaccessible proof blocks deletion.

Preserve necessary work evidence before deletion while task-fixed storage is
usable. R2 admission may require the existing native `task.worktree_path`; do not
assume post-delete access, rebind the root or weaken identity guards. No new
post-delete R2 write or Task-comment protocol is required.

Do not invoke legacy `prepare_cleanup`, Janitor, ledger append/seal, terminal
marker or acceptance machinery for this route. If the Agent storage or evidence
contract cannot be qualified, report the blocker; do not import legacy machinery
or fabricate producer identity.

### Leave and verify the Agent's own execution target

Only after authority and outside-root retention are verified, the owning Agent
uses supported `kent worktree leave` with its genuine identity. In a separate
subsequent Agent step, without `cd` or a `workdir` override, inspect `pwd -P` and
`kent worktree status --json`. Require the actual primary cwd, no retained target
worktree (`target.worktree=null`) and no reported problems (`problems=[]`).
A scheduled leave is not release proof. Do not delete the current root.

### Retire the managed tree

Revalidate exact identity, cleanliness, owners, child/lease settlement and effect
authority immediately before deletion from primary. Use only supported
`kent worktree delete --project <project> --workspace <workspace> --json <worktree-id>`
for the exact managed tree, with no force or branch-deletion options. Never use
direct filesystem removal or bypass native refusal. Read actual response and
verify path absence and absent Git worktree registration. Read Kent registration:
absence is retirement; the exact original ID/root in `missing` topology is
retained restorative metadata, not full registration deletion. Any other state
blocks a success claim.

### Settle the local ref as a separate effect

Tree deletion does not authorize branch deletion. Require separate explicit
authority for the exact local ref and expected OID, plus retained recoverability
proof. Do not delete remote branches in this route; remote retirement needs its
own separately approved procedure and provider-state checks.

After confirmed path and Git checkout absence, freshly enumerate all worktrees
and checkouts, exclude re-entry/other owners, and inspect the full ref name.
A symbolic ref, changed OID, new checkout or unknown ownership blocks retirement.
Worktree enumeration is not atomic with ref deletion; OID CAS guards only the
ref value, not checkout/re-entry. If safe exclusion of concurrent checkout cannot
be established from actual ownership and supported coordination, retain the
small local ref and report
`tree retired; local ref retained, CAS exclusion unqualified`. This ref-only
blocker does not block otherwise authorized tree retirement. Do not claim full
ref cleanup. If the native success contract requires ref absence, report that
completion blocker through its declared route instead of claiming success.
Ancestry refusal is not permission to force-delete.
Only for the separately authorized ordinary ref with exact expected OID use:

```sh
git -C <primary> update-ref --no-deref -d refs/heads/<branch> <expected-oid>
```

Never refresh the expected OID to bypass a CAS refusal. This mechanism does not
transfer legacy Janitor qualification or authority to the Agent. Re-read ref
absence and checkout state after the effect; unknown outcomes require settlement.

### Verify completion or recover

Actual path absence and absent Git registration are deletion proof; report
native registration state and branch disposition truthfully. Include these
observed facts, retained resources and any blockers in the existing native
`cleanup_result` carrier. No additional after-result storage or evidence
infrastructure is required. Use only
the current reminder-prescribed `kent task complete` transition and required
parameters. A successful transition or Done status is not deletion proof.
Do not report successful cleanup while required deletion remains unverified.

Retention is a safe refusal/uncertainty outcome or an explicitly agreed
disposition, not the default substitute for authorized cleanup. Follow the
native blocker route; if none is declared, report the limitation without
inventing success or moving the Task manually.

On timeout, error or missing retained target, first settle actual path,
Git/Kent registration, ref and native Task state. No blind retries, recreated
tree or manual Task move. Replacement-target resume is a separately authorized
supervisor recovery action through a supported route, requiring actual startup
and readback; requeueing alone is not recovery. Source rollback cannot restore
a deleted resource.

## Legacy post-session Janitor

# Cleanup and Post-Session Janitor

Establish verified merged delivery or explicit report-only/cancellation
authority. Inspect exact Task, Session, owned worktree/branch, disposition,
retained evidence and resources. Preserve dirty state, unknown ownership and
ambiguous effects. Follow `contracts/worktree-contract.md`; close only proven
task-owned processes. Never delete the current root or bypass node-owned
cleanup with a manual Task move.

Before `kent worktree leave`, freeze original `workspace_path`, `branch_name`,
the exact declared `pr_base_branch` (or explicit `not-applicable` for
non-merged cleanup), and the complete outgoing carrier. Run the profile's
`prepare_cleanup` from that root. It is the sole owner of the final ordinary
append and seal. When target continuity is enabled, confirm the Janitor's
fresh PR-target check before treating merged cleanup as successful.
Do not append standalone evidence before it, after seal or for a blocker;
retain preflight/blocker observations in existing Task records.

Before retiring the root, the Janitor must authenticate current commentary
refs and producer ancestry, then prepare the immutable GitHub provider-read
guard while all required artifacts are still available. Invoke that
clock-only guard immediately before every later GitHub API child, including
after deletion; it must not reread the retired root. An active or unknown
restriction means no request and no automatic retry or new artifact. Preserve
the existing safe failure route and exact producer bytes.

## Terminal preparation

Read back retained authority outside the retiring root. Reuse adequate Task
evidence or assemble it from original sources: missing agent bookkeeping is
not an external action. A pending approval or downstream node does not prove
consent. Inaccessible originals, conflicting scope/hash or ownership remain
blockers. Never fabricate consent, evidence identities or proof.

R2 acceptance remains external; retain its exact validated locator/cache
without copying document history or deleting R2 storage. Use the admitted
logical evidence view, including recovery-boundary dependencies under
`contracts/evidence-recovery.md`. Do not manually repair ledger bytes or
reseal a completed result.

Invoke the configured helper with one JSON object on stdin:

- `workspace_path`, `task_short_id`: original canonical owned root and Task;
- `snapshot_sha256`: digest of exact accepted `plan-contract.json` bytes;
- `scope_sha256`: original approved preview ScopeHash, not normalized plan hash;
- `retention_receipt`: exactly `task_short_id`, `record_ref`,
  `readback_sha256`, `data`. The record is an actual retained Task record.
  `data` has exactly `snapshot_utf8`, `snapshot_sha256`, `scope_sha256`,
  two distinct `review_refs`, and `approval_ref`. Hash the actual read-back
  data as sorted-key compact UTF-8 JSON without ASCII escaping. The caller
  owns original authority, real remote readback and redaction;
- `final_event`: ordinary ledger payload with `node_key=cleanup`,
  `evidence_type=cleanup_preparation`, `summary`, `artifacts`, `checks`,
  `decisions`, `context`. Use genuine current unmodified `KENT_SESSION_ID`
  and `KENT_STEP_ID`; ignore stale `KENT_RUN_ID`. Never substitute identities
  to bypass deduplication. Qualification checks native Session/Task binding;
  inherited Step is not independently authenticated;
- `seal_request`: existing `terminal-evidence-seal-request-v1` with actual
  `operation_report_digests`, passed `redaction` and
  `retention_class=cleanup_report_only`. Report kinds are `approval`, `merge`,
  `publication`, `qualification`, `runtime_source`;
- `cleanup_report_prefix`: truthful report text without a terminal marker.

Keep requests in Task records or ignored `build/kent-workflow/<task>/`, not
unknown restricted runtime entries. The helper owns archive/receipt paths,
snapshot retirement, final-event verification and sealing; it performs no
Kent, GitHub, branch or worktree effects. Preserve its returned report with
exactly one unchanged `TERMINAL_EVIDENCE_V1` marker in `cleanup_report`.
Never claim completed qualification before its required cleanup.

Retry only the exact frozen request under the helper's recovery contract.
Conflicting bytes, fabricated identities, unknown files or missing proof
block without overwrite, reconstruction or resealing. Completed receipt reuse
after tombstoning still requires fresh Janitor validation.

## Handoff and acceptance

Leave using the supported Kent operation, then submit the frozen carrier.
Do not run relative project commands in the new cwd or re-read it as the Task
branch. The post-session Janitor owns deletion of the original root and branch.
Follow the graph's exact completion keys; do not drop `cleanup_report`,
`workspace_path`, `branch_name` or the disposition fields it declares.

Report-only/no-PR cleanup requires explicit authority, `cleanup_mode=no_pr`
and `report_only` disposition. Exact HEAD must equal a current published
branch tip, not merely be reachable from it. Preserve the bootstrap source
branch through qualification.

Acceptance requires actual path absence, absent Git registration and verified
owned branch disposition. Kent registration must be absent or the exact
original ID/root read back in missing topology; report the latter as retained
restorative metadata, not full resource retirement. A successful transition
or `done` status alone proves neither deletion nor ownership release.
