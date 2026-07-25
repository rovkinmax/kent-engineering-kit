# Kent Engineering Kit

Personal, platform-neutral engineering workflows for Kent.

The kit separates:

- global engineering disciplines in `skills/`;
- explicit user-driven flows in `prompts/`;
- reusable read-only roles in `agents/`;
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
- Project profile schema 3 and generated workflows target Kent 2.3 or newer.
- Kent CLI/TUI, service, and Desktop must be upgraded together when crossing
  the 2.2/2.3 protocol boundary.
- Kent 2.3.0 workflow commands use persisted `workflow-...` IDs, while 2.3.1
  and newer use bare canonical workflow UUIDs. The generator resolves an exact
  display name only for discovery, then preserves the selector form returned by
  the installed Kent version for every operation.
- Kent 2.4 adds safe workflow-deletion previews. Destructive confirmation
  remains an explicit user action.
- Existing workflows retain their Source HEAD behavior after a 2.3 upgrade.
  New generated workflows declare their execution-target policy explicitly.

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

`config/subagents.toml` is the authoritative managed config fragment. Merge it
into `~/.kent/config.toml` before restarting Kent. `scripts/validate` compares
every managed field against the effective global config. The installer
intentionally does not rewrite user configuration.

After changing global subagent configuration, restart Kent and reopen Kent
Desktop. Skills, prompts, and `AGENTS.md` are consumed by new sessions.

## Workflow generation

Project repositories provide `.kent/workflow-profile.toml`, deterministic
verification scripts, and project procedures. Preview a versioned workflow:

```bash
./scripts/generate-workflow \
  --project /path/to/project \
  --kind delivery \
  --version 1
```

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

The optional profile policy `policies.writer_sessions` controls writer
continuity. The backward-compatible `continuous` mode reuses or compacts
writer sessions. `fresh_per_slice` starts a new Implement or Fix session for
each independently verifiable slice and hands off through the task worktree,
authoritative artifacts, exact task-comment IDs, and structured transition
parameters. Non-writer approval-recovery loops retain compact-and-continue
continuity. Use a new non-default workflow instance to canary this policy;
task-backed live graphs are never rewritten to adopt it.

Before starting a generated workflow at a concrete branch, tag, or commit,
validate that the selected revision contains its complete project adapter:

```bash
./scripts/preflight-revision \
  --project /path/to/project \
  --ref feature/my-change \
  --baseline-ref origin/main
```

The preflight requires the audited baseline to be an ancestor of the selected
revision, rejects project-profile drift, and checks required files directly
from Git objects. It does not switch branches or create a worktree. This catches
branch-topology gaps where a live Kent workflow exists but the selected revision
does not yet contain its procedures, executable verification scripts, or
required adapters.

Project-local adapters declared by the profile are synchronized separately:

```bash
./scripts/sync-project-adapters --project /path/to/project
```

Android projects with runtime Smoke use shared emulator resource-lock and
evidence-audit templates while keeping package names, activities, build
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

Current rollout state, audited revision boundaries, and remaining migration work
are tracked in `docs/ROADMAP.md`.
