# Workflow Generation

Live workflow definitions are stored in Kent. Project JSON files are audit
snapshots, not an alternate source of truth.

Runtime v2 is selected only by the schema-4 `runtime_contracts@2.0.0`
adoption discriminator. It adds the validated `pr_feedback_cursor` carrier to
existing delivery edges without adding nodes or changing transition ownership.
Legacy schema-3 and schema-4 workflow output remains v1-compatible.

Release authority template schema 2 is a separate release-spec boundary. The
adapter must capture selected-revision inputs, current Kent or GitHub
execution, and raw external-root bytes in one process, then bind the observed
concrete authority to that sealed context. Do not derive runtime values from a
tracked template, prior run, operation mapping, or JSON round-trip. The
release resolver accepts only the sealed proof chain and the operation's exact
source-envelope digest; it materializes the unchanged concrete authority into
the existing publication operation.

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
`puber-release/managed-in-place`,
`sdk-merged-main-publication/managed-in-place` or
`sdk-merged-main-publication/metadata-only`, and
`slack-reader-release/managed-in-place`. Managed-in-place requires a builder
and graph-bearing workflow intent; metadata-only forbids a builder and requires
metadata-only workflow intent. Synchronization, migration, generation, apply,
and activation remain non-automatic.
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

Workflow update eligibility follows
[Execution-history and compatibility policy](../contracts/workflow-contract.md#execution-history-and-compatibility-policy).
Source generation does not mutate live workflows. For a separately approved
new-revision rollout:

1. Validate the project profile and selected Git revision.
2. Preview generation without `--apply`.
3. Apply a new non-default workflow revision.
4. Run a managed-worktree canary.
5. Set the validated revision as project default for new tasks.
6. Let existing tasks finish on the retained revision under that rollout scope.

The generic client does not support task-referenced or linked semantic updates;
use a separately approved lifecycle operation under the canonical policy.
Retirement has separate stricter gates: recreate Backlog tasks in the
replacement workflow before retirement; completed or canceled history may be
discarded only with user approval.

## Retirement

Preview with:

```bash
kent workflow delete <bare-workflow-uuid> --json
```

Only the user confirms deletion with `--confirm`. Workflow deletion removes
the definition, links, and task database rows but leaves repositories and
managed worktrees for separate inspection. Never repair workflow state by
editing Kent's database.

## Portfolio retirement and canonical reconciliation

The Kit also provides the separate `release-live-portfolio-plan-v1` operation
for one approved cross-project envelope. It owns the fixed journal
`<state_dir>/release-live-portfolio.journal.json` and the ordered
`preview -> prepare -> retire -> apply -> complete` sequence. The plan binds
six retirement Workflows and four protected canonical Workflows, their exact
projects, terminal Tasks, Session manifests, worktrees, resources, links,
defaults, revisions, and target graphs/metadata.

`prepare` is a separately confirmed durable local journal mutation: it requires
exact `--confirm` equal to the plan SHA-256 before the fixed pre-D9 receipt and
performs no D9 or Workflow effect. `retire` uses only the
fixed `workflow delete <uuid> --confirm --json` effect and has no rollback after
the first delete. `apply` revalidates the complete D9 poststate and every
canonical pre/post stage before each effect. Canonical rollback is a confirmed
forward restore inside the same operation; completed operations require a new
restore plan. The existing single-project retirement and canonical CLIs remain
v1-compatible and are not replaced by this operation.
