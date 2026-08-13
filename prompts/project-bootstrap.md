# Objective

Prepare the current repository to use the platform-neutral Kent Engineering Kit.

# Discovery

Inspect:

- repository instructions and existing `.kent` files;
- language, build system, package manager, tests, lint, and type checks;
- Git hosting, issue tracker, CI, release process, and deployment target;
- Git default-branch metadata, non-Git workspace needs, configured
  `worktrees.base_dir`, source-workspace overlap risks, and desired workflow
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

Use the canonical Kent 2.6.1 execution-target recommendations from the toolkit
workflow contract. Kent CLI/TUI, service, and Desktop must be the same approved
version. Record the selected default and workflow-kind overrides in
the project profile instead of copying the policy into project documentation.

# Setup

After approval, create:

- `.kent/project-contract.md`;
- `.kent/workflow-profile.toml`;
- `.kent/scripts/workflow-verification-dispatch` from the toolkit template;
- deterministic `.kent/scripts/workflow-verify-report`;
- optional idempotent `.kent/worktrees/setup.sh` using the Kent setup payload
  contract;
- optional smoke, resource-lock, evidence-audit, PR, and release adapters;
- project-local role implementations using the canonical role keys.

For projects that use Jira as a planning source, declare `jira_api` in
`required_adapters`. Add it to `kit_managed_adapters` only when the project uses
the shared safe template; an extended project adapter remains
project-owned. Configure `[integrations.jira]` with the tenant URL and
credential namespace or 1Password pointers. Keep issue ingestion policy in the
project skill or Plan procedure, and keep mutation approval plus language
policy in the project contract.

For projects that ingest Sentry issue context, declare `sentry_issues` in both
adapter lists, map the synchronized adapter, and configure
`[integrations.sentry]` with only the base URL, organization, project, and
credential namespace. Keep tokens and 1Password item names in environment or a
machine-local credential reference. Define when exact issues may be marked
seen and which approval gates own resolve or mute.

For Android projects with conditional or required Smoke, declare
`mobile_resource_lock` and `mobile_evidence_audit` in `required_adapters` and
`kit_managed_adapters`, map them in `[adapters]`, synchronize them with
`scripts/sync-project-adapters`, and keep device selection plus APK/package
details in the project procedure. The procedure must reject unfiltered logs
and unexpected sensitive UI evidence.

Do not create or link live workflows until the project profile validates.
For Kent 2.6 graph work, inspect the complete graph and compute the kit-owned
local semantic preview before graph apply. Do not treat graph apply as a
dry-run: it saves non-destructive changes and pauses only for destructive
confirmation. Apply a versioned workflow non-default first, then validate and require a
managed-worktree canary before changing the project default. Any workflow with
Tasks is a frozen revision; create a new version rather than editing it in
place. Task Start/Move/Resume may receive an explicit `--branch-name` when the
initial managed-worktree branch must differ from the task short ID.
