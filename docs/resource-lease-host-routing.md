# Managed interactive Android lease attempts

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
(`project-UUID`), `workspace` (the canonical primary path), `workflow_id`
(UUID), `smoke_node` (the exact, unique display name in the authorized
Workflow), and `config` (a canonical owner-private host lease config). Qualify
the Smoke node's uniqueness when registering it. Executables and settings
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

Do not migrate existing explicit consumers, foreground `run`, or Appsome
in this source slice. Before rollout, qualify the actual bundle, safe
host disposition after helper loss, evidence export/readback, and the
release Smoke consumer. To roll back, stop new managed admission and retain
a compatible reader while any managed records or export obligations
remain. Lease release does not retire its evidence. Do not delete managed
records to make an old reader pass.
