# Workflow Generation

Live workflow definitions are stored in Kent. Project JSON files are audit
snapshots, not an alternate source of truth.

## Supported Baseline

- Kent 2.6.1 or newer;
- profile schemas 3 and 4 at a strict compatibility boundary;
- workflow-wide unique transition keys;
- explicit execution-target policy;
- execution-mode validation before task creation.

Schema 3 retains legacy `release_topology` and known-command synchronization.
Schema 4 requires explicit command ownership and versions plus a closed
`release` table. Its approved topology/adoption identities are
`appsome-release-publication/managed-in-place`,
`puber-release/managed-in-place`, `sdk-merged-main-publication/metadata-only`,
and `slack-reader-release/managed-in-place`. Synchronization plans every
managed write before applying it; no project migration or activation is
automatic.
The closed release fields are `topology_kind`, `adoption_mode`, `spec_path`,
`builder_path`, and `snapshot_path`; managed-in-place requires a builder path,
metadata-only requires none. `required_adapters` declares executable runtime
dependencies, while `kit_managed_adapters` is the exact synchronized subset;
remaining adapters and platform-specific startup/device/build/install work
remain project-owned.

## Selected-revision release closure

Schema 4 preflight reads the release spec, source manifest, snapshot, optional
builder, profile-derived paths, declared job workflow paths, and approval Script
paths from the selected Git revision. It derives the mandatory closure before
applying manifest additions, expands sorted regular-file trees, enforces Git
file modes, checks declared prompt coverage, and records raw blob SHA-256
digests. It does not trust working-tree release files or invoke a builder,
generator, Kent client, graph validator, or graph apply.

The emitted preview is deliberately source-side:
`source_contract_valid=true`, `runtime_attested=false`,
`job_sources_validated=false`, `activation_authorized=false`, and
`snapshot_json_valid=true`. Schema 3 retains its existing checked-path and JSON
shape. Runtime attestation, external-root byte capture, project adapters, and
live publication remain separate contracts.

## Generated Workflows

`Engineering Delivery` composes:

- Plan and optional deterministic external branch identity;
- one-writer Implement/Fix slices;
- deterministic verification dispatch;
- read-only Standards and Specification fan-out, Join, and Gate;
- optional runtime Smoke;
- final Compliance;
- optional PR creation, deterministic CI wait, failure classification,
  deterministic merge wait, and Cleanup;
- optional approval-gated post-merge package publication.

`Engineering Canary` validates the planning, writer, verification, and cleanup
core without PR/CI or runtime Smoke.

`Engineering Smoke Lab` validates conditional Smoke routing without delivery
side effects.

## Rollout

1. Validate the project profile and selected Git revision.
2. Preview generation without `--apply`.
3. Apply a new non-default workflow revision.
4. Run a managed-worktree canary.
5. Set the validated revision as project default for new tasks.
6. Let existing tasks finish on their frozen graph.

Do not reconcile semantic graph changes in place after a workflow has task
records. Recreate Backlog tasks in the replacement workflow before retirement;
completed or canceled history may be discarded only with user approval.

## Retirement

Preview with:

```bash
kent workflow delete <bare-workflow-uuid> --json
```

Only the user confirms deletion with `--confirm`. Workflow deletion removes
the definition, links, and task database rows but leaves repositories and
managed worktrees for separate inspection. Never repair workflow state by
editing Kent's database.
