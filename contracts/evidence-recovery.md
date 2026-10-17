# Evidence recovery and retention

This is the additive Delivery Reliability v1 protocol. Legacy unsegmented
ledgers and terminal cleanup remain supported. Profile intent alone does not
qualify an old installed helper: rollout must provide the complete capable
ledger, pure runtime, CI archival, Cleanup and Janitor reader closure.

## Trust and identity

Recovery and retention resolve the exact selected project and Task with
read-only native CLI observations. The Task's complete current-node or live
Session association must contain the inherited Session. Historical Task Session
membership cannot authorize an effect. Historical Session pagination is not a
prerequisite when the complete current association already proves ownership.
There is no `KENT_TASK_ID` assumption or all-project Task inventory.

Session, Run and Step must be canonical UUIDs inherited from Kent. JSON cannot
override them. Native Task/Session and execution/source workspace bindings are
checked; Run/Step are **not independently authenticated**. Every recovery request
and retention receipt hashes the closed classification:

```json
{"schema":"kent-owner-provenance-v1","session_task_binding":"native-readback","run_step_binding":"inherited-kent-environment","independently_verified_current_run":false}
```

This protects against accidental missing, stale or foreign ownership within
the declared cooperative trust model. It does not prevent intentional
same-user environment spoofing, authenticate a current Run, settle a prior
effect, grant permission, replenish a budget or authenticate human authorship.

## One splice

The original `evidence-ledger.jsonl` is never rewritten. The only new durable
Task runtime entries are `evidence-recovery.json` and
`evidence-continuation.jsonl`. One recovery is allowed. A nonempty hash-validated
prefix and a damaged suffix are required. A seal, unknown beginning, foreign
owner, unsafe link, existing tombstone/sentinel, conflicting publication or
damaged continuation blocks recovery.

### Preview and publication

`recovery-preview --task TASK --workspace ROOT` is read-only. It requires the
existing stable Task lock and ignored staging location, and prints `request`
and `request_sha256`. It does not print or interpret damaged suffix contents.
The canonical request binds Task, original/prefix/suffix byte sizes and SHA-256,
prefix record count, fixed continuation filename, original current owner and
the provenance classification.

Pipe that exact request to `recover --task TASK --workspace ROOT --confirm HASH`.
The helper takes the existing exclusive Task lock and rechecks native ownership
and the exact raw preimage. The boundary extends sequence/previous/event hashes
with only genuine owner fields, `record_kind=evidence_recovery_boundary_v1`
and `recovery_request_sha256`. It contains no synthetic checks, decisions,
artifacts, context measurements or acceptance claims. It does not suppress a
genuine same-Run ordinary event and cannot serve as the final ordinary event.

The continuation is exclusively created and fsynced first. The canonical
manifest is staged as private
`build/kent-workflow/TASK/recovery-HASH.tmp`, fsynced and read back, then renamed
on the same filesystem into the fixed manifest path; both directories are
fsynced. The manifest contains exactly `schema=evidence-recovery-v1`, `request`,
`request_sha256`, and `boundary_event_hash`. A stage is a bounded temporary
publication artifact, not a registry, packet or third Task runtime entry.

### Retry settlement

An exact complete continuation without a manifest can finish publication.
An exact published manifest can settle without replay or rewriting the marker,
including from a fresh genuine Run still attached to the current Task Session.
The result records the actual `retry_owner`; retain that exact result in the
operation trace before final cleanup evidence. It is a settlement observation,
not ordinary acceptance. A partially written or conflicting continuation or
stage is preserved and blocks; it is never truncated or silently replaced.
An exact staged manifest is consumed by rename on retry.

### Validated view and limits

`validate_evidence_view(original, task_short_id=..., manifest=...,
continuation=...)` is the pure shared reader. Its logical records are validated
prefix, boundary, and subsequent genuine evidence. Its status permanently
remains `recovered_with_gap`. The suffix remains opaque and supplies no
references, success, consent, effect settlement or remaining budget.
Append, read, seal, CI archival, Kit Cleanup and Janitor use this view.
Legacy source-only invocations without a sibling runtime retain their old
unsegmented reader; they fail closed if either recovery file exists.

