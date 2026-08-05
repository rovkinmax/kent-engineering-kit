# Workflow Generation

Live Kent workflow definitions are stored in Kent's database. Project JSON files
are audit snapshots.

The generator creates project-local experimental instances from common
fragments rather than sharing one mutable workflow ID across projects.

Every generated workflow:

- require Kent 2.3 or newer;
- set its execution target with `kent workflow update --execution-target`;
- use the project profile default unless a workflow-kind override exists;
- validate in execution mode before task creation;
- export the live definition as an audit snapshot.

Implemented `Engineering Delivery` fragments:

- optional deterministic Jira/GitHub issue branch identity before Plan;
- planning and plan review;
- implementation continuation with selectable continuous or fresh-per-slice
  writer sessions;
- verification dispatch, read-only branches, Join, and gate;
- bounded Fix continuation through `continue_fix` in fresh-per-slice mode;
- PR creation, CI monitoring, waiting for merge, and cleanup;
- recoverable blocker and cancellation transitions.

`Engineering Canary` reuses the planning, single-writer continuation,
verification fan-out/Join, fix, and cleanup core while intentionally omitting
runtime Smoke, PR creation, CI, and merge waiting.

`Engineering Smoke Lab` keeps conditional Smoke routing while omitting PR, CI,
and merge waiting. Its default name is unversioned; free-form labels create a
new experiment after the current graph has tasks or needs unsupported
structural rewiring.

Planned fragments:

- maintenance and dependency updates;
- intake and diagnosis;
- single and split release lifecycles;
- standalone rebase flows.

Appsome and Puber are the initial conformance projects. Their generated
Delivery v5 instances passed managed-worktree canaries and are now project
defaults. Temporary Canary and Smoke Lab instances were removed after
promotion. Web, iOS, embedded, and generic shell profiles follow after the core
contracts stabilize.

Existing pre-2.3 workflows are not rewritten in place. After the coordinated
upgrade, their preserved Source HEAD policies are inspected and snapshots are
re-exported before an experimental generated replacement is linked.

Kent 2.4 adds `kent workflow delete <bare-workflow-uuid>`. Without `--confirm`
it returns a non-destructive impact preview. After incorporating evidence and
finishing or canceling active work, the user may confirm deletion explicitly.
Kent 2.3 still requires Kent Desktop for retirement. Deletion removes the
workflow definition, project links, and task database rows but retains
repositories and managed worktrees for separate inspection and cleanup. Never
repair workflow state through direct database mutation.

Kent 2.3.0 workflow edit commands use persisted `workflow-...` IDs. Kent 2.3.1
and newer accept bare canonical workflow UUIDs instead. The generator resolves
an exact name through `workflow list` only when discovering an existing graph,
then preserves the ID representation returned by the installed Kent version.

Before changing an existing workflow, the generator computes drift without
mutation. Unsupported extra graph elements fail immediately. Reconciliation
that would change graph semantics is refused when the workflow already has task
records. Compatible taskless changes may reconcile in place; unsupported
structural changes use another experimental label.

`generate-workflow --apply` refuses semantic reconciliation when the existing
Workflow owns any Tasks because CLI edge updates are not atomic. Create a new
version or retire/migrate those Tasks first. When updating a taskless legacy
Workflow whose display name differs from the generated name, pass its canonical
UUID with `--workflow-id`; this prevents accidental creation of a duplicate
Workflow with the same intended version.

Generator prompt changes do not rewrite task-backed live graphs. Delivery v5
remains frozen with its recorded task history. New review-ownership or model
experiments must use a separately validated non-default workflow instance,
then become default only after a managed-worktree canary passes.

Puber Engineering Delivery v6 proved the checkpoint-aware Plan handoff but
over-applied fresh sessions to non-writer approval recovery. It was stopped
after Plan. The corrected replacement keeps fresh sessions only for
Implement/Fix slices and preserves compact-and-continue recovery for Plan,
Smoke, Compliance, PR/CI, and Cleanup.

Puber Engineering Delivery v7 is task-backed and frozen. Its canary proved
fresh Implement/Fix slices, verification fan-out/Join/Gate, runtime Smoke,
Compliance, CI, and PR waiting. It also exposed a PR-policy gap: GitHub reported
the final tree `MERGEABLE/CLEAN` while `canBeRebased=false` because an earlier
commit could not replay. The authorized exact-tree linearization repaired the
branch without changing its tree. The generator now models `auto`, `merge`,
`squash`, and `rebase` explicitly; those semantics belong in a future
non-default workflow, not an in-place v7 mutation. Delivery v5 remains the
project default until a successor is separately canaried and promoted.
