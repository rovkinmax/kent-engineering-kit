# Kit operations

Practical setup and maintenance for the Kent Engineering Kit.
For an overview and examples, start with the [README](../README.md).
Commands below run from the Kit checkout unless stated otherwise.

- [Install the Kit](#install-the-kit)
- [Connect a project](#connect-a-project)
- [Use specialist roles](#use-specialist-roles)
- [Developing the Kit](#developing-the-kit)
- [Find an operational contract](#find-an-operational-contract)

This guide explains the routes through the tooling. Linked project profiles,
procedures, and contracts own the detailed requirements; this page does not
grant permission to install, activate, publish, or delete anything.

## Install the Kit

### Before installation

Keep the repository in a stable location: the installer links directly to this
checkout, rather than copying a self-contained distribution. Moving it can break
the links; editing linked files can change what new Kent sessions load.
Do source development in a separate managed worktree.

You need Kent and the tools required by your chosen project. The Kit's Python
tooling requires Python 3.11 or newer. Source validation also uses Bash, Git,
and Ruby with YAML/JSON support. Consult the selected project's
`minimum_kent_version` and the [workflow contract](../contracts/workflow-contract.md)
for Kent compatibility; keep the CLI, service, and Desktop compatible with one
another. This guide does not prescribe a universal Kent release.

Inspect [the installer](../scripts/install) and
[the managed role configuration](../config/subagents.toml) before adopting them.
The Kit includes global engineering instructions, not just optional commands.

### Install files, then configure roles

When ready to change your local Kent installation:

```sh
./scripts/install
```

The installer creates links for global instructions, skills, prompts, agents,
helpers, and hooks under `~/.kent` (or `KENT_PERSISTENCE_ROOT` when set).
It refuses conflicting files or foreign symlinks. A byte-identical existing file
is backed up with a `.pre-kent-engineering-kit` suffix before becoming a link.
Installation is not transactional: a later conflict does not undo earlier links.
Inspect a reported conflict before retrying; do not remove user files to force
installation.

Next, merge the managed settings in [config/subagents.toml](../config/subagents.toml)
into your Kent `config.toml`, preserving unrelated settings. The installer does
not perform this merge. Role prompts define behavior; configuration selects
models, reasoning settings, tools, and delegation eligibility.

Coordinate a Kent restart and reopen Desktop after global configuration changes;
do not interrupt other active work. Use new sessions to load the adopted
instructions and settings. Existing sessions retain their locked configuration
and prompts.

File installation and role configuration do **not** create, activate, or change a
project's workflows. Installed hooks remain opt-in through project configuration.

### Optional integrations and validation

For projects using the portable Mobile MCP integration, configure and audit it
separately:

```sh
./scripts/configure-mcporter --apply
./scripts/audit-mobile-schema
```

These are not universal installation steps. The first changes the user's
mcporter catalog; projects still own credentials and server-specific wrappers.
See the [adapter guide](../adapters/README.md) for MCP, Gradle, Jira, Sentry,
and Mobile integration details, including sensitive-output handling.

To check repository sources without inspecting the installed configuration:

```sh
./scripts/validate
```

Checking the actual installation is a separate, explicitly authorized mode:
`./scripts/validate --installed-state`. Passing source checks alone does not
prove that installed configuration or live workflows match the checkout.

## Connect a project

Start a Kent session in the target repository and use
[project bootstrap](../prompts/project-bootstrap.md) to inspect its tools and
propose a setup. Choose a delivery profile based on the work you actually need;
not every project needs PR automation, runtime checks, or release stages.

After approval, the project supplies:

- `.kent/workflow-profile.toml`: selected capabilities, commands, and policies;
- `.kent/project-contract.md`: project-specific responsibilities and boundaries;
- executable verification commands and procedures for implementation and delivery;
- context manifests describing what each workflow stage needs to read;
- any required platform adapters, resource handling, and credential references.

The [example profile](../contracts/project-profile.example.toml) and
[project adapter boundary](../contracts/workflow-contract.md#project-adapter-boundary)
describe these interfaces. Keep private runtime state out of Git using
`/.kent/runtime/`. Do not put credentials in profiles, fixtures, or reports.

### Preview before activation

Once the project profile and its required files are ready, generate a local
preview:

```sh
./scripts/generate-workflow \
  --project /path/to/project \
  --kind delivery \
  --version 1
```

Here `--version` identifies the generated workflow, not the Kent release.
Without `--apply`, this command prints the graph and does not install it.
`--apply` creates or updates and links live workflow state; it is not a dry-run.
Changing the project default additionally requires `--set-default`.

Review the graph and follow the
[workflow update compatibility policy](../contracts/workflow-update-compatibility.md)
before a separately authorized activation. Validate a new non-default candidate
with a managed-worktree canary before promoting it to the project default.
Changing source files does not update existing live workflows.

### Check the revision that will execute

A workflow can exist while the selected branch is missing its project scripts.
Check the exact selected revision before starting work:

```sh
./scripts/preflight-revision \
  --project /path/to/project \
  --ref feature/my-change
```

Preflight reads the required files from Git objects, without switching branches
or creating a worktree. The installed entry point is
`~/.kent/bin/kent-preflight-revision`. Its launcher selects a compatible Python;
`KENT_ENGINEERING_KIT_PYTHON` can explicitly select an executable.

Adapter synchronization is a separate operation:
`./scripts/sync-project-adapters --project /path/to/project`.
It can write Kit-managed adapters and commands, so use it only for the approved
project setup. Project-owned executable adapters are validated, not overwritten.

## Use specialist roles

With files and configuration adopted, run these commands from the repository
you want to work on. Start an interactive specialist session with
`kent --agent <role>`, or submit a focused request with
`kent run --agent <role> '<request>'`.

- **architecture-designer** compares module boundaries and design alternatives.
- **grill** challenges assumptions and unresolved decisions without editing code.
- **researcher** investigates questions using primary sources.
- **build-doctor** diagnoses build and test failures, with bounded fixes when authorized.
- **task-supervisor** accompanies the Tasks and projects you select.

See [role definitions](../agents/) for each role's limits and
[the role contract](../contracts/role-contract.md) for shared responsibilities.
Supervision runs within a live session; it does not provide an independent
scheduler or observation after the session exits.

### Cross-session critique and supervision

Give grill the exact Session ID when asking it to critique ongoing work.
Active feedback uses `kent run steer`; continuation has a stricter boundary.
Grill may attempt one continuation of a verified idle, ordinary non-Workflow
Session if either (a) the previous run completed normally, or (b) the previous
run was manually stopped by the user, canceled, or interrupted and a new explicit
human decision naming that exact Session was made after that outcome.
Unknown outcomes or unverified fresh authority block continuation;
an ambiguous attempt is not retried.

The [grill prompt](../prompts/grill.md) and
[Session communication contract](../contracts/role-contract.md#session-communication)
own the complete rules. Task supervision has its own
[role](../agents/task-supervisor.md) and
[operating reference](../skills/kent-engineering-kit/references/task-supervision-operations.md).
Do not use ordinary Session continuation to resume a Workflow-owned Session.

## Developing the Kit

### Select source without changing the installed checkout

The [self-development contract](../.kent/project-contract.md) and
[project profile](../.kent/workflow-profile.toml) define the development process.
Use a Kent-managed source worktree; preserve the installed primary checkout,
its local `main`, symlinks, global configuration, and consumer pins.
Follow the [Kit change process](../contracts/kit-change-governance.md) before edits.

Before starting a Task from the default branch, explicitly refresh and inspect
the source selection:

```sh
git fetch origin
git symbolic-ref refs/remotes/origin/HEAD
git rev-parse refs/remotes/origin/HEAD
```

Kent's `default-branch` policy resolves local tracking refs and locks the selected
commit; it does not implicitly fetch. Verify the complete `.kent` profile,
procedures, and executable command closure at that exact commit using revision
preflight. A first Task Script cannot repair stale source selection.

After an approved delivery is merged, refresh remote tracking and repeat
preflight before the next Task. Do not check out or fast-forward installed local
`main` as part of ordinary source development. Installed adoption follows the
separate [source versus installed state](../.kent/project-contract.md#source-versus-installed-state)
procedure.

### Verify and deliver

Run affected tests and focused production-shaped wrapper/helper checks from the
selected source worktree. The configured verifier owns one fresh full
`./scripts/validate` run after writer bookkeeping; carry its report/log and
source/environment identity references through the existing handoffs. Source or
environment drift requires fresh verification. Do not use `--installed-state`
for source development.

See [verification ownership](../.kent/project-contract.md#source-verification-ownership-and-identity)
for the exact report and identity requirements.
Commit, push, PR creation, merge, installation, and live workflow changes retain
their separate authorization boundaries. A merged source PR is not evidence of
installed adoption or successful runtime qualification.

## Find an operational contract

Use these sources for advanced operations instead of treating historical rollout
notes as current installation state.

| Topic | Owning reference |
| --- | --- |
| Workflow lifecycle, profile policies, and execution targets | [Workflow contract](../contracts/workflow-contract.md) |
| Changes to existing workflows | [Update compatibility](../contracts/workflow-update-compatibility.md) |
| Accepted plans and revalidation | [Plan contract](../contracts/plan-contract.md) |
| Runtime evidence and recovery | [Evidence recovery](../contracts/evidence-recovery.md), [sealed recovery](../contracts/sealed-recovery.md) |
| PR checks and merge handling | [PR contract](../contracts/workflow-contract.md#pr) |
| Worktree ownership and cleanup | [Worktree contract](../contracts/worktree-contract.md), [task-owned cleanup](../contracts/workflow-contract.md#task-owned-cleanup) |
| Release source checks and publication | [Release contract](../contracts/workflow-contract.md#release-sourcepublication), [source bindings](../contracts/release-source-bindings.md) |
| Retirement and portfolio operations | [Kit operational tooling](../skills/kent-engineering-kit/SKILL.md) |
| Upgrade planning and recovery | [Upgrade runbook](KENT-UPGRADE-RUNBOOK.md) |
| Model allocation and historical adoption records | [Model policy](MODEL-POLICY.md) |

[The roadmap](ROADMAP.md) records planning and rollout history. It is not proof
of the current installed or live state.