The original and each admitted logical view are bounded to 8 MiB; the logical
view is at most 8192 records. Suffix newline count is not a logical record count.
Recovery reserves capacity for the boundary, genuine final event and seal;
preview and publication validate the complete prospective marker view before
writing. Append checks capacity before writing. A second corruption is not repaired.
Malformed/duplicate/nonfinite JSON, boolean sequence aliases and request or
provenance drift are rejected.

## Terminal retention

The existing terminal ordering is preserved:

1. Reconcile effects and prepare real operation/redaction reports.
2. Append the genuine final ordinary event; seal the active logical ledger.
3. Retain and independently verify final evidence outside the retiring root.
4. Janitor renames to its marker-derived tombstone, installs/fsyncs the existing
   durable sentinel, and retires or retains raw Task runtime under that lock.

The seal itself rejects later append; there is no new active-plus-sentinel
state. Kit `prepare_cleanup` owns final append/seal and retention before marking
its preparation complete. Other projects keep their existing helper procedure;
this contract introduces no cleanup controller or Workflow node.

`retain-evidence` without `--confirm` is a read-only inventory preview.
`retain-evidence --confirm BUNDLE_SHA256` publishes only that exact inventory.
The native primary source workspace is independently resolved. The destination
is `.kent/retained-evidence/TASK/BUNDLE_SHA256/` under that source workspace.
Native source identity is exactly `{project_id,workspace_id,task_id}`.
For primary-runtime cleanup the source can be the same primary checkout; it
must not be a child of a retiring managed root.

The receipt schema `evidence-retention-v1` has exactly:
`schema`, `task_short_id`, `source_workspace`, `source_native_identity`,
`terminal_event_hash`, `recovery_request_sha256`, `owner_provenance`,
`files[{logical_path,byte_size,sha256}]`, and `bundle_sha256`.
The bundle digest covers the canonical receipt without `bundle_sha256`.
The sorted unique inventory is at most 256 files and 256 MiB total.

Healthy v1 retention includes the sealed original and required local references,
with null recovery hash and no invented recovery files. Recovered retention
includes all three fixed ledger files and the non-null recovery request hash.
Only validated-view artifact references are followed. Missing, outside-root,
unsafe or ambiguous local references block. Terminal operation/redaction
digests must have retained local byte coverage. Unknown restricted runtime
entries or unclassifiable/unsettled checkpoints block. Empty `remaining` and
`mutation_ledger` are required for an admitted transient checkpoint; unknown
effect state is not inferred from the recovered suffix.

Producer footer resolution uses the qualified checkpoint adapter and its
exact base/emitted artifact closure. An unresolved `KENT_PACKET_SOURCE_V1`
footer blocks retention. The resolver follows digest-bound producer references,
not modification time or a latest-result index. Retained validation reads the
same closure from receipt-bound archive files after the original packet paths
are unavailable; it does not restore or fabricate those paths.

Bundle directories are 0700 and files 0600, owner-bound and no-follow; unsafe
hardlinks/symlink components are rejected. Source artifacts are streamed from
admitted regular files into private numbered files `000`, `001`, and so on;
the receipt preserves their logical identities. Publication uses the private
external `.preparing-BUNDLE_SHA256` directory and atomic rename. Complete exact
prepared files can be reused; partial/conflicting files are preserved and block.
Source drift or a second competing inventory blocks. Neither path unseals,
rewrites or erases evidence.

Janitor independently verifies receipt digest, native binding, reference closure,
archive file digests and remaining live bytes before retirement. Subsequent
checks compare the already verified inode/metadata observations and namespaces;
this is cooperative freshness, not hostile same-user tamper resistance.
A verified marker-derived tombstone plus durable sentinel may resume removal
with already-absent runtime files using the retained logical view. Absence is
not claimed as current-byte verification. Managed-worktree retirement still
requires all raw ledger files and referenced reports to remain present.

Public workflow evidence carries the bundle locator/hash, not the private raw
suffix or the archive contents. Legacy Tasks without v1 or recovery have no
retroactive archive obligation. R2 accepted-plan locator/cache schemas and
exact packet production are separate integration seams; neither is invented
or reconstructed by this protocol.
