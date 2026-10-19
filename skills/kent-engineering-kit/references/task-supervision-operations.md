# Task supervision operations

Use the section relevant to the current operation. The caller's role and
project contracts determine authority; this reference describes mechanics.
Check current CLI help before an unfamiliar invocation.

## Locate current work

Resolve a Task's project and stable ID with `kent task show`, then inspect
current executions and `kent task sessions`. Use fresh native pending
decisions, not approval IDs copied from old comments. Session names, empty
live-session lists and a command's enqueue acknowledgement are insufficient
to establish execution ownership or settlement.

Read original human messages or Question answers when a decision is conveyed
by another agent. Retain the actual Session and event/Question locator in an
agent-authored Task reference. Do not manufacture human authorship with
`--author user`. For continued work, record durable authority pointers as
well as sending the active owner a message.

## Address the right recipient

- Active Task owner or ordinary operator: `kent run steer`.
- Pending factual Question: inspect with `kent question`, then answer the
  verified owning Session using `kent question answer`.
- Pending Approval: inspect its actual transition and values before
  `kent task approve`; preserve human-only gates and factual prerequisites.
- Retained Task execution: native `kent task resume` when applicable.
- Explicitly selected ordinary idle operator: `kent run --session` with the
  bounded assignment after verifying it is not Workflow-owned.
- New helper: `kent run --agent` with an available qualified role.

Kent 2.10 continuation of an already active Session steers that execution and
observes its outcome; it does not start a second writer or refresh its locked
instructions. Prefer explicit `kent run steer` for active-owner feedback.
Continuation may warn about ignored execution overrides; thinking-level
changes have their native semantics, not a new authority grant. Workflow-owned
Sessions still use Task lifecycle operations only under the owning contract;
active-session continuation does not authorize Task resume or route changes.

For durable Session identity, `kent session-id` prints a bare UUID in 2.9
and diagnostic Short ID/UUID/Log path lines in 2.10. Extract the UUID and verify
native ownership; numeric short IDs are convenient CLI selectors, not durable
artifact identities. New evidence uses inherited Session/Step UUIDs, never a
fabricated Run. Native Session/Task readback does not authenticate current Step.

Question answering may select the first pending object, not an exact ID.
Re-read immediately before answering; this is not an atomic compare-and-set.
Changed targeting or concurrent answers need reconciliation. Read back the
actual response and continuation; successful transport is not decision closure.

Kent 2.9 headless runs can exit zero with `status: "ok"` and
`outcome.kind: "question"` but no result, even with `--quiet`. Inspect the
pending Question or Approval rather than treating the helper as finished.
An answer resumes that execution; do not start `--continue` or Task Resume
to release this wait. On older runtimes or without a Question tool, return
the unresolved blocker to the operator.

Question history in 2.9 includes `answered_by_session_id`. An agent identity
is useful provenance; a null value is sessionless/legacy presentation, not
proof of human authorship. Verify the original human source and exact locator.
Native rejection of a child's answer to its direct parent's ordinary Question
does not protect all ancestors or Approvals and does not grant answer authority.

## Resume and route selection

Re-read the node, retained execution, source/root, pending decisions and
unfinished effects before recovery. A native Resume acknowledgement means
queued work; check actual execution after a bounded delay.

Kent 2.9 can recover a committed-but-unstarted Session with zero
`LastSequence`, false `ConversationEstablished` and zero `ModelRequestCount`.
Distinguish this from an absent binding or lost established history; neither
is repaired by inventing a new retained execution. Plain continuation retains
settings; compaction continuation may apply the target configuration under
the same ID. Check the compaction event and refreshed lock, not ID rotation.

For a route change, inspect the complete transition's prerequisites, context
source/mode and required values. Preserve provenance and meaning of carried
inputs. Verify how the native operation settles any old pending object and
prevents late competing continuation. An existing edge alone does not prove
those semantics. Missing retained execution needs a supported new-session
entry. Do not invent successful result fields to satisfy a route.

A technical blocker may need diagnosis or replanning before it is resolved.
Use a route with that meaning; a readiness approval cannot substitute.
If the project has no qualified route, report the precise missing capability
and arrange an authorized repair owner/proposal. Do not test uncertain
movement semantics on a live Task.

## Transfer bounded work

Supply the executor with the intended outcome, original authority, exact root,
allowed effects, evidence, current ownership, prior attempts, remaining limits
and completion/cleanup conditions. Check actual locked capabilities; source
role changes do not refresh existing Sessions.

For writing or resources, verify explicit release/handoff from the previous
owner and absence of competing work. An idle or waiting Task can still own
its checkpoint, source or device. Never substitute a supervisor identity for
the executor's genuine Task/Session identity.

Keep observation waits separate from executor deadlines. If a run times out
after possible effects, reconcile those effects before retry or reassignment.
Do not abandon a live helper or count a delivered assignment as completed work.

## Runtime and external effects

Before a future Kent 2.9 upgrade, qualify CLI and server compatibility together;
the CLI version alone is insufficient. Private main-workspace
`.kent/config.local.toml` can override shared role settings. Inspect effective
policy without copying private files or secrets into task roots. Connection
definitions and shell concurrency remain server-owned; do not migrate them
as part of project repair. Provider credential environment names may be
excluded from child shells, so do not reuse those names for project credentials.

Native worktree retirement may reject accepted work, or fail after partial
idle-Session retargeting. Re-read actual state; do not force cleanup or retry
destructive actions automatically. A retained Task with a missing root is not
permission to select a replacement execution target. Preserve Task identity
and request the necessary explicit selection through the supported interface.

Before delegating runtime work, verify a compatible project acquisition,
token-persistence and cleanup procedure and absence of a conflicting owner.
The helper uses its genuine identity for serialized acquisition, then verifies
exclusive ownership and the persisted token before runtime actions.
An ordinary operator cannot use a Task-only lease procedure by borrowing
another Task ID. An absent lease, a lost token and foreign ownership require
different project-supported treatment; TTL expiry alone proves none of them.

For a permitted GitHub PR merge, target the explicit repository/PR and use
`--match-head-commit` with the verified accepted head. Preserve the selected
method, current-base validation policy and repository protections. Read back
merged versus queued state before declaring success.

Record compact intent/result in existing authorized evidence. On an unknown
outcome, reconcile the exact operation before repeating it. Audit records
are cooperative duplicate prevention, not a distributed lock.
