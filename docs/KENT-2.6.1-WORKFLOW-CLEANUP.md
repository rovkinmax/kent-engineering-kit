# Kent 2.6.1 Workflow Validation And Cleanup

Execution date: 2026-08-13

## Approved Decision

Validate compatible workflows in place. Do not create a new workflow UUID
when the complete Kent 2.6.1 graph plan is semantically unchanged. A
task-backed workflow remains frozen: an unchanged graph apply is allowed for
transport verification, while any semantic delta requires a new revision.

After validation, retire completed Canary workflows and obsolete workflow
revisions only when a fresh deletion preview confirms the approved impact.
Preserve active Appsome Delivery revisions and every current task/worktree.

## Checklist

- [x] Inventory every linked workflow and related unlinked obsolete candidate.
- [x] Run complete graph inspect/plan/apply verification for every retained
      workflow; require an unchanged graph result and stable version.
- [x] Run execution validation for every retained workflow.
- [x] Archive retirement snapshots and append deletion evidence.
- [x] Re-verify Puber backlog replacement equivalence.
- [x] Recreate Appsome v24 Backlog tasks losslessly on v25.
- [x] Preview and serially retire approved Canary and obsolete workflows.
- [x] Remove retired working snapshots and stale Canary branch state.
- [x] Update project workflow indexes, export tooling, and shared roadmap.
- [x] Verify defaults, retained tasks, workflow links, worktrees, Git diffs,
      and the full Engineering Kit validation suite.
