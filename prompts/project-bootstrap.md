# Objective

Prepare the current repository to use the platform-neutral Kent Engineering Kit.

# Discovery

Inspect repository instructions and existing `.kent` configuration; toolchain,
tests and architecture; source control, issue tracker, CI and release process;
worktree namespace and execution targets; shared resources; machine-local
bootstrap needs; credential/evidence policy; and effective roles.

# Proposal

For a new project, recommend the ready `engineering-lite-v1` Kit package:
eleven fixed nodes with independent immutable-plan review, one writer,
verification, independent candidate review, checked PR/CI and Agent Cleanup.
Projects adapt product commands and effect limits through
`.kent/context/engineering-lite.md`, `.kent/commands/delivery-lite.md`,
`.kent/commands/cleanup-lite.md` and their project contract; they do not build
a custom Lite graph or mutate roles. Inspect actual procedures and approval
boundaries. If the fixed package cannot meet requirements, ask for that
material decision instead of growing the Lite template.

The schema-3 profile generator is an explicit compatibility route for projects
already selecting its Script acceptance/ledger/checkpoint/Janitor contracts.
Do not label its `lite` profile as the ready modern Agent-owned Lite package.

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

Validate profile and executable closure before live creation/linking. Use `scripts/install workflow preview --project <root> --template engineering-lite-v1`
for the ready package; inspect its exact bytes/hash and complete graph. For an
explicit compatibility route, generate its complete semantic preview: graph apply mutates state and
is not a dry-run. Follow
[update compatibility](../contracts/workflow-update-compatibility.md) for
existing graphs. An approved new revision is applied non-default first,
validated and exercised with a managed-worktree canary before promotion.
Source edits alone do not activate workflows or refresh retained Sessions.

## Fresh R2 installation

For a new R2-only project, prepare and publish its approved profile and helper
closure, and create/link the approved non-default Workflow before installation.
Keep native Task creation paused: the fresh installer requires zero Tasks and
does not adopt or import existing ones. Do not create a dummy legacy Task.

Use the existing Kit installer project submode described in
[operations](../docs/OPERATIONS.md#install-a-fresh-r2-project): read-only
`scripts/install project preview`, review its exact canonical plan/digest and
effects, then separately authorized `project apply`. It initializes the empty
project store, publishes the first receipt and publishes the binding last.
This registers ordinary R2 storage, not a fully source-admitted runtime.
Verify the published storage adapter/core and actual native/profile/namespace
identity; observe a retained CI alias without changing it. Existing strict
source/CI gates remain effective if selected by the Workflow.

The installer does not select a default Workflow, create/initialize a Task,
qualify ingress, update global aliases or refresh instructions. Separately
qualify the actual approved non-default Plan/independent review/Implement/Verify/
PR-CI/Cleanup route and managed-worktree access before promotion or feature
tasks. Follow the existing graph approval/compatibility route.

For an existing registered R2 project, preserve its binding/receipt and Tasks.
Do not rerun the fresh zero-Task installer. The ready Lite installer creates
one new non-default Workflow only; it does not update/migrate old Workflows,
promote defaults or refresh retained Sessions. Existing-task recovery requires
its own compatibility preview and effect approval.
