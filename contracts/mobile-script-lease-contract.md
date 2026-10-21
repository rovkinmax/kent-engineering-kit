# Mobile Script Lease Contract

This defines typed Script ownership for `emulator-resource-lock.sh`.

## Shared resource and owner model

Session and Script callers use the same resource directory, per-resource guard,
and owner record. The owner is exactly one of:

- `Absent`;
- `LegacySessionOwner`, with exactly the existing fields
  `token`, `resource`, `pid`, `cwd`, `created_at`, `task_id`, and `session_id`;
- `ScriptOwnerV1`, with exactly
  `owner_schema`, `owner_kind`, `token`, `resource`, `task_id`,
  `context_sha256`, `pid`, `process_platform`, `boot_id`, `process_start`,
  `pgid`, `cwd`, and `created_at`;
- `UnknownOwner`.

The Script record uses `owner_schema=mobile-script-owner-v1` and
`owner_kind=script`. Owner records are bounded UTF-8/ASCII key-value records:
one `key=value` field per LF-terminated line, no duplicate, missing, unknown,
empty, or malformed fields. A valid Session record requires an actual Session
UUID; historical `session_id=unknown` is `UnknownOwner`. Unknown, partial,
unsafe, or conflicting state is preserved and never rewritten or TTL-reclaimed.

Script tokens are private bearer values. They may appear only in the owner
file and the owning process's private memory or pipe. Never place a Script
token in checkpoint, Task, route-carrier, log, or workflow evidence. This
restriction does not change the existing Session checkpoint contract. Status
and recovery inspection must redact tokens.

## Script request protocol

Requests travel only on standard input. They are ASCII, at most 1 KiB, end in
one LF, contain no CR/NUL, and use the exact field order below. Duplicate,
unknown, missing, reordered, oversized, or invalid fields are rejected. No
token or other secret is accepted as a command-line argument.

`acquire-script` and `acquire-script-any` accept exactly:

```text
schema=mobile-script-lease-request-v1
task_id=task-<native-task-uuid>
context_sha256=<64 lowercase hexadecimal characters>
pid=<positive decimal parent-process ID>
```

The project wrapper obtains Task identity from native readback, computes its
stable context digest and passes its actual parent PID. Script identity must
not be borrowed from an Agent Session.
`entry_transition` is business routing context and is
not part of the stable owner digest. The Kit adapter bounds and independently
observes the supplied process PID; OS observations are cooperative identity,
not native Kent authentication.

`acquire-script <resource> [wait_seconds]` and
`acquire-script-any <resource>... -- [wait_seconds]` may create an owner only
when the resource is `Absent`. They have no TTL, takeover, resume, force, or
orphan cleanup behavior. On success, stdout is exactly:

```text
schema=mobile-script-lease-result-v1
resource=<selected-resource>
token=<opaque-token>
owner_sha256=<sha256-of-exact-owner-file-bytes>
```

The resource and token are each returned once and never echoed to stderr.
`acquire-script-any` returns exactly one selected resource. Existing Session,
Script, Unknown, partial, or unsafe ownership remains busy and unchanged.

`release-script <resource>` accepts exactly this ordered request on stdin:

```text
schema=mobile-script-lease-request-v1
task_id=task-<native-task-uuid>
context_sha256=<64 lowercase hexadecimal characters>
pid=<positive decimal parent-process ID>
token=<opaque-token>
owner_sha256=<64 lowercase hexadecimal characters>
```

Release succeeds only for a valid Script owner whose Task, context, token,
exact owner-byte hash, and currently observed PID/platform/boot/start/group
identity all match the request and record. A mismatch or absent, Session, or
Unknown owner is preserved and returns a failure. The wrapper requests release
only after it has established that its owned work completed; the kernel cannot
prove that descendants, build daemons, or device operations stopped.

`inspect-script-recovery <resource>` is read-only and emits exactly:

```text
schema=mobile-script-recovery-v1
resource=<requested-resource>
owner_kind=Absent|LegacySessionOwner|ScriptOwnerV1|UnknownOwner
owner_sha256=<64 lowercase hexadecimal characters|none|unknown>
```

It never returns a token and never creates a runtime directory, resource
directory, or guard. Unknown process status, PID death/reuse, boot change,
leader exit, or unavailable native Script metadata do not authorize takeover;
ambiguous recovery preserves the record and follows the caller's existing
`needs_user_action` path.

## Existing Session interface

The existing `acquire`, `acquire-any`, `resume`, `resume-owned`, `release`, and
`status` names, arguments, and normal valid-Session results remain. Session
TTL reclamation is permitted only for a fully valid `LegacySessionOwner`, and
only when `age > caller_ttl`. Session `resume` and `resume-owned` cannot
reinterpret Script or Unknown records. Token-only Session `release` can remove
only a valid Session record. Valid same-Task inter-Session recovery is
preserved. Unknown historical owner expiry or recovery intentionally changes
from permissive behavior to fail-closed preservation.

## Guard and process identity

All operations use one stable per-resource guard inode. Guard files are private,
regular, single-link files and are never unlinked during unlock. The selected
backend is fixed by the supported host platform and invoked by its canonical
system path: Darwin uses `lockf -k`; Linux uses the supported `flock` backend.
The other backend, PATH-shadowed executables, unsupported platforms, and
unknown selections fail closed. No second guard namespace or generic resource
service is introduced.

Process identity is bounded local observation only. Darwin uses one
`proc_pidinfo(PROC_PIDTBSDINFO)` observation plus boot identity; Linux uses
bounded `/proc/<pid>/stat` parsing plus `/proc/sys/kernel/random/boot_id`.
Unsupported, failed, ambiguous, or excessive probes are unknown and block
Script acquisition or release. No `ps` heuristics are used. These cooperative
fields do not attest a native Kent Script PID or prove that work escaped from a
leader has stopped.

## Activation boundary

Isolated qualification uses disposable owners, processes and fixtures. It does
not qualify installed consumer bytes or authorize resource/device effects.
Publication, activation and Task migration follow their applicable effect gates.
