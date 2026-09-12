# Cleanup and Post-Session Janitor

Inspect and report exact Task, Session, worktree, branch, PR/disposition,
retained evidence and owned processes/resources. Establish verified merged
delivery or explicit report-only/cancellation authority before terminal
cleanup. Preserve unknown ownership, dirty state and ambiguous resources;
report concrete blockers rather than guessing or deleting them.

Close only processes proven owned by this task and safe to stop, retaining
evidence first. Never kill an unknown process, delete primary/user resources,
credentials or consumer state, or bypass node-owned cleanup with a task move.
Before `kent worktree leave`, freeze the original `workspace_path` and exact
`branch_name` from the task worktree, prepare the complete outgoing carrier,
and prepare final evidence through that worktree's configured command as
described below. The helper owns the final ordinary append and seal.
Then leave this session's managed worktree using the supported Kent operation
before handing off; do not delete its own root or branch in this session.
After leave, installed primary 9363 has no new `.kent` adapter: do not run
relative project commands there or re-read primary main as the task branch.
Submit the frozen carrier; the post-session Janitor uses the original owned
root and branch, not the Cleanup session's new primary cwd.

## Terminal preparation before leave

Confirm terminal authority and quiescence first. Preserve and actually read
back the retention data in the Task record outside this future-deleted root.
The local archive is not durable after Janitor and cannot replace that record.
Do not return to Implement or Plan Contract after retiring the accepted cache.

Invoke the profile's `prepare_cleanup` command with one JSON object on stdin:

- `workspace_path`, `task_short_id`: the original canonical owned root/task;
- `snapshot_sha256`: SHA-256 of the exact private `plan-contract.json` bytes;
- `scope_sha256`: original approved preview ScopeHash, NOT normalized plan hash;
- `retention_receipt`: exactly `task_short_id`, `record_ref`,
  `readback_sha256`, `data`. `record_ref` identifies the actual retained Task
  record, never a local file. `data` contains exactly `snapshot_utf8` (original
  bytes as UTF-8 text), `snapshot_sha256`, `scope_sha256`, two distinct
  `review_refs`, and `approval_ref`. `readback_sha256` hashes the actually
  read-back data encoded as sorted-key compact UTF-8 JSON, without ASCII
  escaping. The helper checks consistency, not independent remote-comment
  authenticity; the caller owns the real readback, authority and redaction.
- `final_event`: the existing ordinary ledger payload with `node_key=cleanup`,
  `evidence_type=cleanup_preparation`, `summary`, `artifacts`, `checks`,
  `decisions` and `context`. Use real current Kent Session/Run/Step identities.
- `seal_request`: the existing `terminal-evidence-seal-request-v1` object
  containing actual `operation_report_digests`, passed `redaction` proof and
  `retention_class=cleanup_report_only`. Existing kinds are `approval`,
  `merge`, `publication`, `qualification`, `runtime_source`; never invent a
  plan kind or claim qualification completed before its cleanup finishes.
- `cleanup_report_prefix`: truthful report text without any terminal marker.

Requests/previews/reviews may live in retained Task records or ignored
`build/kent-workflow/<task>/`, never as unknown restricted runtime entries.
The helper fixes its own archive path
`build/kent-workflow/<task>/plan-contract-<snapshot_sha256>.json` and receipt
`build/kent-workflow/<task>/cleanup-preparation.json`. It verifies and retains
the exact six-field accepted cache, then retires only `plan-contract.json`;
unknown runtime entries block and remain untouched. It does not mutate Kent,
GitHub, branches or worktrees, or delete checkpoint files/locks.

The helper appends the final ordinary event through the existing ledger
command, verifies its actual contents despite run-ID deduplication, and uses
the existing seal protocol. Its returned `cleanup_report` ends with exactly
one unchanged `TERMINAL_EVIDENCE_V1` marker. Preserve that report in the frozen
outgoing carrier. Do not append final evidence again or append after seal.

On interruption, retry with the exact frozen request and retention receipt.
Matching archive/prepared state can finish an absent-source retirement; an
already appended matching final event is not appended again in a new Run.
Conflicting requests/bytes or missing proof block without overwrite/restoration.
Completed receipt/marker reuse after Janitor tombstoning does not recreate
runtime evidence: the unchanged Janitor must freshly validate its retained
ledger. Preserve/report blockers; do not sweep unknown files to force success.

Emit the complete existing `cleanup_run_janitor` carrier for successful
delivery, including `cleanup_session_id` and `cleanup_report`, preserving
the incoming cleanup mode. A closed unmerged PR uses `closed_without_merge`;
retain its explicitly authorized non-empty `closure_reason` in the cleanup
report. There is no separate cancellation completion key. Explicitly
cancelled tasks may conservatively retain resources under the existing graph
and safety rules; report that preservation honestly. The existing
post-session Task Janitor owns safe worktree/branch deletion.
Unsafe or missing evidence uses
`cleanup_needs_user_action`, preserving resources and describing the real
external action required; no manual deletion workaround.

Report-only/no-PR cleanup requires explicit authority and `cleanup_mode=no_pr`
with the proper `report_only` disposition. Exact HEAD must equal a current
published branch tip, not merely be reachable from one. Preserve the source
bootstrap branch through qualification; do not confuse its retained root
with the qualification task's disposable worktree.

Acceptance requires actual worktree path absence AND absent Git and Kent
registration after Janitor, with the expected retained evidence/disposition.
A `done` status or successful transition name alone proves neither deletion
nor ownership release. Inspect the Janitor result/readbacks; if a resource
remains, report it as retained/blocked rather than claiming cleanup complete.
