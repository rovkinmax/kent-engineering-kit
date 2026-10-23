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
