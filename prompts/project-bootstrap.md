# Objective

Prepare the current repository to use the platform-neutral Kent Engineering Kit.

# Discovery

Inspect:

- repository instructions and existing `.kent` files;
- language, build system, package manager, tests, lint, and type checks;
- Git hosting, issue tracker, CI, release process, and deployment target;
- Git default-branch metadata, non-Git workspace needs, and desired workflow
  execution-target policies;
- device or hardware resources that require locking;
- untracked machine configuration required by a fresh worktree;
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

Use the canonical Kent 2.3 execution-target recommendations from the toolkit
workflow contract. Record the selected default and workflow-kind overrides in
the project profile instead of copying the policy into project documentation.

# Setup

After approval, create:

- `.kent/project-contract.md`;
- `.kent/workflow-profile.toml`;
- `.kent/scripts/workflow-verification-dispatch` from the toolkit template;
- deterministic `.kent/scripts/workflow-verify-report`;
- optional idempotent `.kent/worktrees/setup.sh` using the Kent 2.3 payload
  contract;
- optional smoke, resource-lock, PR, and release adapters;
- project-local role implementations using the canonical role keys.

Do not create or link live workflows until the project profile validates.
Preview with `scripts/generate-workflow` before applying a versioned non-default
instance. Require a managed-worktree canary before changing the project default.
