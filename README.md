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
- Project profile schema 2 and generated workflows target Kent 2.3 or newer.
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

Changing the project default requires the separate `--set-default` flag. Do not
use it before the generated workflow passes a managed-worktree canary.

## Current phase

The global toolkit and Kent 2.3 workflow generator are implemented. Appsome and
Puber have linked non-default `Engineering Delivery v2` instances. Managed
worktree canaries remain required before either project changes its default.
