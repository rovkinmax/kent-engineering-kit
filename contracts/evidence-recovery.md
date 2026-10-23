# Evidence recovery and retention

Recovery requires the qualified ledger/runtime, archival and terminal-reader
closure. Legacy unsegmented ledgers remain supported.

## Trust and identity

Recovery and retention resolve the exact selected project and Task with
read-only native CLI observations. The Task's complete current-node or live
Session association must contain the inherited Session. Historical Task Session
membership cannot authorize an effect. Historical Session pagination is not a
prerequisite when the complete current association already proves ownership.
There is no `KENT_TASK_ID` assumption or all-project Task inventory.

New writes require canonical Session and Step UUIDs inherited from Kent.
Ignore stale `KENT_RUN_ID`; never manufacture a Run or use
a numeric Session short ID as a durable owner. JSON cannot override ownership.
Native Task/Session and execution/source workspace bindings are checked;
the current Step is **not independently authenticated**. New recovery requests
and retention receipts hash the closed classification:

```json
{"schema":"kent-owner-provenance-v2","session_task_binding":"native-readback","step_binding":"inherited-kent-environment","independently_verified_current_step":false}
```

This protects against accidental missing, stale or foreign ownership within
the declared cooperative trust model. It does not prevent intentional
same-user environment spoofing, authenticate a current Step, settle a prior
effect, grant permission, replenish a budget or authenticate human authorship.

Ordinary record `schema_version=2` carries Session/Step only. Version 1 keeps
its original Session/Run/Step and hashes. Deduplication is Task-scoped by known
Session/Step, excluding recovery boundaries and seals; unknown owners never
match and ambiguous historical matches block. Mixed chains are supported.
The outer artifact version selects the closed owner object (no nested schema
field): `evidence-recovery-v2` uses `evidence_recovery_boundary_v2` and v2
provenance; v1 retains its exact old owner and provenance. Unknown or
inconsistent versions block. Terminal seal/marker remain v1.

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
with only genuine owner fields, the request-version-bound boundary kind
defined above, and `recovery_request_sha256`. It contains no synthetic checks, decisions,
artifacts, context measurements or acceptance claims. It does not suppress a
genuine ordinary owner event and cannot serve as the final ordinary event.

Publication exclusively creates the continuation before atomically publishing
the exact canonical manifest under the existing Task lock, with durable
readback. New manifests contain exactly `schema=evidence-recovery-v2`, `request`,
`request_sha256`, and `boundary_event_hash`. A stage is a bounded temporary
publication artifact, not a registry, packet or third Task runtime entry.

### Retry settlement

An exact complete continuation without a manifest can finish publication.
An exact published manifest can settle without replay or rewriting the marker,
including from a fresh genuine Step still attached to the current Task Session.
The result records the actual `retry_owner`; retain that exact result in the
operation trace before final cleanup evidence. It is a settlement observation,
not ordinary acceptance. A partially written or conflicting continuation or
stage is preserved and blocks; it is never truncated or silently replaced.
An exact staged manifest is consumed by rename on retry.
Historical v1 retry requires the exact durable old continuation or manifest;
a caller-only old request cannot initiate a fresh v1 effect. Never rewrite
historical ownership, request digest, prefix bytes, receipt or seal on retry.

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

The new receipt schema `evidence-retention-v2` has exactly:
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

New Agent bases use `workflow-producer-source-v2` and a Session/Step owner.
Native Script bases keep `workflow-producer-source-v1` and their native
invocation identity. Historical Agent v1 bases retain Session/Run/Step;
readers validate the stored base by its own schema and compare Task/root
binding, not the historical Run against the current owner. Packet footer,
checkpoint and R2 document formats are unchanged.

The explicit Script CI archive path appends a closed
`schema_version=3`, `record_kind=script_ci_archive_v1` row. It binds the
native Script identity, Task/workspace, sequence/predecessor, validated CI
report digest and its content-addressed artifact locator. The row participates
in evidence-chain validation and hashing but is not an ordinary Agent event,
final-event count or telemetry sample; it cannot replace the required genuine
final ordinary event before a terminal seal. The archive command uses its
explicit `--script-ci-v1` boundary and a qualified in-process reader provider.
Stdin cannot supply reader proof, graph identity or a deployment receipt.
Schema-1/2 evidence and the legacy CI archive path remain readable without
rewriting historical bytes.

Retention selects an existing verified v1 receipt or exact `.preparing-HASH`
publication before constructing a new v2 receipt. It preserves the original
confirm/hash, including when preparation stopped before `receipt.json`.
Selection validates the full live source/reference closure and every existing
prepared file; conflicting, partial or foreign bytes block. A caller-only
old hash cannot initiate a fresh v1 operation or create a duplicate bundle.

Bundles are private, owner-bound and no-follow; unsafe links are rejected.
The qualified helper atomically publishes a digest-bound receipt and admitted
regular-file contents while preserving their logical identities. Complete exact
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

## Sealed recovery boundary

A sealed ledger remains immutable. Only an explicitly admitted project recovery
route may record recovery outside it. Verify genuine current native identity
and original effect authority, preserve Task/resources on mismatch, and settle
unknown effects before any new action. Never append, reseal or automatically
replay a completed/ambiguous cleanup. Project adapters own concrete forwarding,
pinning, bounds and outcome mapping; shared source approval grants no live
entry or cleanup permission.
