# Kent Engineering Kit

Personal, platform-neutral engineering workflows for Kent.

The kit separates:

- global engineering disciplines in `skills/`;
- explicit user-driven flows in `prompts/`;
- reusable operational and read-only role fallbacks in `agents/`;
- opt-in platform and toolchain behavior in `adapters/`;
- workflow and project adapter contracts in `contracts/`;
- workflow generation in `workflowkit/` and `scripts/generate-workflow`.

Project repositories remain responsible for architecture rules, build commands,
device details, release policy, and integration credentials.

## Developing the Kit itself

The checkout-local `.kent/workflow-profile.toml` and `.kent/project-contract.md`
define the current **Kit Engineering Delivery v4** source candidate. The
project builder is `.kent/workflows/kit_development.py`; the generated
`.kent/workflows/kit-engineering-delivery-v4.spec.json` is a semantic audit
input, not evidence of a live workflow installation. The v1, v2, and v3
snapshots remain immutable historical evidence. This source-only change does
not modify the existing task-backed Workflow. Any future rollout of v4 needs
separate approval, a new Workflow UUID, and fresh qualification; existing
failed qualification records and their task-backed graph must not be repaired
or reused as successful evidence.

This schema-3 development-only flow reuses the lite delivery graph with a
continuous writer: Plan, two independent preview reviews, human approval,
Implement, local verification and Standards review, PR delivery, and owned
Cleanup/Janitor. There is no separate CI stage, Smoke, publication, or final
Compliance layer. Existing GitHub checks and merge policy remain in force.
Run affected tests and focused production-shaped wrapper/helper checks from
the selected source worktree. The configured verifier owns one fresh full
`./scripts/validate` run after writer bookkeeping; carry its report/log and
source/environment identity references through existing handoffs. Any
identity or source drift requires fresh verification. Do not use
`--installed-state` for source development. The authoritative ownership and
identity limits are in `.kent/project-contract.md`; Python 3.11+ must already
exist.

Source development is separate from installed Kit adoption. The installed
primary checkout remains clean on
`9363fa48f9f21d2742a41841ab97b18cc4c4e521`; preserve its index, local `main`,
symlinks, global configuration, consumer pins and workflows. Do not run the
installation instructions below as part of self-development.

Before starting a future Task, explicitly acquire the approved source with
`git fetch origin`, inspect `git symbolic-ref refs/remotes/origin/HEAD` and
`git rev-parse refs/remotes/origin/HEAD`, and verify the complete `.kent`
profile, procedures and executable command closure at that exact commit.
Kent's `default-branch` policy resolves **local tracking refs**, locks the
commit before execution and does not implicitly fetch. A first Task Script
cannot repair stale source selection. Use normal Kent-managed worktrees;
do not create a second source workspace or change setup hooks.

After an approved merged delivery, explicitly refresh remote tracking and
repeat that preflight before another Task starts. Do not check out or
fast-forward installed local `main`. Bootstrap qualification instead selects
the exact published candidate SHA under separate effect approval. Keep its
published branch intact through qualification; report-only cleanup requires
exact HEAD equality with a current published branch tip, not mere ancestry.

Source approval permits only its named source changes and local checks.
Commit/push/PR, live create/apply/link/default, qualification Task execution,
consumer rollout and installed adoption each need their applicable explicit
authority. Future authorized delivery commits and pushes the task branch
only; never push directly to `main` or merge the PR. Cleanup runs through the
existing post-session Janitor, and acceptance checks actual worktree-path and
Git absence plus an absent Kent record or the exact original ID/root read back
as missing retained restorative metadata, not just Task Done.

## Runtime contract v2

Schema-4 projects adopt runtime v2 atomically through
`runtime_contracts@2.0.0`. The conditional `verify`, evidence, janitor, GitHub
CI, and GitHub PR commands must use the same version and support-module parent
directory. Partial or mixed adoption is rejected. Terminal evidence is sealed
under a stable lock; verification output is bounded and content-addressed; raw
child output never becomes transition authority.

Release schema 2 adds runtime-bound authority templates without changing the
persisted publication operation schema. Tracked Kent templates fix workflow
identity and approval policy; tracked GitHub templates fix workflow identity
and a closed exact or project-field ref policy. Adapters capture the current
execution context and external-root source envelope in same-process sealed
proof objects. Canonicalization rejects serialized, foreign-module, stale, or
authority-substituted proof chains.

