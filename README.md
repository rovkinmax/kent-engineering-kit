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
existing non-symlink files.

Platform adapters are installed under `~/.kent/hooks` but remain inactive until
a project explicitly selects them.

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

## Current phase

The global toolkit and Kent 2.3 workflow generator are implemented. Appsome and
Puber have linked non-default experimental `Engineering Delivery v5`,
`Engineering Canary v2`, and unversioned `Engineering Smoke Lab` instances
generated from the current profile-schema-3 hypothesis. Numeric suffixes are
lab labels, not frozen releases. Taskless generated workflows may be reconciled
in place only when the Kent CLI can express the change without deleting
nodes/edges, changing an edge source, or removing an approval. Unsupported
structural drift uses another free-form lab label. A workflow becomes
mutation-protected after tasks reference it in any linked project. Defaults
remain unchanged.
