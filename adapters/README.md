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
--assert-json-true <JSON-pointer>
--hash-matches <extended-regex>
--marker-present <literal>
```

Safe modes suppress the raw response and are incompatible with `--save-raw` or
`--raw-dir`. Except for `--quiet`, safe modes emit a sanitized
`kent-mcp-result-v1` JSON object with separate `transport`, `processing`,
`action`, `assertion`, and `assertionKind` fields. Transport describes only the
mcporter process result; processing describes the adapter's bounded handling
of its output; `action` remains `unknown` because this adapter has no provider
completion decoder. A successful digest or extraction is not an assertion.
`--marker-present` remains an observation and can report false markers while
processing succeeds.

`--assert-contains` and `--assert-not-contains` are byte-literal checks,
reported as `assertionKind: literal`; they do not establish UI state or
authentication. Repeat `--assert-json-true <JSON-pointer>` to assert that each
selected value is exactly JSON boolean `true`. Pointers use RFC 6901 escaping;
an empty pointer selects the document root. A selected `false` fails;
missing, null, wrong-type, or unsupported values are `unknown`. Duplicate JSON
keys, invalid UTF-8, malformed JSON, and non-finite numbers fail processing.
The sanitized result includes only fixed outcome enums and bounded counts, not
selectors, selected values, or raw parse errors.

Only a consumer that knows the relevant provider/state schema and accounts for
its error conditions may use a passed JSON-boolean assertion as destination
evidence. A true value alone is not a universal provider-success decoder or
proof that an action completed. Without that known schema, the state remains
unknown. Quiet remains empty and process-exit-only; digest, literal checks,
hashes, and marker booleans cannot establish authentication or a passed Smoke
checkpoint. Existing authorized bounded visual or other observation paths
remain available under the Mobile Smoke contract.

The adapter limits interpretation to 32 combined predicates/markers, 4096
UTF-8 bytes per selector/literal, 1 MiB per parsed/asserted/extracted response,
and 256 unique extracted hashes. Exceeding argument limits is a usage error;
oversized or invalid processing returns a sanitized failure, never truncated
success. Digest hashing is streaming. These limits do not bound the existing
temporary stdout/stderr capture of the initial tool process. Neither that
capture nor safe output is an authorization to persist raw UI, authentication,
credential, header, broad-log, or unredacted network content.

Portable servers are added separately:

```bash
./scripts/configure-mcporter --apply
./scripts/audit-mobile-schema
```

The managed `mobile` server uses mcporter's default ephemeral lifecycle. Mobile workflows must
list devices, acquire the project resource lock, and pass `platform` plus the
exact locked `deviceId` to every target-specific call. Process-local
`device set` / `get_target` state is not a valid cross-call target guarantee.
The adapter enforces explicit device addressing for `screen`, `input`, `ui`,
and `app`. It rejects generic `system` and `flow` calls because their current
schema cannot prove one exact locked device; use the project's exact platform
adapter (`adb -s`, simulator UDID, or equivalent) instead of an implicit MCP
target.

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

Kent resolves relative postprocessor paths from the service process working
directory. Use the stable home-relative installed path rather than a
project-relative path.

## Gradle UI story-change evidence

Projects that opt into checking changes to supported UI can use the
[story-change policy](gradle/story-change-policy.md) and its
[bounded evidence validator](gradle/story_change_evidence.py). The adapter
validates evidence structure and status; each project owns adoption and review
of actual registry membership, source changes, and check results.

## Jira source adapter

Projects that use Jira as an authoritative planning source may declare the
kit-managed adapter:

```toml
required_adapters = ["jira_api"]
kit_managed_adapters = ["jira_api"]

[adapters]
jira_api = ".kent/adapters/jira/jira-api.sh"

