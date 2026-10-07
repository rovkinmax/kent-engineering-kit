# Cleanup and Post-Session Janitor

Inspect and report exact Task, Session, worktree, branch, PR/disposition,
retained evidence and owned processes/resources. Establish verified merged
delivery or explicit report-only/cancellation authority before terminal
cleanup. Preserve unknown ownership, dirty state and ambiguous resources;
report concrete blockers rather than guessing or deleting them.

For managed worktree cleanup and retained Janitor recovery, follow the bounded
cross-Session cleanup coordination protocol in
`contracts/worktree-contract.md` before terminal preparation, retry, or leave.
Attempt identity and contact outcomes belong in the exact cleanup Task's
existing comments outside the retiring root, not in an extra ledger event.
Complete safe task-owned child cleanup yourself; ask the user only for a real
cross-owner decision or unavailable external action. This protocol does not
change the existing terminal-preparation helper's ownership.

Close only processes proven owned by this task and safe to stop, retaining
evidence first. Never kill an unknown process, delete primary/user resources,
credentials or consumer state, or bypass node-owned cleanup with a task move.
Before `kent worktree leave`, freeze the original `workspace_path` and exact
`branch_name` from the task worktree, prepare the complete outgoing carrier,
and prepare final evidence through that worktree's configured command as
described below. The helper owns the final ordinary append and seal.
Do not issue any standalone ledger append before the helper, after seal,
or merely to record a blocker. Keep preflight/blocker observations in retained
Task records until terminal preparation can validly proceed.
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

Cleanup owns assembling and reading back `retention_receipt` from verified
original sources before invoking the helper. A missing preassembled receipt
alone is not an external blocker. Inspect the retained preview/review
references and original decision and review sources with bounded reads; do
not treat a historical Plan comment saying approval is still pending as the
outcome of a later gate. Reuse a verified adequate Task record, or retain the
exact snapshot bytes, original ScopeHash and source references in an existing
Task record outside the future-deleted root, then actually read it back and
compute the required digest. Do not rewrite original authority or append
ordinary ledger evidence just to assemble this record.

A pending approval, current node or downstream progression does not prove
original human consent. An inaccessible original decision, conflicting
scope/hash, missing original review proof or ambiguous ownership still blocks
cleanup: preserve resources and name the exact missing fact, inspected
sources and necessary recovery action. Do not fabricate approval, request
reapproval solely for bookkeeping, or invent a native audit capability.
Escalate only an established external dependency, not receipt assembly that
this owner can safely complete.

Cleanup also owns the redaction and operation-report proofs required by
`seal_request`, using actual evidence and genuine current identities. These
are agent preparation work, not documents the user must write. When configured,
the existing helper remains the sole owner of the final ordinary append and
seal; none of this recovery relaxes its admission, recovery or Janitor checks.

The following helper request and recovery protocol applies only when
`commands.prepare_cleanup` is non-empty. If the opt-in is absent or empty, do
not guess or install a helper; use the no-helper fallback below.

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
  `decisions` and `context`. Use real current unmodified `KENT_SESSION_ID`,
  `KENT_RUN_ID`, and `KENT_STEP_ID`. Never generate, export, substitute or
  replace identities to bypass deduplication. Non-empty identity strings
  are not native authentication; qualification must read back the actual
  Kent Run/Session/Step records for the final event.
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

Only genuinely successful prior preparation permits the existing validated
frozen-request/report recovery, without another ordinary append. On a safe
interruption before completion, use the exact frozen request and retention
receipt under the existing project recovery contract and real current IDs.
Matching archive/prepared state can finish an absent-source retirement; an
already appended matching final event is not appended again in a new Run.
Conflicting requests/bytes or missing proof block without overwrite/restoration.
Conflicting/fabricated evidence or any identity substitution also blocks:
preserve the original history, never accept its seal as valid, reconstruct
it, discard it or reseal it. A fresh Run alone does not legitimize an
already-completed receipt containing invalid evidence.
Completed receipt/marker reuse after Janitor tombstoning does not recreate
runtime evidence: the unchanged Janitor must freshly validate its retained
ledger. Preserve/report blockers; do not sweep unknown files to force success.

## Cleanup without a preparation helper

This fallback applies only when `commands.prepare_cleanup` is absent or empty.
The ordinary final-event instruction in the Cleanup context remains applicable
to a genuinely new successful Cleanup with a valid unsealed ledger. A
non-empty helper remains the sole final-event/seal owner when explicitly
configured.

If `.kent/runtime/<task>/plan-contract.json` exists, retain its exact bytes and
required authority references in an existing Task record or ignored
`build/kent-workflow/<task>/` artifact outside the restricted runtime. Read the
retained bytes back and verify their SHA-256 before retiring only that exact
cache. Do not delete or relocate any other runtime entry; preserve unknown
entries and report the owner-led classification/retention action they require.

Before any append or seal, inspect the original task runtime with the existing
evidence command's `validate --task <task> --workspace <workspace>` and, when
present, `read` the ledger. Inspect only the records needed for this decision;
do not copy a broad ledger dump into Task comments or retained artifacts.

If the final ledger record is a terminal seal, recover the original successful
seal request, cleanup report and marker from their actual retained Task or
Session sources. Use the existing runtime-contract support module's
`validate_terminal_chain`, `validate_terminal_seal_request` and
`validate_cleanup_report` functions, together with ledger validation/readback.
Require the same task identity; the original cleanup report must validate and
end with exactly the same marker as the validated ledger chain. The original
frozen request must validate and exactly match the marker's operation-report
digests, redaction proof and retention class; verify those digests against the
actual retained operation reports and redaction evidence. Read back the final
ordinary event's actual Kent Session/Run/Step records and confirm its
identities are unchanged. Local non-empty identity strings do not establish
native provenance.

Only after all original proof is present and consistent may Cleanup reuse the
successful result. Do not append or invoke `seal` again, derive a replacement
request or report from the marker, fabricate a marker, replace native
identities, or reconstruct missing proof. If the ledger, request, report,
marker, operation evidence, native records or provenance is missing, malformed
or conflicting, preserve the evidence and resources and report the exact
blocker without append/seal.

For a genuinely new successful Cleanup with a valid unsealed ledger, append
the one ordinary final event required by the active Cleanup context only when
there is no earlier Cleanup final event or partial seal attempt. Then use the
existing evidence command's `seal` operation with the truthful
`terminal-evidence-seal-request-v1` built from actual operation reports and
redaction evidence. Preserve the exact marker returned by `seal` as the final
line of `cleanup_report`. Do not append or seal merely to report a blocker. An
uncertain or partial terminal history is not a new unsealed completion.

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

Acceptance requires actual worktree path absence and absent Git registration.
Kent registration must be absent or the exact original managed record must be
read back with the same ID/root in missing topology; report that record as
retained restorative metadata and never call this full resource retirement.
Verify every owned branch disposition, including exact-OID local deletion and
retained report-only publication. A `done` status or successful transition
name alone proves neither deletion nor ownership release. Inspect the Janitor
result/readbacks; if a resource remains, report it as retained/blocked rather
than claiming cleanup complete.
