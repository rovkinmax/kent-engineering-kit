# Objective

Prepare the current repository to use the platform-neutral Kent Engineering Kit.

# Discovery

Inspect repository instructions and existing `.kent` configuration; toolchain,
tests and architecture; source control, issue tracker, CI and release process;
worktree namespace and execution targets; shared resources; machine-local
bootstrap needs; credential/evidence policy; and effective roles.

# Proposal

Recommend the smallest suitable profile:

- `lite`: plan, implement, verify, done;
- `standard`: plan review, verification fan-out and optional PR;
- `team`: approvals, independent reviews, PR/CI/waiting/cleanup;
- `release`: gated release effects and monitoring.

Select only needed adapters using the [adapter guide](../adapters/README.md)
and [profile example](../contracts/project-profile.example.toml). Projects own
URLs, credentials, platform commands and procedure adaptation. Ask only for
material choices; do not invent new integration documentation.

Check actual supported Kent capabilities. Declare default/per-kind execution
targets in the profile using the
[workflow contract](../contracts/workflow-contract.md#execution-targets).

# Setup

After applicable approval, create project contract/profile, context manifests,
verification dispatch/report commands, work-kind procedures and needed
adapters. Optional setup hooks use the
[worktree contract](../contracts/worktree-contract.md#setup-hook).
Project runtime procedures conditionally load their applicable platform/login
contracts and provide safe targeting, resource, evidence and recovery seams.

Validate profile and executable closure before live creation/linking. Generate
and inspect a complete local semantic preview: graph apply mutates state and
is not a dry-run. Follow
[update compatibility](../contracts/workflow-update-compatibility.md) for
existing graphs. An approved new revision is applied non-default first,
validated and exercised with a managed-worktree canary before promotion.
Source edits alone do not activate workflows or refresh retained Sessions.
