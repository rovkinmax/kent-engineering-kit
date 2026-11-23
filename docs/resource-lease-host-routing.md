# Managed interactive Android lease attempts

## Foreground commands with host-selected configuration

From a registered physical primary checkout or one of its verified Git
linked worktrees:

```text
resource-lease-client run --profile INTENT -- COMMAND [ARG...]
```

Use the same owner-private OS-account registry described below. A regular
Workflow route can opt in with `foreground_intents`, a nonempty, unique
list of enabled profile names. A foreground-only row has exactly
`project`, `workspace`, `config` and `foreground_intents`; it admits
neither Workflow nor standalone attempts. The field is not supported
on standalone-only routes. Both top-level Kent coordinates remain
required, but foreground admission does not query native Kent. The
profile must also be defined by the selected config. An unregistered,
ambiguous, nested, or non-enabled checkout/profile fails before
acquisition, with no fallback to another pool or `RESOURCE_LEASE_CONFIG`.

The configured allocator and pool, and the config's normal private
`runs_directory`, must be canonical trusted host paths. The foreground
runs directory must not overlap a checkout, allocator pool, or managed
runs root. It can also contain the existing explicit command records.
The client pins the selected profile and config before acquiring once
through the existing foreground lifecycle. The allocator uses the OS
account home, system `PATH`, and `/`. The child keeps its working
directory and receives both `ANDROID_SERIAL` and `UI_TEST_DEVICE_SERIAL`
from the acquired endpoint; inherited conflicts are refused. The child
uses the local loopback ADB server at `127.0.0.1:5037` without ambient
transport redirects or an exposed run directory. Only later enroll
pools qualified for that same transport.

The host-selected form does not take `--owner` or `--env`. Explicit
operator `run --config PATH --profile INTENT --owner OWNER [--env ...]`
and `start --config` remain unchanged. Host-selected foreground records
cannot be reopened with legacy `check`, `finish` or `_serve`. There is
no new public recovery command.

The caller must run a synchronous command that joins its work and
finalizers before returning. A forwarded nonzero exit or allocator
terminal result does not establish that a detached Gradle executor
stopped after losing its client. Do not enable a live pool for this
consumer until abnormal executor loss is qualified separately. Before
enrollment update or exclude older readers of the shared registry.
Before rolling back a compatible reader, disable foreground admission
by removing the foreground-only row or removing the opt-in from the
regular row, and retain the reader and receipts until known settled.
These source contracts do not authorize installation or enrollment.

## Direct standalone agent lease

An owner-private host route may optionally add `standalone_intents`, a closed
list of enabled profile names. Omission or an unlisted profile denies direct
agent admission. Each enabled name must also exist in that route's pinned
resource-lease configuration, and the route must pin its existing
`android_config`. The field belongs to the existing route
registration; it does not add a second pool, Task, Workflow, or registry.

For dedicated capacity, an entry can instead be **standalone-only**:
omit both `workflow_id` and `smoke_node`, supply `android_config` and a
nonempty `standalone_intents` list, and add `standalone_only` with exactly
`caller_session` (the enrolled native UUIDv4) and `checkout` (the enrolled
canonical linked checkout). New Workflow attempts cannot use this entry,
even when no standalone controller is running. A caller must match both
selectors after native checkout verification and before any reservation
or launch. A standalone-only entry's pool must not overlap another
entry's physical pool, even when the configs differ or the same config
is referenced twice. New admissions using either conflicting entry fail
closed. Existing Workflow and dual-use entry formats are unchanged.

Start exactly one fresh direct native Run from an enrolled primary's physical
Git linked checkout:

```text
resource-lease-client agent --profile INTENT --prompt TEXT
```

The command must itself run inside a native caller Session already positioned
on that exact linked checkout. Before creating an attempt or launching, it
requires canonical inherited `KENT_SESSION_ID`, verifies that Session's
workspace/worktree readback and exact cwd against the physical Git checkout,
and passes only that Session and an optional canonical `KENT_STEP_ID` into the
fresh launcher. The actor must be a different native Session; the caller
Session cannot claim its child Run. A terminal shell or a Session positioned
on the primary or another linked checkout is denied before attempt creation.

