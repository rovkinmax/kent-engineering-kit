# Objective

Prepare the current repository to use the platform-neutral Kent Engineering Kit.

# Discovery

Inspect:

- repository instructions and existing `.kent` files;
- language, build system, package manager, tests, lint, and type checks;
- Git hosting, issue tracker, CI, release process, and deployment target;
- device or hardware resources that require locking;
- existing feature/spec/plan artifact conventions;
- current Kent workflows and roles.

# Proposal

Recommend the smallest suitable delivery profile:

- `lite`: plan, implement, verify, done;
- `standard`: plan review, implementation, verification fan-out, optional PR;
- `team`: approvals, independent reviews, PR/CI/waiting/cleanup;
- `release`: destructive release gates and monitoring.

Identify platform adapters such as Android, Web, iOS, embedded, or generic
shell commands. Ask the user to decide only material choices.

# Setup

After approval, create:

- `.kent/project-contract.md`;
- `.kent/workflow-profile.toml`;
- deterministic `.kent/scripts/workflow-verify`;
- optional smoke, resource-lock, PR, and release adapters;
- project-local role implementations using the canonical role keys.

Do not create or link live workflows until the project profile validates.