## Advisory effect steps

Effect-job contracts remain strict by default: a step with
`continue_on_error = true` is rejected unless the matching exact contract step
also declares the sparse overlay `advisory_effect = true`. The overlay is
retained only in the contract step projection; the normalized workflow source
continues to represent the exact source field unchanged.

An advisory effect step must not be `validation_required`. Required and
qualification jobs may not declare the overlay and continue to reject every
failure-masking step. Job-level `continue_on_error` remains forbidden.
Successful job completion is not evidence that an advisory effect occurred.
`allowed_effects` remains a permission boundary, not a claim that every
allowed effect was performed.

`./scripts/validate` is source-only by default. Installed-state checks and
mcporter configuration checks require the explicit
`./scripts/validate --installed-state` mode.

Role behavior and execution policy are separate contracts. Role prompts must
not declare `model` or `tools`; global or project Kent configuration owns
model, reasoning, verbosity, tool availability, and delegation eligibility. See
`contracts/role-contract.md`. The current source role allocation and retained
historical policy/adoption records are documented in `docs/MODEL-POLICY.md`;
they do not attest installed configuration or runtime state.

## Compatibility

- The approved generated-workflow baseline is **Kent 2.6.1**, released August
  13, 2026. Kent 2.6.0 was released August 12, 2026; do not mix those dates
  or run a mixed CLI/TUI, service, and Desktop version set.
- Project profiles declare and enforce their exact minimum Kent version.
- Kent CLI/TUI, service, and Desktop must be upgraded together when crossing
  protocol boundaries.
- Workflow graph inspection and editing use the Kent 2.6 graph document
  contract: `kent workflow graph inspect <uuid>` exports the complete graph,
  while the kit computes the required local semantic preview. Then
  `kent workflow graph apply <path|->` validates and atomically saves the
  document. Graph apply is not a general dry-run: without `--confirm` it saves
  non-destructive changes and pauses only for destructive impact.