The host qualifies the physical checkout and primary before launch, then uses
the route's trusted `kent` executable, `kent run --workspace CHECKOUT
--output-mode=json`, and that exact checkout as the child working directory.
The command never resumes a Session or creates a Task. Its newly created
opaque `ATTEMPT_REF` is delivered only in the Run prompt. It is not a host
path, serial, configuration value, environment variable, result, diagnostic,
or evidence value. The actor calls the installed sibling tools in separate
processes:

```text
resource-lease-client begin --attempt ATTEMPT_REF --profile INTENT
resource-lease-client status --attempt ATTEMPT_REF
android-tools --attempt ATTEMPT_REF device ACTION --arguments JSON
resource-lease-client end --attempt ATTEMPT_REF
```

`begin` has no `--task` on this path. A bare UUID is not a standalone
credential and retains only its existing Workflow meaning. Before the first
allocator call, the host requires the live whole-Run controller, the grant
digest, the inherited canonical Session UUID, the exact physical primary and
linked checkout from native target readback, and the exact checkout working
directory. It seals that Session and route into the managed record. Retries
remain pinned to that attempt and cannot adopt a different Session, checkout,
intent, route, or finished record.

The controller lock spans the selected native Run. The helper independently
checks the bound controller and checkout before and during renewal; no caller
grant is passed to it. A short publication lock orders provider admission
against controller stop, but is not held during allocator I/O. If launch,
controller, native observation, or cleanup becomes ambiguous, the attempt
remains unresolved and does not automatically acquire a new scope or release
the device. A native Question/Approval, headless exit, or frontend exit alone
is not proof of Android command drain or successful cleanup.

Successful cleanup requires an explicit `end`, a clear existing command-drain
marker/fence, and a real completed allocator observation for the original
lease identity with `endpoint: null`, no cleanup pending, no quarantine, and
no retained error. A terminal `generation: null` is accepted only with that
positive completed observation. The host reports `frontend_completion`
separately from `device_cleanup`. A native Run counts as complete only when
its JSON response has `status: "ok"`, the matching canonical Session UUID,
and a nonblank final `result`, with no `outcome` or error. Kent can exit zero
for a pending Question or Approval, so exit status or `status: "ok"` alone is
not completion evidence.

The host operator must not replace an existing route for the same primary,
or enroll a pool with existing consumers or unsettled obligations. Keep
the dedicated pool and its enrollment unchanged for the selected launch.
This does not enforce a one-shot limit for the enrolled caller and is not
isolation from a process running as the same OS account. Run once; after
return or uncertainty atomically remove only the verified standalone-only
entry. Keep observer settings, unrelated routes and compatible readers
available for existing attempts. Never reuse the dedicated capacity or
retry while settlement remains unknown.

Before enabling either standalone form, update or exclude every older reader of the shared
registration, including retained processes and readers for unrelated routes;
old readers reject unknown route keys. Before rollback to an older reader,
disable new standalone admission first: remove `standalone_intents` from a
dual-use entry, or remove the standalone-only entry. Keep a compatible reader
and settlement path for all new-schema attempts, including settled
records; never rewrite or reuse their receipts or IDs. These are source
contracts, not installation, enrollment, or rollout authorization.

`resource-lease-client begin --profile INTENT --attempt UUID --task task-UUID`
is an optional entry point for an authorized native Smoke Session. Generate the
attempt UUIDv4 before calling `begin` and retain it. Use it for
`status --attempt UUID`, `end --attempt UUID`, and any retry. No private path is
a public argument or a result. An already finished attempt cannot be started
again. `attempt_pending` means a competing call owns the attempt's admission
lock; `managed_attempt_unresolved` does not prove that there is no lease.
Neither response authorizes a new attempt.

Host operators provision the owner-private
`~/.config/kent/android-tools/lease-routes.json` at the OS account home. It
contains `schema: 1`, canonical absolute `kent_executable` and
`kent_persistence_root`, and a `routes` array. Each route has `project`
(`project-UUID`), `workspace` (the canonical primary path), and `config`
(a canonical owner-private host lease config). A Workflow route also has
`workflow_id` (UUID) and `smoke_node` (the exact, unique display name in the
authorized Workflow); the foreground-only and standalone-only shapes are
described above.
Qualify the Smoke node's uniqueness when registering it. Executables and settings
must not come from a checkout, symlink or group/world-writable ancestry.

The client verifies the native Session, active Task and physical checkout.
The owning OS account is trusted; this is not isolation between processes
running as the same identity. Caller `HOME`, `PATH` and Kent socket or state
overrides do not select the native observation executable or endpoint.

New attempts are stored in private `managed-runs` next to the registration.
The first admission pins the pool and capability in the normal `binding.json`.
Retries and finalization find it there; changes to the route or its config
do not redirect an admitted attempt. Managed records are not automatically
pruned. The configured implementation cap fails closed for new attempts;
do not delete managed records to bypass it. Sustained capacity management
requires a separately qualified retention policy that cannot readmit an
old UUID.

An owner that died after helper launch without a valid allocator receipt or
confirmed terminal status is not reported as finished. Settle the original
attempt through a qualified operator recovery path; do not acquire again
or infer release from a lost response or elapsed time.

## Android device operations on an existing attempt

An operator may add `android_config` to a route to enable the managed
Android entry point for new admissions. It names an owner-private JSON
file outside checkouts. The closed schema has `schema: 1`,
`timeout_seconds`, `output_limit_bytes`, `providers` (exactly
`mobile-native`, with `executable` and `version`), the trusted
`android_sdk_root` with `platform-tools/adb` (no `PATH` separator in the SDK
path), a private canonical
`records_directory`, and optional `retention`. It does not contain a lease
section, source checkout, or second pool. Admission pins this policy and
the native verified physical checkout with the existing lease binding.
Previously admitted attempts without Android settings are not upgraded.
Previously pinned Android settings without the SDK remain readable for
settlement and evidence but cannot dispatch a device command.

`android-tools --attempt UUID device ACTION --arguments JSON` uses the
authorized attempt's exact endpoint and provider policy. Do not supply a
serial, config path or storage binding; the result names the invocation
by ID, not a private path. `android-tools` and the lease client must be
qualified as co-installed siblings in the OS account's
`~/.kent/tools/android`, not copied into a checkout. An unobservable attempt, changed
authority, or unresolved custody is a blocker, not an alternate route.
Managed allocator subprocesses use the OS account home, system `PATH`, and
a neutral working directory. The provider uses the pinned SDK and local
ADB at `127.0.0.1:5037` without caller overrides. Qualify the same local
server and SDK for the allocator before enabling the route. The provider
version probe is followed by another custody check before device commands.
Managed evidence binds invocation to attempt, lease and generation.
The existing explicit exporter refuses managed evidence; this slice does
not implement authorized delayed managed export.

A managed device dispatch publishes its custody marker before the
provider call. The managed release fence and marker share one short
publication lock. A known completed process group may clear only its own
marker. A live or uncertain marker blocks all client release and cancel
paths, including helper interruption. This does not prevent the allocator
from taking its independent TTL path if the helper cannot renew; expiry
alone is not verified cleanup or a safe handoff.

Do not migrate existing explicit consumers or reuse managed attempts for
foreground `run`. Before managed rollout, qualify the actual bundle, safe
host disposition after helper loss, evidence export/readback, and the
release Smoke consumer. To roll back, stop new managed admission and retain
a compatible reader while any managed records or export obligations
remain. Lease release does not retire its evidence. Do not delete managed
records to make an old reader pass.
