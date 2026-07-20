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

## Compatibility

- Global prompts, skills, and role definitions remain usable with Kent 2.2.
- Project profile schema 3 and generated workflows target Kent 2.3 or newer.
- Kent CLI/TUI, service, and Desktop must be upgraded together when crossing
  the 2.2/2.3 protocol boundary.
- Existing workflows retain their Source HEAD behavior after a 2.3 upgrade.
  New generated workflows declare their execution-target policy explicitly.

## Installation

Run:

```bash
./scripts/install
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

The global toolkit and Kent 2.3 workflow generator are implemented. Generated
workflows use a shared fan-out/Join/Gate lifecycle with project-owned profiles,
procedures, verification, Smoke, and delivery adapters. Taskless generated
workflows may be reconciled in place only when the Kent CLI can express the
change without deleting nodes or edges, changing an edge source, or removing an
approval. A workflow becomes mutation-protected after tasks reference it.

Current rollout state, audited revision boundaries, and remaining migration work
are tracked in `docs/ROADMAP.md`.
