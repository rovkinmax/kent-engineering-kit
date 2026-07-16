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

## Android emulator resource lock

Android projects with conditional or required runtime Smoke declare:

```toml
required_adapters = ["mobile_resource_lock"]

[adapters]
mobile_resource_lock = ".kent/adapters/mobile/emulator-resource-lock.sh"
```

Synchronize the committed project-local adapter:

```bash
./scripts/sync-project-adapters --project /path/to/project
```

Use `--update` only after reviewing a differing project copy. The adapter keeps
machine-wide locks under `~/.kent/runtime/resource-locks`, separates emulators
from physical devices, and requires token-matched release. A managed worktree
therefore carries the executable while still coordinating with every other
Kent session on the machine.

`required_adapters` is platform-neutral: profiles list the executable adapters
their workflow contract cannot operate without. The adapter does not choose a
device policy for the project. Project procedures still define whether an
emulator may be started, whether a physical device is allowed, the
APK/application target, and the runtime evidence required.
