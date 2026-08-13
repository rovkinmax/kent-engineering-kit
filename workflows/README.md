# Workflow Generation

Live workflow definitions are stored in Kent. Project JSON files are audit
snapshots, not an alternate source of truth.

## Supported Baseline

- Kent 2.6.1 or newer;
- profile schema 3;
- workflow-wide unique transition keys;
- explicit execution-target policy;
- execution-mode validation before task creation.

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
