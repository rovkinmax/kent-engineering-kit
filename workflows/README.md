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

- planning and plan review;
- implementation continuation;
- verification dispatch, read-only branches, Join, and gate;
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

Kent 2.3 has no CLI workflow-delete command. Retire superseded experiments
through Kent Desktop after incorporating their evidence and finishing or
canceling active work. Desktop deletion removes the workflow definition,
project links, and task database rows but retains repositories and managed
worktrees for separate inspection and cleanup. Never repair workflow state
through direct database mutation.

Before changing an existing workflow, the generator computes drift without
mutation. Unsupported extra graph elements fail immediately. Reconciliation
that would change graph semantics is refused when the workflow already has task
records. Compatible taskless changes may reconcile in place; unsupported
structural changes use another experimental label.
