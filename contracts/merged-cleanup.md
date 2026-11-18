# Merged cleanup contract

This opt-in route requires explicit Workflow selection and effect authority.
It does not change legacy Janitor or ordinary Agent cleanup. Publication
allowlists remain unchanged. There is no archive requirement or background job.

## Endpoint and ownership

Delivery observes the exact provider PR as MERGED, including repository, base
and delivered head; this grants no permission to merge. No-PR delivery and
non-delivery bypass the final disposal Script through their ordinary routes.

The authorized disposal unit is the whole exclusive released task worktree,
including dirty, untracked and uncommitted contents, plus proven local task and
Session containers. Individual files need no additional allowlist. Protect
primary checkouts, R2, installed runtimes, foreign and in-use resources.
Native force may bypass dirty checks, never active-owner refusal.

Freeze direct Task Sessions and the complete inverse-parent descendant closure
across projects before deleting any parent. `previous_session_id` is not a
parent edge. Include registered supervisors and explicitly reused helper roots;
missing ancestry or ownership is a completeness blocker, not an exclusion.

Supervisor membership uses an agent-attributed Task comment with closed schema
`merged-cleanup-supervision-v1` and fields `schema`, `project_id`,
`supervisor_session_id`, `task_ids`, `reused_root_session_ids`. Append the same
record to every listed Task. It records ownership, not human consent or native
lifecycle. Historical membership requires original assignment/launch evidence.

Retain an entire shared supervisor tree until all assignments are settled and
PR-backed outcomes merged. Retention does not block an individual Task's Done.
The final member's supervisor stops spawning, releases resources and hands off
before final disposal; other members must be terminal or in qualified final
disposal. A renewed assignment or child invalidates release. Do not stop foreign
owners or treat acknowledgement as atomic process exclusion.

## Preparation and immutable handoff

The governed native Workflow selects standing policy in the delivered group's
description: a closed `merged-cleanup-policy-v1` object with `schema`,
`project_id`, `workflow_id`, and `mode: after_merge`. Require ASCII and at most
1000 bytes, genuine native subject/route and qualified runtime. Reject extra
fields, unsupported modes and caller-selected policy. Versioned code and this
contract own behavior; the description is not an extensible list of exceptions.

Original human decisions are verified during authorized rollout and retained
as rollout evidence, not required as live inputs for every future Task.
Applicable task-specific restrictions remain binding. Freeze selected policy,
native provenance, exact subject and eligibility/release proofs in the inventory.
No external admission file or permanent dependency on an approving Session is
required. Package hashes are provenance, not permission tokens.

The Cleanup Agent prepares only; it does not delete Sessions or the worktree.
Use `scripts/cleanup-merged-task prepare` with genuine native identity while
task storage is valid. Resolve task-fixed storage; unknown or incompatible
legacy mode blocks adoption, never silently migrates it.

Prepare reads a closed JSON stdin request with
`schema: merged-cleanup-prepare-request-v1`, `pull_request_url`,
`expected_base_ref`, and `release` containing `prepared_by_session_id` and
`owner_session_ids`. The PR URL is a candidate locator, not authority.
The genuine native `cleanup_context` remains an opaque bounded string; preserve
its provenance without inventing top-level PR carriers or parsing prose for
consent. Derive subject and repository natively, compare expected base branch
and delivered head with provider facts. A moving provider base OID is an
observation, not an immutable delivery baseline equality requirement.

Persist and read back the technical inventory through the qualified installed
`workflow-project-memory workflow` attachment protocol. Do not use a direct
R2 path or borrow Task identity. Maximum attachment size is 8 MiB. The inventory
contains exact native identities, roots, branch, provider facts, full closure,
protected exclusions and admitted authority/release proofs sufficient after
victim transcripts disappear. It is not an archive of logs or source.

The closed `cleanup_result` handoff has `schema: merged-cleanup-handoff-v1`,
`phase: prepared`, `project_id`, `task_id`, `workflow_id`, and `inventory` with
`namespace: attachments`, `sha256`, `byte_size`. Task binding supplies storage
scope, never a caller-selected task key. Native string and stdout bounds still
apply; do not inline the full inventory.

