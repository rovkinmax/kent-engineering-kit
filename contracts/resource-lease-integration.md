# Cooperative resource-lease integration

This opt-in adapter invokes an independently installed `resource-lease` CLI.
It does not contain the resource service, Android backend, or a Kent lifecycle
extension. Adoption uses a coordinated Kit/workflow update and Task migration
before new consumers start. Old and new ownership protocols are not intended
to run concurrently; this integration adds no legacy-consumer exclusion or
separate device-discovery infrastructure. Preserve existing work during the
coordinated transition; never reinterpret old lock receipts as new leases.
Like the current standalone CLI, this adapter requires Python 3.11+ and a POSIX
host. It does not claim Windows support.

## Configuration and interface

The project profile adapter key is `resource_lease`; its managed executable
filename is `resource-lease-client`. Register the same key in required and
Kit-managed adapter lists when opting in.

`resource-lease-client` uses a private JSON configuration with exactly:

```json
{
  "schema": 1,
  "executable": "/absolute/path/to/resource-lease",
  "pool": "/absolute/path/to/private-pool",
  "runs_directory": "/absolute/path/to/private-consumer-runs",
  "profiles": {
    "ui.clean": "project-ui-clean",
    "ui.persistent": "project-ui-persistent",
    "smoke.clean": "project-smoke-clean",
    "smoke.persistent": "project-smoke-persistent"
  }
}
```

Profile names and capabilities are project decisions. Missing profiles fail;
there is no fallback. The adapter does not start or initialize the service.
The installer pins the executable artifact and prepares the pool separately.
Configuration, run directories, receipts and control files are owner-private.
No receipt secret may appear in argv, checkpoints, logs or workflow evidence.
Keep at most 64 proven-finished run records. Unknown/failed records are retained;
at 256 total run entries admission stops for explicit operator inspection.

Commands:

```sh
resource-lease-client run --config CONFIG --profile PROFILE --owner LABEL \
  --env VARIABLE=endpoint_key -- COMMAND ARGUMENTS
resource-lease-client start --config CONFIG --profile PROFILE --run-dir RUN \
  --project PROJECT --task TASK_UUID --session SESSION_UUID
resource-lease-client check --run-dir RUN
resource-lease-client finish --run-dir RUN
```

`--env` is repeatable. The adapter rejects conflicting inherited values and
missing endpoint keys. A project wrapper supplies platform-specific mappings
and commands; the adapter itself has no Android or Gradle knowledge.

## Stop provenance

Bindings retain `custody_marker_protocol: 1`. Every new status contains the
required `parent_command_stop` enum (`not_started`, `pending`, `confirmed`,
`unconfirmed`) and `external_stop_unconfirmed` boolean. The public
`command_stop` aggregate is derived from those fields and the existing
`command-stop-uncertain.json` marker; it is never used as intrinsic provenance.
The external negative is sticky, and heartbeats preserve it. A parent command
may be `pending` while active child operations use their owned custody marker.
The marker keeps its baseline schema-1 reason/operation format and exclusive,
fsynced create-before-dispatch lifecycle; only its live context may confirm and
remove the exact observed marker. A confirmation cannot clear a global or
foreign negative.

Global uncertainty handoff updates the existing private status without requiring
a live lease or helper. It preserves lease observations and heartbeat time.
Existing runs with absent or malformed required provenance, or unknown marker
formats, are rejected without migration. No remote journal or additional run
state files are used.

`finish` performs ordinary release even when command outcome is unknown. It
reports confirmed resource cleanup separately, persists the terminal lease
observation, retains `command_stop: unconfirmed`, and returns failed status with
a nonzero result; cleanup does not prove business success or remote drain.
Unknown/failed runs are not pruned.

Legacy stop records have no general-purpose recovery reader or command.
Incident-specific recovery requires its own exact evidence and authority.
Preserve original errors and unknown business effects; never replay or reacquire
a resource as automatic recovery.
`run` acquires one lease, renews while the direct command runs, waits for its
synchronous finalizers, and releases. It preserves command failure and reports
cleanup failure separately; lease loss cannot produce success. Ordinary
signals stop known owned work with bounded waits. It does not prove that an
escaped daemon or arbitrary descendant stopped.

For `start`, the caller allocates an exact RUN path below runs_directory and
records that path before dispatch. The helper owns acquisition and renewal
across tool calls. Repeated `start` for the same live exact binding is
idempotent; it must not create a second request after an ambiguous response.
Stopped helpers cannot be restarted with an expired receipt.

`start` and `check` return JSON containing `run_directory`, public lease
identity/state and `endpoint`. Read the exact endpoint for every device call.
`check` requires both a live helper observation and a currently active lease.
It never renews or revives a stopped helper.
`finish` requests ordinary release and returns its verified outcome. It never
kills a process discovered from a stored numeric PID. Keep failed/unknown
receipts and diagnostics for explicit recovery.

## Native owner observation

The smoke helper binds the original Project, native Task UUID and Session UUID.
It uses bounded `kent task sessions` pagination and `kent task show` readback.
Renew only while the exact Session is `running` and the Task is nonterminal.
Stop renewal permanently on Question, idle, missing owner, terminal/unknown
state, invalid response, timeout or CLI failure. Do not immediately release
merely because observation fails; existing lease expiry is the fallback.

Renewal cadence is derived from CLI `expires_in`, no greater than one third
of the observed remaining lease lifetime. CLI operations are bounded.
When a resource-lease subprocess exits nonzero, the client reports the requested
operation and exit code without copying subprocess stdout or stderr. Acquisition
failures additionally identify the existing pool-status command using the
executable and pool from the client configuration and advise inspecting the
original run before retrying. The displayed `<executable>` and `<pool>` are
literal placeholders, not values or a claim that the executable is on PATH.
This guidance does not classify the failure cause or establish cleanup. The
client performs no additional observation, service startup, or retry. Existing
exit codes, result schemas, receipt handling, renewal, and cleanup behavior
remain unchanged.

Same-Session continuation may check and retain an exact healthy binding.
Cross-Session handoff ends the old lease and acquires a new one; no implicit
owner transfer. Revalidate installed artifact and application state as required
by the project procedure.

## Limits and effects

This is cooperative ownership, not an atomic Step fence or OS sandbox.
Native `running` includes drain. Polling may miss short pause/resume windows;
stuck native running state can keep renewal active. Wrapper death stops its
heartbeat but may leave a descendant capable of direct device access.
CLI expiry cannot fence an arbitrary stale direct ADB caller from a reused
serial. Report unresolved work honestly; do not promise otherwise.

Installation, emulator startup, credentials, workflow activation and publication
require their own applicable authority. Source synchronization does not refresh
locked Session instructions. New consumers must use a compatible fresh context;
old active consumers retain their original adapter and pool boundary.
