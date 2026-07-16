# Workflow Generation

Live Kent workflow definitions are stored in Kent's database. Project JSON files
are audit snapshots.

The generator creates versioned project-local instances from common
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
device smoke, PR creation, CI, and merge waiting.

Planned fragments:

- maintenance and dependency updates;
- intake and diagnosis;
- single and split release lifecycles;
- standalone smoke and rebase flows.

Appsome and Puber are the initial conformance projects. Their generated
instances are linked non-default pending managed-worktree canaries. Web, iOS,
embedded, and generic shell profiles follow after the core contracts stabilize.

Existing pre-2.3 workflows are not rewritten in place. After the coordinated
upgrade, their preserved Source HEAD policies are inspected and snapshots are
re-exported before a versioned generated replacement is linked.

When a draft version contains stale graph elements that Kent cannot remove
through the CLI, leave it taskless and unlinked, mark it superseded, and create
the next clean version. Never repair it through direct database mutation.

Before changing an existing workflow, the generator computes drift without
mutation. Unsupported extra graph elements fail immediately. Reconciliation
that would change graph semantics is refused when the workflow already has task
records; create a new workflow version instead.
