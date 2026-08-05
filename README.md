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

Role behavior and execution policy are separate contracts. Role prompts must
not declare `model` or `tools`; global or project Kent configuration owns
model, reasoning, verbosity, tool availability, and delegation eligibility. See
`contracts/role-contract.md`. The current cross-project Balanced experiment is
documented in `docs/MODEL-POLICY.md`.

## Compatibility

- Global prompts, skills, and role definitions remain usable with Kent 2.2.
- Project profile schema 3 and generated workflows target Kent 2.5 or newer.
- Kent CLI/TUI, service, and Desktop must be upgraded together when crossing
  protocol boundaries.
- Kent 2.5 requires every transition key to be unique across its whole
  workflow. Generated keys are source-qualified, for example
  `verification_gate_needs_changes`.
- Kent 2.5 workflow and task list commands use offset pagination. The generator
  uses canonical workflow UUIDs after exact-name discovery.
- Workflow deletion remains an explicit user action.
- Existing workflows retain their Source HEAD behavior after an upgrade.
  New generated workflows declare their execution-target policy explicitly.
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
into `~/.kent/config.toml` before restarting Kent. `scripts/validate` compares
every managed field against the effective global config. The installer
intentionally does not rewrite user configuration.

The global baseline includes bounded implementation, build diagnosis, evidence
gating, runtime Smoke, release lifecycle operations, PR/cleanup delivery, CI
monitoring, research, architecture, and independent review roles. These canonical roles are
contract-complete without project overrides. Kent documents workspace config
as higher precedence, but Kent 2.4 canaries observed scheduler-created direct
workflow roles selecting the global definition for a same-named role.
Workspace specialization remains optional and workflow correctness must not
depend on it until that behavior is clarified upstream.

After changing global subagent configuration, restart Kent and reopen Kent
Desktop. Skills, prompts, and `AGENTS.md` are consumed by new sessions.

## Workflow generation

Project repositories provide `.kent/workflow-profile.toml`, deterministic
verification scripts, project procedures, and these kit-managed commands:

- `branch_identity` — post-Plan/pre-Implement Jira/GitHub issue branch
  resolution;
- `checkpoint` — atomic ignored Fix/Smoke state under `.kent/runtime/`;
- `wait_pr` — zero-model GitHub merge watching;
- `janitor` — post-Cleanup safe managed-worktree and branch cleanup.

Projects add `/.kent/runtime/` to `.gitignore`. Preview a versioned workflow:

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

If the current Smoke Lab already has tasks and a structural experiment needs a
new graph, add a free-form suffix such as `--label "iteration beta"`. Labels are
temporary experiment names, not semantic versions. Labeled snapshots include a
deterministic hash suffix so distinct labels cannot overwrite each other.

Changing the project default requires the separate `--set-default` flag. Do not
use it before the generated workflow passes a managed-worktree canary.
Generation may run from a project-local Git worktree. The profile, scripts, and
snapshot stay rooted in that worktree while Kent project linking targets the
repository's primary worktree automatically.

The optional profile policy `policies.writer_sessions` controls writer
continuity. The backward-compatible `continuous` mode reuses or compacts
writer sessions. `fresh_per_slice` starts a new Implement or Fix session for
each independently verifiable slice and hands off through the task worktree,
authoritative artifacts, exact task-comment IDs, and structured transition
parameters. Non-writer approval-recovery loops retain compact-and-continue
continuity. Use a new non-default workflow instance to canary this policy;
task-backed live graphs are never rewritten to adopt it.

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
repository. Missing external identity keeps the Kent branch. Existing local or
remote branch collisions stop in a recoverable user-decision node rather than
attaching a new task to ambiguous work. The deterministic Script runs after the
read-only Plan handoff because Kent 2.5 does not provide a task execution root
to a relative Script used as the first executable node.

After green CI, an open feasible PR moves to a deterministic script watcher.
Unchanged state consumes no model turn and requires no approval. Material
changes wake the retained Waiting PR session; a confirmed merge goes directly
to Cleanup.

Long Fix and Smoke stages atomically checkpoint one next action plus completed,
remaining, and mutation-ledger state. Final Compliance may route
packaging-only report/checklist defects through Evidence Repair and directly
back to Compliance without rebuilding or reacquiring runtime resources.

Managed-worktree Cleanup is two-phase: the Cleanup agent emits a report and
session-scoped request, then a deterministic Task Janitor runs after that agent
exits. It deletes only exact clean task-owned resources proven recoverable and
preserves every dirty, primary, ambiguous, or unique resource.

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

The global toolkit and Kent 2.3+ workflow generator are implemented and
validated against Kent 2.4. Generated workflows use a shared fan-out/Join/Gate
lifecycle with project-owned profiles, procedures, verification, Smoke, and
delivery adapters. Taskless generated workflows may be reconciled in place only
when the Kent CLI can express the change without deleting nodes or edges,
changing an edge source, or removing an approval. A workflow becomes
mutation-protected after tasks reference it.

Changes to generator prompts or shared contracts do not mutate task-backed live
workflows. They require a new non-default experiment and canary before any
default promotion.

Kent Desktop may display
`workflow.validation.script_path_relative_check_skipped` for a relative script
node while no task worktree exists. This diagnostic is non-blocking when
execution validation is otherwise valid. Keep relative paths for portability;
revision preflight and the managed task worktree provide the real file and
executable checks.

Current rollout state, audited revision boundaries, and remaining migration work
are tracked in `docs/ROADMAP.md`.