After preparation the Agent performs supported leave, then in a separate step
proves actual cwd and native target outside the retiring root before emitting
`delivered`. Scheduled leave is not completion.

## Final Script

The no-argument entrypoint consumes genuine native Script stdin and the prepared
handoff. Load the qualified code closure and attachment while task storage is
usable, then change real cwd to protected primary. Revalidate provider facts,
authority, exact identities and fresh owner release before effects.

1. Delete eligible Sessions through supported noninteractive native operations.
2. Separately verify metadata settlement and physical artifacts. Remove only
   exact validated residual containers, including unknown contained files.
3. Delete the managed worktree last with authorized force and explicit native
   project/workspace identity. Preserve the local branch for supported retry.
4. Verify path, Git registration, native worktree and retained ref outcomes.
5. Emit `disposal_completed` directly to terminal Done; no executable successor.

Resolve Session paths from validated native `artifact_relpath`, not guessed
short IDs. Reject aliases, traversal, substituted roots and unsafe containment.
Do not follow child symlinks or delete the Sessions parent. Sibling artifacts
require exact ownership, never a prefix glob. Read native state through the
qualified bounded read-only observer; never mutate the database.

Success carries `schema: merged-cleanup-result-v1`, `phase: completed`, matching
identities and inventory digest, actual dispositions and postcondition counts.
Prepared, partial, queued and metadata-absent results are not complete disposal.
Use agent attribution for Script comments, never default human attribution.

## Failure and recovery

Write append-only `merged-cleanup-receipt-v2` executor observations in existing
Task comments. Bind exact Task, inventory digest, genuine producer and stage to
bounded exact subject entries. They are not native operation receipts or a
new source of permission; agent attribution alone is not authentication.
The closed receipt fields are `schema`, `task_id`, `inventory_sha256`,
`producer`, `stage`, `entries`. Script producer identifies genuine Task/node/
transition branch; historical producer identifies genuine operator Session/
Project. Entries use stable original inventory-array index and canonical full
subject digest, disposition and stage-specific observed proof. Bound each body
to 16 KiB; persist each native ACK individually. Check returned comment identity,
exact body and unchanged agent attribution on readback. Do not manufacture
success receipts from partial-error responses. Read both V1 and V2; do not
rewrite old records or immutable inventories. After a V2 write, continuation
requires a V2-capable reader; rolling back to a V1-only reader is unsafe.
After each positive native Session deletion ACK, append and read back
`session_native_settled` immediately, before snapshot or postcondition reads.
Prequalify producer, outside cwd, append command and finite capacity before
dispatch. Between positive ACK and durable append/readback, do not requalify the
producer or scan full history. Exact returned comment ID, or exact Task/body/
author query after a lost append response, must positively establish the write.
Absent/incomplete/timed-out readback blocks; never blindly repeat an append.
Record `container_settled`
physical postconditions and require complete exact coverage before root deletion.
V2 adds `worktree_native_ack`: `acknowledged` disposition, exact immutable row
binding, pre-dispatch `incarnation` (device/inode/ctime_ns) and validated
`native_outcome`. This makes no path/Git/native-absence assertion. Persist it
immediately after the native ACK, before those observations. After postconditions,
append/read back
`worktree_settled` before successful stdout. Use explicit Task/project, real
outside cwd and agent attribution, never a deleted Agent identity.

Read matching receipts with bounded complete pagination. Identical duplicates
are acceptable; conflicting/foreign/incomplete records block. Native lifecycle
ACKs are persisted individually; physical records may use bounded exact-index
batches. A lost comment response requires readback, not assumed failure.
Durable Session ACK permits no-repeat lifecycle recovery and fresh physical
checks. A previous worktree receipt applies to its old incarnation, not a
new root recreated by native Resume. Validate that same-subject instance again.
An ACK-only retry may finish physical observations without repeating Delete.
A surviving acknowledged incarnation is not settled; multiple unsettled
incarnations are ambiguous and block. A fresh admitted incarnation needs its own
ACK and proof. Full history reconciliation is still required before later effects.