- Workflow update eligibility follows
  [Execution-history and compatibility policy](contracts/workflow-contract.md#execution-history-and-compatibility-policy).
  The generic client does not support task-referenced or linked semantic
  updates; those require a separately approved lifecycle operation.
- Kent 2.5's workflow-wide transition-key and offset-pagination contracts
  remain compatibility facts for existing data; the generator uses canonical
  workflow UUIDs and source-qualified transition keys.
- `kent task watch`/`wait` and `kent run watch`/`wait` provide scriptable,
  long-running observation without polling model sessions. `kent question` and
  `kent question answer` inspect and answer pending Task or Session questions
  and approvals.
- `kent task start`, `move`, and `resume` accept `--branch-name` for an
  explicit initial managed-worktree branch. The task short ID remains the
  lifecycle identity; Git branch identity is separate and must be reported
  exactly.
- Managed worktrees must remain inside Kent's configured `worktrees.base_dir`
  and must not overlap their source Workspace. Worktree setup failures preserve
  actionable recovery choices rather than silently discarding the retained
  target or worktree.
- Script failures preserve stderr diagnostics and leave invalid or unavailable
  scripts as resumable interrupted work. Resume is asynchronous: re-read Task
  state and recover through the smallest valid workflow entry, not by assuming
  that a successful command means a Session or Script started.
- Workflow deletion remains an explicit user action.
- Existing workflows retain their recorded execution-target and graph
  behavior after an upgrade. New generated workflows declare their
  execution-target policy explicitly.
- Upgrade and active-task recovery procedure:
  `docs/KENT-UPGRADE-RUNBOOK.md`.

## Installation

Run:

```bash
./scripts/install
./scripts/configure-mcporter --apply
./scripts/audit-mobile-schema
./scripts/validate
```

The installer creates additive symlinks under `~/.kent`. It refuses to replace
non-matching files. A byte-identical legacy file is adopted safely: the
original is retained with a `.pre-kent-engineering-kit` suffix and the managed
path becomes a symlink to the kit.

Platform adapters are installed under `~/.kent/hooks` but remain inactive until
a project explicitly selects them.

Cross-session managed-worktree operations use
`~/.kent/bin/kent-worktree <command> --session <id> ...`. The installed wrapper
removes inherited Kent execution context before delegating to the native CLI.
Revision checks are available from any project through
`~/.kent/bin/kent-preflight-revision`.
The launcher selects Python 3.11 or newer even when Kent's inherited `PATH`
points at an older interpreter. Set `KENT_ENGINEERING_KIT_PYTHON` to override
the selected runtime explicitly; invalid or incompatible overrides fail fast.

MCP calls are available from every project through
`~/.kent/bin/kent-mcp-call` and `~/.kent/bin/kent-mcp-list`. The adapter owns
safe config resolution, mutation approval, metadata call logging, schema
caching, and worktree-aware project identity. It does not create a separate raw
artifact by default, but normal stdout is retained by Kent's shell transcript.
Sensitive calls use `--quiet`, `--digest-output`, or output assertions so raw
responses never reach shell output. Explicit `--save-raw` or `--raw-dir`
retention is reserved for known-safe evidence. Projects still own server
credentials and project-specific stdio wrappers.
`configure-mcporter` adds the portable `mobile` server to the user's mcporter
catalog without replacing unrelated entries.

GitHub merge policy is resolved deterministically through
`~/.kent/bin/kent-resolve-github-merge-strategy`. It consumes repository merge
capabilities, target-branch protection, applicable rulesets, and merge-queue
policy, then returns either one resolved method or a structured
`needs_user_action` result. Workflow agents do not guess between remaining
methods.

`config/subagents.toml` is the authoritative managed config fragment. Merge it
into `~/.kent/config.toml` before restarting Kent. Only
`./scripts/validate --installed-state` compares managed fields against the
effective global configuration; the default invocation is source-only.
Installed-state verification requires separate explicit authorization. The
installer intentionally does not rewrite user configuration.

The global baseline includes bounded implementation, build diagnosis, evidence
gating, runtime Smoke, release lifecycle operations, PR/cleanup delivery, CI
monitoring, research, architecture, and independent review roles. Canonical
roles are contract-complete without project overrides. Workspace
specialization remains optional and must preserve the same role contract.

After changing global subagent configuration, restart Kent and reopen Kent
Desktop. Skills, prompts, and `AGENTS.md` are consumed by new sessions.

## Workflow generation

Project repositories provide `.kent/workflow-profile.toml`, deterministic
verification scripts, project procedures, and these kit-managed commands:

- `branch_identity` — post-Plan/pre-Implement Jira/GitHub issue branch
  resolution;
- `checkpoint` — atomic ignored Fix/Smoke state under `.kent/runtime/`;
- `evidence` — append-only task evidence and node context metrics under
  `.kent/runtime/`;
- `plan_contract*` — graph-owned accept/check entry points over one ignored
  normalized plan snapshot; deterministic drift routing ignores checkbox-only
  progress;
- `wait_ci` — zero-model GitHub CI watching over every effective current-PR
  check until terminal green/red state;
- `wait_pr` — zero-model GitHub merge and feedback watching;
- `github_observation` — shared bounded read-only GitHub PR, check, and
  feedback observation support module;
- `janitor` — post-Cleanup safe managed-worktree and branch cleanup.

## Selected-revision release preflight

Schema 4 release profiles are checked from the selected Git revision before
any workflow or publication decision. Preflight reads the release spec,
tracked source manifest, snapshot, optional executable builder, profile-owned
commands and procedures, job workflow sources, and approval Scripts from Git
blobs; it never falls back to release files in the working tree.

The manifest owns only sorted, normalized additions and trees. Preflight
derives the mandatory profile and release closure, validates regular-file and
executable modes, expands regular-file trees, checks prompt-reference
coverage, and records raw SHA-256 values for the selected spec, manifest,
snapshot, and builder. Snapshot input is accepted only as a JSON object.

Use the revision launcher with an explicitly selected ref:

```bash
~/.kent/bin/kent-preflight-revision \
  --project /path/to/project \
  --ref <revision>
```

The schema-4 JSON preview is source-side and read-only:
`source_contract_valid=true`, `runtime_attested=false`,
`job_sources_validated=false`, `activation_authorized=false`, and
`snapshot_json_valid=true`. It does not invoke a builder, generate or validate
a workflow graph, call Kent, or activate publication. Runtime envelopes,
external-root bytes, project release semantics, and live effects remain owned
by later project and runtime slices.

Profile synchronization has a strict dual-schema boundary. Schema 3 preserves
legacy `release_topology` and implicit ownership of known command templates;
schema 4 requires explicit `kit_managed_commands`, matching
`command_versions`, and a closed `release` table. Approved release identities
are `appsome-release-publication/managed-in-place`,
`puber-release/managed-in-place`,
`sdk-merged-main-publication/managed-in-place` or
`sdk-merged-main-publication/metadata-only`, and
`slack-reader-release/managed-in-place`. Managed-in-place requires a builder
and graph-bearing workflow intent; metadata-only forbids a builder and requires
metadata-only workflow intent. Synchronization, migration, generation, apply,
and activation remain non-automatic.
The closed `release` fields are `topology_kind`, `adoption_mode`, `spec_path`,
`builder_path`, and `snapshot_path`; managed-in-place requires a non-empty
`builder_path`, while metadata-only requires an empty one.

Schema-4 synchronization owns only listed managed adapters and commands. Every
other non-empty command is executable project-owned content, and the explicit
template registry is checked before a complete no-write preflight plan is
applied. Schema 3 remains supported during the transition; any later
contraction, migration, or activation requires a separate approved change.
`required_adapters` declares executable runtime dependencies and
`kit_managed_adapters` the exact synchronized subset; remaining adapters are
project-owned. The loader is platform-neutral and Android lock/evidence
adapters do not own project startup, device permissions, build/install,
credentials, or runtime acceptance.

Projects add `/.kent/runtime/` to `.gitignore` and declare
`[context_manifests]` entries for `plan`, `implement`, `review`, `smoke`, and
`delivery`. Generated prompts treat the selected manifest as the node's read
budget. Preview a versioned workflow:

```bash
./scripts/generate-workflow \
  --project /path/to/project \
  --kind delivery \
  --version 1
```

Every generated Engineering Delivery profile declares `[work_kinds.<key>]`
entries. Plan selects one supported key from an explicit task-body
`work_kind: <key>` declaration or conservative classification, then carries it
through each Implement slice.

Apply, link as non-default, validate, and export its audit snapshot:

```bash
./scripts/generate-workflow \
  --project /path/to/project \
  --kind delivery \
  --version 1 \
  --apply
```

Create or update the unversioned conditional-Smoke lab without a PR/CI tail:

```bash
./scripts/generate-workflow \
  --project /path/to/project \
  --kind smoke-lab \
  --apply
```

For a separately approved isolated Smoke Lab experiment needing a new graph,
add a free-form suffix such as `--label "iteration beta"`. Labels are
temporary experiment names, not semantic versions. Labeled snapshots include a
deterministic hash suffix so distinct labels cannot overwrite each other.

Changing the project default requires the separate `--set-default` flag. Do not
use it before the generated workflow passes a managed-worktree canary.
Generation may run from a project-local Git worktree. The profile, scripts, and
snapshot stay rooted in that worktree while Kent project linking targets the
repository's primary worktree automatically.

The optional profile policy `policies.writer_sessions` controls writer
continuity. The backward-compatible `continuous` mode reuses or compacts
writer sessions and is preferred for coupled feature work or review bundles.
`fresh_per_slice` starts a new Implement or Fix session for each independently
verifiable slice and is best reserved for low-coupling mechanical work with
small handoffs. Verification Gate always deduplicates review findings into one
dependency-ordered bundle; continuous Fix resolves every compatible group
before re-verification. Non-writer approval-recovery loops retain
compact-and-continue continuity. Adoption follows
[Execution-history and compatibility policy](contracts/workflow-contract.md#execution-history-and-compatibility-policy).
Canary a separately approved new workflow instance before default promotion.

The optional `policies.pr_merge_strategy` accepts `auto`, `merge`, `squash`,
or `rebase` and defaults to `auto`. `auto` resolves from source-control
capabilities, target-branch protection/rulesets, and merge-queue policy. It
continues only when those constraints leave exactly one method. The resolved
strategy is carried through PR creation, CI, and merge waiting; generic
mergeability never substitutes for method-specific feasibility.

Projects that declare
`release_topology = "manual-package-publish-after-main"` also provide a
`procedures.publish` file and `roles.package_release`. Their generated Delivery
graph waits for the PR to merge, requires explicit approval, publishes from the
exact merged source, verifies the remote package, and only then enters Cleanup.
Failed or partial publication preserves the task workspace for an
approval-gated retry. The project procedure owns the credential reference and
build-tool environment mapping; the publisher resolves it just in time instead
of depending on ambient CLI authentication.

`policies.branch_identity` controls source-control naming before the first
implementation writer:
`task` retains Kent's short-ID branch, `jira` uses `feature/<KEY>`, and
`github_issue` uses `issue-<number>` only for an issue in the same GitHub
repository. The task source URL is authoritative; a body-only fallback must
contain exactly one matching issue URL. Multiple body candidates block before
renaming so linked or cloned issues cannot silently become branch identity.
Missing external identity keeps the Kent branch. Existing local or remote
branch collisions stop in a recoverable user-decision node rather than
attaching a new task to ambiguous work. The deterministic Script runs after
the read-only Plan handoff so the task root is established before
project-relative Scripts run. This remains a kit portability guard; Kent 2.6.1
also enforces the managed-worktree `base_dir` namespace and preserves recovery
diagnostics.

After green CI, an open feasible PR moves to a deterministic script watcher.
Unchanged state consumes no model turn and requires no approval. Material
changes wake the retained Waiting PR session; a confirmed merge goes directly
to Cleanup.

Long Fix and Smoke stages atomically checkpoint one next action plus completed,
remaining, and mutation-ledger state. Final Compliance may route
packaging-only report/checklist defects through Evidence Repair and directly
back to Compliance without rebuilding or reacquiring runtime resources.

Managed-worktree Cleanup is two-phase: the Cleanup agent emits a report and
session-scoped request after closing background shells and leaving the task
worktree, then a deterministic Task Janitor runs after that agent exits. It
accepts only a completed Kent deletion with verified postconditions, deletes
only clean task-owned resources proven recoverable from the exact merged PR
head, and preserves every dirty, primary, ambiguous, diverged, or unique
resource.

Verification dispatch validates `workspace_path` before fan-out. The value must
be the canonical current task repository or execution root; `.todo` artifact
directories and foreign repositories are rejected into a metadata-only Fix
slice before any review or build branch starts.

Before starting a generated workflow at a concrete branch, tag, or commit,
validate that the selected revision contains its complete project adapter:

```bash
./scripts/preflight-revision \
  --project /path/to/project \
  --ref feature/my-change
```

The preflight validates the profile and checks required files directly from Git
objects. It does not switch branches or create a worktree. This catches
branch-topology gaps where a live Kent workflow exists but the selected revision
does not yet contain its procedures, executable verification scripts, or
required adapters. During the iterative rollout, canary revisions remain
informational rather than an ancestry gate.

Adapters declared by the profile are checked separately:

```bash
./scripts/sync-project-adapters --project /path/to/project
```

Kit-managed adapters such as the emulator resource lock and evidence audit are
created or updated from templates. Project-owned adapters such as MCP policy or
service wrappers are validated for an executable project-relative path and are
never overwritten. Android projects keep package names, activities, build
variants, accounts, and tested flows in project-owned procedures.

## Current phase

The global toolkit and Kent 2.6.1 workflow generator are implemented and
validated by the repository test suite. The generator shape has prior project
canary coverage; every new candidate still follows the rollout process in the
roadmap. Generated workflows use a shared fan-out/Join/Gate
lifecycle with project-owned profiles, procedures, verification, Smoke, and
delivery adapters. Taskless generated workflows may be reconciled in place only
when the Kent CLI can express the change without deleting nodes or edges,
changing an edge source, or removing an approval. The generic client's
task-reference and project-link guards remain unchanged; general eligibility
follows [Execution-history and compatibility policy](contracts/workflow-contract.md#execution-history-and-compatibility-policy).

Changes to generator prompts or shared contracts do not mutate live workflows.
Adoption requires a separately approved lifecycle operation under that policy;
new non-default experiments require a canary before default promotion.

Kent Desktop may display
`workflow.validation.script_path_relative_check_skipped` for a relative script
node while no task worktree exists. This diagnostic is non-blocking when
execution validation is otherwise valid. Keep relative paths for portability;
revision preflight and the managed task worktree provide the real file and
executable checks.

Current rollout state, audited revision boundaries, and remaining migration work
are tracked in `docs/ROADMAP.md`.
