# Workflow Generation

Live Kent workflow definitions are stored in Kent's database. Project JSON files
are audit snapshots.

The generator will create versioned project-local instances from common
fragments rather than sharing one mutable workflow ID across projects.

Planned fragments:

- planning and plan review;
- implementation continuation;
- verification dispatch, read-only branches, Join, and gate;
- PR creation, CI monitoring, waiting for merge, and cleanup;
- single and split release lifecycles;
- recoverable blocker and cancellation transitions.

Appsome and Puber are initial conformance projects. Web, iOS, embedded, and
generic shell profiles follow after the core contracts stabilize.