Before effects, admit complete bounded evidence, iterative descendant order,
command vectors, remaining receipt growth and output capacity. Historical apply
admits every selected member before deleting any: reject overlapping removable
resources and earlier removals invalidating later read dependencies. Bound both
per-Task and aggregate output including newline to 262144 bytes. Never partition
or rewrite an oversize/conflicting batch automatically.

Receipt admission includes actual existing duplicates/metadata and the maximum
remaining stage proofs for current admitted incarnations. Reserve no comments.
Also admit their combined byte and row growth in every affected shared snapshot
scope, including supervision members and catalog overhead. Historical member
plans accumulate before the first effect and are rechecked against previously
observed scopes; own-Task receipt capacity alone is insufficient. Credit no
future Session/catalog deletion to make remaining receipts fit.
Refresh before effects; concurrent writes, new incarnations or retries require
fresh admission and can still exhaust capacity. No finite budget guarantees
unbounded retries. Exec admission uses filesystem encoding, NUL checks, pointer
overhead, platform vector/string limits and a frozen sanitized environment;
recheck before spawn without trimming that environment to bypass a failure.

Use complete metadata-first SQL scans within one qualified read-only transaction,
shared byte budget and N+1 row sentinel before materializing variable payloads.
Retain the 16 MiB/32768-row snapshot and receipt-history limits, 256 selected
Tasks and 256 KiB native comment body limit. Project only consumed current-input
`cleanup_context`/`cleanup_result`, preserving typed legacy result objects;
validate root object, carrier type and each 64 KiB value, not an arbitrary
64 KiB cap on unrelated generic node JSON. Payload bounds do not promise an
equal Python RSS bound.

Drain diagnostic stderr continuously into a 64 KiB tail with discarded-byte
count. Diagnostic overflow alone never kills a valid machine stdout ACK.
Stdout remains strictly bounded; timeout, transport errors or malformed/
oversized stdout never become success.

On failure emit no transition; retain the Script node and immutable inventory.
Compare current and frozen policy by its four typed values, not formatting or
the full graph hash. Missing or changed policy blocks new effects; settle
attempted operations first. Unrelated graph edits do not invalidate the policy.
Resume must reuse that inventory without a deleted Agent continuation. Qualify
native same-subject worktree recreation before live adoption; do not manually
reconstruct another checkout. Keep branch refs; do not delete remote branches.

Native metadata deletion can precede artifact removal. A lost response is an
unknown effect even when the row is absent. Establish operation settlement
before residual deletion or replay; otherwise block the affected remainder.
Kent may expose no supported completion barrier for a lost Delete reply.
An ACK lost before its durable receipt leaves the same unresolved window;
neither missing metadata, a timeout nor an empty directory closes it.
Receipt-backed post-root-deletion retry does not qualify arbitrary mid-RPC,
server-crash or power-loss recovery.
Native refusal, pending approvals and live-owner protection remain binding.

Historical inspect/apply uses the same finite executor and a frozen inventory
outside victim roots, confirmed within historical effect authority. Preserve
it across interruption and remove transient control files only after settlement.
Do not reopen/move historical Tasks or migrate their storage. Deletion has no
data rollback; rollback disables future effects and settles partial operations.
The assigned historical operator verifies original finite-operation authority;
digest confirmation binds the exact inventory but does not grant consent.
Historical subjects need proven native/resource ownership, not a newly installed
policy in every old Workflow. Preserve sufficient operation evidence for retry.

Use `history-inspect --request <private-file> --inventory-out <new-private-file>`
with closed request `schema: merged-cleanup-history-request-v1`, `project_id`,
sorted unique `task_ids`, and audit-only `permission_evidence` containing
`reference` and `context`. Apply with `history-apply --inventory <file>
--confirm <exact-sha256>`. The genuine standalone operator may belong to another
Project and need not have a Task binding. An already absent worktree does not
prevent qualified Session-remnant cleanup; never substitute a guessed tree.
