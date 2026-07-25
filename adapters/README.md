# Platform Adapters

Adapters contain opt-in behavior for a build system, platform, or toolchain.
Projects select them explicitly; the platform-neutral toolkit core does not
assume that every repository uses them.

## Global MCP adapter

The installer exposes:

```text
~/.kent/bin/kent-mcp-call
~/.kent/bin/kent-mcp-list
```

The adapter uses the current worktree as the execution and artifact root while
resolving project identity from the primary Git worktree. Machine config files
such as `~/.kent/mcp.Puber.env` therefore remain valid inside task-named Kent
worktrees. Process `MCP_CONFIG_PATH` remains the highest-priority override.

Call metadata is logged, and no separate raw artifact is created by default.
Normal stdout is still part of the Kent shell transcript. Sensitive calls must
therefore select one safe output mode:

```text
--quiet
--digest-output
--assert-contains <literal>
--assert-not-contains <literal>
--hash-matches <extended-regex>
--marker-present <literal>
```

Safe modes suppress the raw response and are incompatible with `--save-raw` or
`--raw-dir`. Mobile tools other than `device` fail closed without a safe mode.
Use output assertions for known acceptance facts and digests for before/after
equivalence without disclosing content. `--hash-matches` emits only unique
SHA-256 values for matched semantic tokens; combine it with one or more
`--marker-present` checks for bounded pagination proof. Neither matched values
nor marker literals are copied from the response to stdout. Never opt in to raw
output for an unexpected authenticated UI tree, credentials, headers, broad
device logs, or unredacted network payloads.

Portable servers are added separately:

```bash
./scripts/configure-mcporter --apply
./scripts/audit-mobile-schema
```

The managed `mobile` server uses mcporter's default ephemeral lifecycle. Mobile workflows must
list devices, acquire the project resource lock, and pass `platform` plus the
exact locked `deviceId` to every target-specific call. Process-local
`device set` / `get_target` state is not a valid cross-call target guarantee.
The current schema exposes explicit device addressing for `screen`, `input`,
`ui`, and `app`. If a target-specific tool such as `system` lacks `deviceId`,
use the project's exact platform adapter (`adb -s`, simulator UDID, or
equivalent) instead of an implicit MCP target.

Project-specific stdio servers may remain executable at
`.kent/adapters/mcp/servers/<server>` or the legacy
`.kent/adapters/mcp/<server>-server.sh` path. Credentials and server-specific
policy remain project-owned.

Unknown non-mobile tools require `--allow-mutate` by default. A project may
classify its own tools with executable `.kent/adapters/mcp/policy`; it receives
`<server.tool>` and `action` and prints exactly `read-only`, `mutating`,
`blocked`, or `inherit`. The adapter resolves this hook from the primary Git
worktree so managed worktrees share one policy.

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

## Mobile runtime safety adapters

Android projects with conditional or required runtime Smoke declare:

```toml
required_adapters = ["mobile_resource_lock", "mobile_evidence_audit"]

[adapters]
mobile_resource_lock = ".kent/adapters/mobile/emulator-resource-lock.sh"
mobile_evidence_audit = ".kent/adapters/mobile/mobile-evidence-audit.sh"
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
their workflow contract cannot operate without. These adapters do not choose a
device policy for the project. Project procedures still define whether an
emulator may be started, whether a physical device is allowed, the
APK/application target, and the runtime evidence required.

`mobile_evidence_audit` fails closed when evidence contains broad-log
artifacts, common authentication/account payload markers, or symlinks. It
reports filenames and reasons without echoing matched content. Project
procedures pass the tested package name and run it before completing Smoke.
