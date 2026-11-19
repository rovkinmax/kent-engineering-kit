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

This change does not migrate foreground `run`, retained explicit
`start/check/finish`, Appsome, `android-tools`, or evidence consumers.
It is not a deployment or a certification of real emulator readiness.
