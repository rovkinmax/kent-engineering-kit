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
instructions and settings. Ordinary continuation and resume retain the current
Session's locked configuration and prompts. Compaction or a workflow context
reset can clear that lock; the next dispatch may load changed instructions and
settings even when the Session ID is unchanged. Before adoption, assess those
refresh boundaries against retained helper versions, carried values and pending
decisions. Do not assume an existing Session permanently isolates old prompts.

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

### Admit a native source ingress

When a reviewed Workflow carries a source-admission frame, read the complete
`{project_id, workflow_id, source_admission}` object from the exact native
ingress edge; do not accept a caller-supplied receipt path or identity. Before
the current Script performs its first provider or storage effect, call the
installed Kit delivery API with the actual execution root and native `_kent`
context:

```python
delivery.admit_script_source_ingress(
    ROOT,
    _kent,
    kent_path=qualified_kent_binary,
)
```

For prompt-free Script targets, create the retained document with
`workflowkit.operations.create_script_ingress_reference` in an existing
private persistent rollout directory. Put its returned canonical reference in
the exact ingress transition group's description and leave the Script edge
prompt absent. The operator and runtime both validate the private document;
never replace it in place. Its retention and exclusive legacy compatibility
rules are defined in the
[update compatibility contract](../contracts/workflow-update-compatibility.md).

Proceed only after verified admission. The closed v2 binding selects exactly
one mode: `r2-installed` verifies the installed binding and preserves existing
R2 task initialization, while `plan-publication` admits only the pinned
published source closure. The latter does not initialize a Task, call
project-memory, perform a revision write, infer legacy storage mode or create a
`.todo` fallback. A source receipt is not human approval or permission for an
external effect. Native identity mismatch, missing source, or changed bytes
must stop before the effect and report the fixed blocker.
This ingress API is only for the Script node targeted by the exact frame;
later Script nodes must use their own qualified native/provenance contracts
and must not pretend that the ingress frame targets them.

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

See [role definitions](../agents/) and
[effective configuration](../config/subagents.toml) for behavior and capabilities.
Supervision runs within a live session; it does not provide an independent
scheduler or observation after the session exits.

## Developing the Kit

### Select source without changing the installed checkout

The [self-development contract](../.kent/project-contract.md) and
[project profile](../.kent/workflow-profile.toml) define the development process.
Use a Kent-managed source worktree; preserve the installed primary checkout,
its local `main`, symlinks, global configuration, and consumer pins.
Follow [global governance](../global/AGENTS.md#authority-and-decisions) before edits.

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
separate [source versus installed state](../.kent/project-contract.md#effects-and-report-only-work)
procedure.

### Verify and deliver

Run affected tests and focused production-shaped wrapper/helper checks from the
selected source worktree. The configured verifier owns one fresh full
`./scripts/validate` run after writer bookkeeping; carry its report/log and
source/environment identity references through the existing handoffs. Source or
environment drift requires fresh verification. Do not use `--installed-state`
for source development.

See [verification ownership](../.kent/project-contract.md#verification-and-evidence)
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
| Runtime evidence and recovery | [Evidence recovery](../contracts/evidence-recovery.md) |
| PR checks and merge handling | [PR contract](../contracts/workflow-contract.md#pr) |
| Worktree ownership and cleanup | [Worktree contract](../contracts/worktree-contract.md), [task-owned cleanup](../contracts/workflow-contract.md#task-owned-cleanup) |
| Release source checks and publication | [Release contract](../contracts/workflow-contract.md#release-sourcepublication), [source bindings](../contracts/release-source-bindings.md) |
| Model, tool and delegation settings | [Managed configuration](../config/subagents.toml) |

### Content-policy publication candidates (not installation)

Use the qualified Kit Python API, not a new CLI or installer framework:

- `operations.prepare_runtime_publication_v3(...)` prepares one closed runtime-v3
  receipt candidate from exact installed predecessor binding and receipt refs.
- `operations.prepare_source_publication_v2(...)` prepares source-only-v2 receipt
  and provenance candidates from a closed canonical owned predecessor capture
  and its exact receipt ref. `additional_published_paths` contains additions
  only, as root/relative-path records. Retained extras and permissions remain.

Both APIs require named explicit project/root/workspace/commit/publication-ref/
CI-helper/helper-declaration inputs. The executing producer must be the selected
clean exact-pinned Kit. Old checkout ancestry or successful old admission is not
required. New publication inventory comes from immutable Git blobs, with safe
actual filesystem permissions; do not chmod copies or refresh a receipt after
intended drift. Existing publication refs are read, not fetched or rewritten.

Provide absent output paths in existing owned mode0700 directories. Candidates
are exclusively created mode0600, fsynced, read back and freshness-checked.
Preserve a partial artifact as **unadmitted** after failure; do not overwrite,
install or silently retry it. The returned path/hash/size references prove
candidate integrity only, not native ownership, current approval or activation.
Source-only capture integrity likewise does not authenticate a native preimage.

For a source-only content-policy introduction, carry the typed provenance packet
as the single evidence reference of review disposition
`source_admission.publication_provenance`. Canonical preparation/new-effect gates
bind it to the original owned plan preimage and candidate target; later stages
compare live state to their own expected before-state. A different capture/plan
requires fresh preparation/review, even if candidate bytes happen to match.
Unchanged complete installed carriers may be reused by a subsequent independent
plan without rerunning the producer. Read-only settlement can still classify an
attempted effect when a packet disappears; the next new effect remains blocked.

See `contracts/workflow-update-compatibility.md` for exact policy/version,
role/projection, freshness, extra-coverage and provenance rules. Old runtime-v1/
v2 and source-only-v1 receipts keep their original ancestry checks. The new
source-only receipt never authorizes R2 storage. Source success does not install
new readers, refresh retained frames, change global aliases or prove a consumer
cohort compatible. Publication/adoption and forward-qualified rollback are
separate decisions; restarting Kent is not a substitute for compatibility.
