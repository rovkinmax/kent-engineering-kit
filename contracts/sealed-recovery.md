# Governed sealed Cleanup-to-Recovery contract

## Scope and authority

This is the platform-neutral contract for one narrowly admitted
`sealed_cleanup_recovery` path. It is not a general-purpose recovery or
cleanup permission. The shared workflow defines admission, authority,
immutable evidence, outcome and lifecycle requirements; each project owns its
adapter, runtime integration, concrete resource bounds and platform-specific
implementation safeguards.

The exception applies only to the explicitly admitted Recovery entry and its
declared `sealed_cleanup_needs_user_action` blocked outcome under the complete
reviewed inline protocol. Neither appends to or changes sealed evidence. It
does not relax higher-priority role, resource, normal-owner-leave, or Janitor
restrictions. Ordinary Cleanup, other nodes, and missing or empty
`commands.prepare_cleanup` retain their existing contracts.

## Admission and immutable evidence

The Cleanup-to-Recovery entry requires the closed
`sealed-cleanup-admission-v3` object defined inline in the project workflow.
Its payload is immutable and bound by SHA-256 over strict canonical UTF-8 JSON
(sorted compact keys, no duplicate keys, non-finite numbers, or trailing
newline). The current external effect-approval envelope and its locator are
excluded from that payload digest. Resolve the exact original human authority
independently; packet contents, agent statements, prior unrelated approvals,
or a digest alone are not authority. Source reviews, source approval, and an
actual publication receipt may be included only after they genuinely exist.

The native runtime's current Task identity and other authoritative
readbacks—not identity asserted by the carrier—determine whether the
admission applies. A mismatched, malformed, incomplete, stale, or unauthorized
admission preserves the CURRENT Task and resources, performs no recovery
effect, and must not write to a Task named by untrusted carrier data. Attempts
and blockers are recorded only on the independently verified current Task,
outside its retiring root, when the complete protocol and higher-priority
instructions permit it. Sealed evidence remains unchanged.

## Native identity and adapter responsibility

The project workflow declares the exact values needed by its recovery
adapter. The adapter MUST validate the native runner's identity envelope using
that runner's authoritative input contract. It MUST preserve the original
valid input bytes, including native identity, when forwarding them to the
fixed project-owned recovery implementation. It MUST NOT synthesize, strip, or
re-serialize native identity. The shared contract does not prescribe a
project-specific parameter count, executable path, pin, file mode, I/O byte
limit, or operating-system API; those belong to the project adapter and its
reviewed tests.

Each project adapter MUST validate its fixed implementation before invoking
it, using safe platform-appropriate file handling that cannot block on an
unsafe file type. It MUST verify the locally required file type, ownership,
permissions, bounded complete contents, and content pin before any child is
started. Exact paths, expected metadata, pin values, and implementation
technique are project-owned and must not be generalized into this contract.

## Bounded lifecycle and outcomes

The project adapter and native runtime MUST enforce documented finite input
and output bounds. Output processing must remain bounded through parsing and
mapping. Child error output must remain under the native runtime's bounded
capture rather than an unbounded adapter buffer. The adapter MUST use the
native process lifecycle and cancellation ownership; it MUST NOT detach work
from that lifecycle.

The fixed recovery implementation is invoked at most once per admitted
attempt. No automatic retry is permitted. Invalid input, pin failure, child
failure, malformed or ambiguous output, or resource-bound overflow emits no
synthetic Done or Blocked outcome. If effects may have started before failure,
the outcome is unknown: preserve the Task and resources, use the native
failure path, and require fresh human investigation. Do not replay cleanup or
infer completion. Only explicitly declared legacy outcomes may map to their
reviewed Recovery outcomes, preserving their evidence and blocker details
exactly.

## Source and live-effect gates

A source review qualifies only the exact reviewed source postimages and
read-only native draft/task-creation/wiring validity. It does not require
running the adapter, creating a placeholder for an unpublished adapter, or
performing Janitor, Task, resource, device, publication, or live Workflow
effects. Source mutation requires the separately recorded human approval for
the exact reviewed preview after its required independent governance reviews.

Publication, live graph application, entry of a specific Task, and any cleanup
or resource effect are later, separate effect gates. Shared graph availability
does not admit other Tasks. A source approval, review, preview, or candidate
admission never substitutes for those later gates. Preserve existing
higher-priority instructions and require fresh authority for any scope change.