[integrations.jira]
base_url = "https://example.atlassian.net"
credential_namespace = "EXAMPLE"
op_vault = "Private"
op_item = "Example Jira API Token"
```

Synchronize it with `scripts/sync-project-adapters`. The repository stores only
the base URL, credential namespace, and optional 1Password pointers. It never
stores email addresses, API tokens, or resolved secrets.

Credential resolution prefers generic `KENT_JIRA_*` variables, then
`<CREDENTIAL_NAMESPACE>_JIRA_*`, then `JIRA_*`. Each form supports direct
credentials or `_OP_REF` pointers. This lets related projects intentionally
share one namespace while unrelated projects select independent Jira tenants
and tokens.

The common adapter supports issue, comment, URL, Jira-relation, JQL, board, and
board-issue ingestion plus a small exact-target mutation surface:
`create-issue`, `edit-issue`, `comment-issue`, and `transition-issue`. Actual
mutations require `--allow-mutate`; safe payload previews use `--dry-run`.
Natural-language writes default to English and require
`--allow-non-english` for an explicit exception.

The common adapter does not expose version release, deletion, arbitrary custom
fields, or bulk mutation. Projects own the approval policy for its write
commands.

A project-extended adapter with the same canonical key may add separately
gated release/version operations. It remains project-owned by omitting it from
`kit_managed_adapters`; the synchronizer validates the executable but never
replaces it, even with `--update`.

## Sentry issue adapter

Projects that ingest exact Sentry issues may declare the kit-managed adapter:

```toml
required_adapters = ["sentry_issues"]
kit_managed_adapters = ["sentry_issues"]

[adapters]
sentry_issues = ".kent/adapters/sentry/sentry-issues.sh"

[integrations.sentry]
base_url = "https://sentry.io"
organization = "example"
project = "android"
credential_namespace = "EXAMPLE"
```

The adapter uses the official Sentry REST API for structured issue/event reads
and seen-state updates. It uses the official `sentry-cli` for resolve, mute,
and unresolve operations. Every mutation is exact-issue only, supports
`--dry-run`, and requires `--allow-mutate`; bulk mutation is intentionally not
exposed.

Credential resolution prefers `KENT_SENTRY_AUTH_TOKEN`, then
`<CREDENTIAL_NAMESPACE>_SENTRY_AUTH_TOKEN`, then `SENTRY_AUTH_TOKEN`, followed
by matching `_OP_REF` variables. A machine may instead store one 1Password
reference in:

```text
~/.kent/credentials/sentry/<lowercase-credential-namespace>.opref
```

That local file contains only an `op://...` reference, uses mode `0600`, and is
never committed. Profiles store the tenant coordinates and credential
namespace only; they must not name a vault/item or contain a token.

`candidates` defaults to unresolved issues and omits issues already seen by the
current Sentry user. `issue` and `latest-event` emit bounded normalized context
without raw request, user, breadcrumb, variable, or event-context payloads.
An explicitly Sentry-backed task may mark its exact issue seen after durable
task context exists. Resolve and mute remain approval-gated delivery decisions.

## Mobile runtime safety adapters

Android projects with conditional or required runtime Smoke declare:

```toml
required_adapters = ["mobile_resource_lock", "mobile_evidence_audit"]
kit_managed_adapters = ["mobile_resource_lock", "mobile_evidence_audit"]

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

### Mobile resource-lock interface

Before calling the adapter, a project procedure resolves the current Task's
native ID and short ID from native Task JSON and obtains the current Session
with `kent session-id`. Kent does not promise to export `KENT_TASK_ID`; the
procedure sets `KENT_TASK_ID` to the verified Task short ID and
`KENT_SESSION_ID` to the current Session. New leases use the Task short ID as
owner identity. A legacy native-ID owner may be retained only when native
readback proves its short-ID mapping.

The stdout API is command-specific: `acquire` emits a bare token, while
`acquire-any` emits exactly `resource=<id>` and then `token=<value>`. Parse the
selected command's complete output, validate it, and checkpoint the resource
and token before device operations. `status` redacts tokens. After token-matched
release or trap cleanup, read back the exact resource and require `unlocked`;
otherwise cleanup remains unresolved. The detailed checkpoint and recovery
rules live in
[`contracts/mobile-smoke-contract.md`](../contracts/mobile-smoke-contract.md).

`required_adapters` is platform-neutral: profiles list the executable adapters
their workflow contract cannot operate without. These adapters do not choose a
device policy for the project. Project procedures still define whether an
emulator may be started, whether a physical device is allowed, the
APK/application target, and the runtime evidence required.

`kit_managed_adapters` is the explicit subset synchronized from toolkit
templates. Required adapters outside that list are project-owned.

`mobile_evidence_audit` fails closed when evidence contains broad-log
artifacts, common authentication/account payload markers, or symlinks. It
reports filenames and reasons without echoing matched content. Project
procedures pass the tested package name and run it before completing Smoke.
