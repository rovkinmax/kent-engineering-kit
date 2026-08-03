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
- runtime evidence privacy, redaction, and retention requirements;
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
- optional smoke, resource-lock, evidence-audit, PR, and release adapters;
- project-local role implementations using the canonical role keys.

For projects that use Jira as a planning source, declare `jira_api` in
`required_adapters`, configure `[integrations.jira]` with the tenant URL and
credential namespace or 1Password pointers, and synchronize it with
`scripts/sync-project-adapters`. Keep issue ingestion policy in the project
skill or Plan procedure; the shared adapter is read-only.

For Android projects with conditional or required Smoke, declare
`mobile_resource_lock` and `mobile_evidence_audit` in `required_adapters` and
`[adapters]`, synchronize them with `scripts/sync-project-adapters`, and keep
device selection plus APK/package details in the project procedure. The
procedure must reject unfiltered logs and unexpected sensitive UI evidence.

Do not create or link live workflows until the project profile validates.
Preview with `scripts/generate-workflow` before applying a versioned non-default
instance. Require a managed-worktree canary before changing the project default.
