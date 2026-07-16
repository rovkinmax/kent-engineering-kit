# Platform Adapters

Adapters contain opt-in behavior for a build system, platform, or toolchain.
Projects select them explicitly; the platform-neutral toolkit core does not
assume that every repository uses them.

## Gradle shell postprocessor

The Gradle adapter warns when an agent runs `./gradlew` directly inside a Git
worktree that provides `./tools/agentw`.

The installer exposes it at:

```text
~/.kent/hooks/gradle-worktree-warning
```

Opt in from project configuration:

```toml
[shell]
postprocessing_mode = "all"
postprocess_hook = "~/.kent/hooks/gradle-worktree-warning"
```

Kent 2.3 resolves relative postprocessor paths from the service process working
directory. Use the stable home-relative installed path rather than a
project-relative path.
