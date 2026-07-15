# Kent Engineering Kit

Personal, platform-neutral engineering workflows for Kent.

The kit separates:

- global engineering disciplines in `skills/`;
- explicit user-driven flows in `prompts/`;
- reusable read-only roles in `agents/`;
- workflow and project adapter contracts in `contracts/`;
- future workflow generation in `workflows/`.

Project repositories remain responsible for architecture rules, build commands,
device details, release policy, and integration credentials.

## Installation

Run:

```bash
./scripts/install
./scripts/validate
```

The installer creates additive symlinks under `~/.kent`. It refuses to replace
existing non-symlink files.

`config/subagents.toml` is the authoritative managed config fragment. Merge it
into `~/.kent/config.toml` before restarting Kent. `scripts/validate` compares
every managed field against the effective global config. The installer
intentionally does not rewrite user configuration.

After changing global subagent configuration, restart Kent and reopen Kent
Desktop. Skills, prompts, and `AGENTS.md` are consumed by new sessions.

## Current phase

This initial phase installs global skills, prompts, and read-only roles. It does
not modify live workflow graphs. Workflow fragments and the generator are the
next phase after the Kent restart.
