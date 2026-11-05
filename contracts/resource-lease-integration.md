# Cooperative resource-lease integration

This opt-in adapter invokes an independently installed `resource-lease` CLI.
It does not contain the resource service, Android backend, or a Kent lifecycle
extension. Adoption uses a coordinated Kit/workflow update and Task migration
before new consumers start. Old and new ownership protocols are not intended
to run concurrently; this integration adds no legacy-consumer exclusion or
separate device-discovery infrastructure. Preserve existing work during the
coordinated transition; never reinterpret old lock receipts as new leases.
Like the current standalone CLI, this adapter requires Python 3.11+ and a POSIX
host. It does not claim Windows support.

## Configuration and interface

The project profile adapter key is `resource_lease`; its managed executable
filename is `resource-lease-client`. Register the same key in required and
Kit-managed adapter lists when opting in.

`resource-lease-client` uses a private JSON configuration with exactly:

```json
{
  "schema": 1,
  "executable": "/absolute/path/to/resource-lease",
  "pool": "/absolute/path/to/private-pool",
  "runs_directory": "/absolute/path/to/private-consumer-runs",
  "profiles": {
    "ui.clean": "project-ui-clean",
    "ui.persistent": "project-ui-persistent",
    "smoke.clean": "project-smoke-clean",
    "smoke.persistent": "project-smoke-persistent"
  }
}
```

Profile names and capabilities are project decisions. Missing profiles fail;
there is no fallback. The adapter does not start or initialize the service.
The installer pins the executable artifact and prepares the pool separately.
Configuration, run directories, receipts and control files are owner-private.
No receipt secret may appear in argv, checkpoints, logs or workflow evidence.
Keep at most 64 proven-finished run records. Unknown/failed records are retained;
at 256 total run entries admission stops for explicit operator inspection.

Commands:

```sh
resource-lease-client run --config CONFIG --profile PROFILE --owner LABEL \
  --env VARIABLE=endpoint_key -- COMMAND ARGUMENTS
resource-lease-client start --config CONFIG --profile PROFILE --run-dir RUN \
  --project PROJECT --task TASK_UUID --session SESSION_UUID
resource-lease-client check --run-dir RUN
resource-lease-client finish --run-dir RUN
resource-lease-client reconcile --request REQUEST --qualification QUALIFICATION
```

`--env` is repeatable. The adapter rejects conflicting inherited values and
missing endpoint keys. A project wrapper supplies platform-specific mappings
and commands; the adapter itself has no Android or Gradle knowledge.

`reconcile` is a separate, proof-gated terminal-only route for legacy status
records. It does not acquire, renew, release, cancel, restart helpers, or issue
device commands. It requires an exact request and qualification envelope
referencing the original native run. New-protocol records and unresolved
markers cannot use this route. Never use it as a general status repair.

## Stop provenance protocol 2

New bindings and status records use `stop_protocol: 2` and
`custody_marker_protocol: 2`. Status keeps the public aggregate
`command_stop`, derived from two independent causes:

- `parent_command_stop` records the lease client's local child lifecycle.
- `remote_command_stop` records the remote operation lifecycle.

A remote operation is represented by one `remote-stop.json` record with
`schema: 1`, `stop_protocol: 2`, a canonical operation UUID, exact run path,
binding digest, lease identity and a durable marker fingerprint. The fingerprint
binds device/inode, size, modification/change times and SHA-256 from marker
creation through confirmation, including across owner restart. A missing
fingerprint after a crash is not reconstructed from operation UUID or marker
contents; reconciliation fails closed. Its state is `pending` or `confirmed`.
The `command-stop-uncertain.json` marker has `schema: 2` and the same operation
UUID; it is removed only when its durable fingerprint matches the confirmation
record. A pending operation remains pending if its marker is missing. Parent-command
uncertainty uses the separate `parent-command-stop-uncertain.json` marker and
cannot be cleared by remote confirmation. `finish` succeeds only after all
applicable causes are settled and resource cleanup is confirmed. Legacy
bindings/statuses remain legacy; ordinary client loading refuses them rather
than silently rewriting provenance.
The bound operation record retains historical lease generation. Terminal pool
observations may report `generation: null`; repeated finish keeps that actual
observation null and does not depend on the previous status retaining a number.

## Legacy stop proof and reconciliation

The bounded reader is delivered beside `resource-lease-client` as
`resource_lease_stop_proof.py`. Its managed synchronization copies both files
as one adapter unit. The client opens only that owner-owned, non-symlink sibling,
checks a fixed SHA-256 pin before compiling it, and rejects writable or changed
reader files before execution. The only supported producer contract is
`native-start-borrowed-android-reviewed-history-v1`; native `run` records,
unknown stop causes, missing owner context, incomplete coverage, unknown
native event schemas, live/foreign markers or changed history/preimages block.
It is not a generic shell interpreter or fleet migration.

Proof JSON is bounded, UTF-8, rejects duplicate keys/non-finite values and
uses canonical JSON (`sort_keys`, compact separators, UTF-8, no NaN/Infinity)
for C/I digests. All referenced proof objects are owner-private files with
absolute canonical paths, exact byte sizes and SHA-256 digests. The request
has exactly `schema`, `run_directory`, `expected_preimage_sha256`,
`qualification_ref`, and `effect_approval_ref`. The qualification envelope
binds C and I digests, immutable C/I references and exactly two native audit
locators. C contains the evidence core (`subject`, `source_closure`, `history`,
`coverage`, and `manifest_inventory`); I binds its C digest, expected status
preimage, the exact terminal-only mutation and a monotonic-at-application
timestamp policy. Q contains no approval or audit-report digest cycle. The
approval is outside Q and must bind both Q and I digests to
`terminal_status_reconciliation`.

The closed packet object shapes are:

- C: `schema`, `producer_contract`, `subject`, `source_closure`, `history`,
  `coverage`, `manifest_inventory`.
- `subject`: project/task/session UUIDs, exact run directory, binding and receipt
  digests, and lease identity (`id`, `slot`, historical `generation`, `serial`).
- `source_closure`: `candidate_ref`, non-empty `source_refs`,
  `runtime_schema_ref`, and non-empty `auxiliary_refs`.
  Producer admission uses exact reviewed legacy revisions, not AST word
  presence: Stage-input, client, custody, facade, native provider and evidence
  module bytes must match their pins. Preserve the two actually invoked
  project delegates as separate private auxiliary objects; their executable
  candidate entries must match too. A similarly named unused global shim is
  not an execution-chain edge. The candidate must bind executable
  `tools/stage-input` to the same source bytes. Supported Stage-input recipes
  preserve statement order, exact run resolution and result position, with
  reviewed checkpoint administration, private preparation and guarded
  read-only checks. Private preparation additionally requires its original
  native Add File and exact source revision. No shell/Ruby/jq code is evaluated.
  Facade results require actual supported invocations, source-pinned inline
  filters where used, matching operations/serials, and exact equality of the
  native-attributed and durable started/finished invocation sets. A manifest
  locator printed by an unrelated command is not invocation proof. Public
  result data and artifacts are compared using the pinned evidence module's
  narrower sanitization/staging projection, not assumed byte-identical schemas.
  `runtime_schema_ref` is a `kent-question-runtime-source-v1` object with
  `kent_version` exactly `"2.12.0"` or `"2.13.0"` and exactly three
  `source_refs`, pinned by the reader to the reviewed hashes for
  `ask_question_resolution.go`, `ask_question_tool.go`, and
  `question_history_projection.go`. The three files are byte-identical across
  these two qualified versions. This source closure is required to interpret
  the native Question answerer field.
- `history`: native `start`, optional `async_completion`,
  `audited_start_sequence`, and native `cutoff`.
- Each coverage row: `event_locator`, one supported disposition, and one or more
  immutable `evidence_refs`. Each manifest row: `invocation_id`, started and
  optional manifest digests, and `object_ref`.
  `mixed_administrative_execution_reviewed` denotes only a reviewed execution
  boundary for a composite command (for example checkpoint writes, steering,
  lifecycle attempts, inspection, and artifact export). It asserts neither
  success, confirmed stop, backend/business effects, nor absence of effects;
  prior UNKNOWN outcomes remain unchanged. It cannot replace a qualified
  Stage-input stop disposition or source-causal validation. Actual Stage-input
  invocations, including private preparation/checkpoint composites, still
  require `stage_input_stop_confirmed` and the independent native producer
  check. Native start, manifest attribution, pre-dispatch rejection, complete
  invocation coverage, and both twelve-claim audits remain mandatory.
- I: `schema`, `operation_id`, `core_sha256`, `expected_preimage_sha256`,
  `application_timestamp_policy`, `permitted_mutation`, and `target_status`.
  The only mutation is `terminal_status_only` to `finished/confirmed`; the
  target requires preserving the prior error and all unknown effects.
- Q: `schema`, `core_sha256`, `intent_sha256`, `core_ref`, `intent_ref`,
  and exactly two `audit_refs`.

Native history is interpreted from the actual `kent.session.events` v2
records: `openai-responses` function-call and native custom patch items are base64-decoded and joined by
call ID and Step to the native presentation, `tool_completed`, provider output,
and framework-owned asynchronous completion where applicable. Custom patch
outputs join the native structured result to the provider's JSON result;
image outputs preserve their actual list-valued provider envelope. Shell presentation
text alone is never an invocation. Owner identity is also checked against the
read-only native Task/Session/node/Step binding; project and Task IDs retain their
native prefixes.
An empty native `write_stdin` observation may settle its exact original
background process only with genuine terminal shell metadata and provider
completion; a nonterminal poll or printed exit text alone is insufficient.
The canonical native persistence root and metadata database may be
owner-controlled but readable by others, as the native producer creates them.
They must not be group/other writable or symlinks; Session histories and
supplied proof/custody objects retain their owner-private requirements.

Reviewer assignment must be a genuine parent native `kent run --agent` call
whose completion resolves to the reviewer Session, with the retained native
role, prompt and enabled-tool snapshot checked read-only from the native session
metadata database. A supplied `role=user` JSON message is not assignment proof.
The native CLI's exact `Started a new session` response may identify the
reviewer by its positive numeric short ID. That short ID must resolve through
the same read-only native database to the exact reviewer UUID and project,
whose native parent, role, prompt, and tools still qualify independently.
The actual native shell completion must either be successful or carry the
framework's background-dispatch envelope; echoed UUIDs, arbitrary substrings,
and later stdout cannot replace the assignment completion.
A `lease-stop-audit-report-v1` report is a native assistant response item, not a
copied transcript. A
`lease-stop-audit-report-v1` message has exactly `schema`,
`reviewer_session_id`, `assignment_locator`, `core_sha256`, `intent_sha256`,
`interval`, `coverage_complete`, `facts`, and `findings`; it must report
complete coverage, no unresolved findings, and one evidence-linked `pass` fact
for each of: `native_owner_binding_exact`, `source_closure_reviewed`,
`stage_input_stop_confirmed`, `facade_manifest_coverage_complete`,
`process_settlement_complete`, `direct_command_coverage_complete`,
`parent_stop_cause_rejected`, `unknown_effects_preserved`,
`history_interval_complete`, `history_tail_reviewed`,
`terminal_cleanup_confirmed`, and `crash_retry_and_cas_reviewed`. Each fact
contains `claim`, `conclusion`, and non-empty `evidence_refs`; references are
native event locators or immutable object references. The request's native
effect-approval locator must explicitly approve the exact terminal effect;
a supplied approval-shaped JSON document is not consent. Event hashes cover
the exact UTF-8 JSON event line excluding its newline delimiter.

Native locators are exactly `project_id`, `session_id`, `event_sequence`, and
`event_sha256`. The reader resolves them only beneath the configured native
persistence root (`KENT_PERSISTENCE_ROOT`, otherwise `~/.kent`), accepts only
`kent.session.events` version 2 and validates sequence, exact event bytes and
hashes. Each audit report is a structured native assistant message binding C,
I and the complete audited interval, with factual findings and no unresolved
findings. Its assignment locator resolves to the genuine native parent call
and matching completion assigning that reviewer to the same Task with retained
read-only restrictions. The final approval locator resolves to the original
native `ask_question` call and its structured native completion answer, binding
Q, I and the exact terminal-status-only effect. The approval may belong to the
owner's tail or an actual coordinator Session in the same project; Q/I bind
its exact subject. A coordinator need not be the owner's native parent.
Under the source-pinned Kent 2.12.0/2.13.0 protocol, explicit
`answered_by_session_id: null` means the answer was from the User. When the
native encoder omits a nil actor field, the reader requires an exact join to
the genuine native Question-history projection: Question text, typed answer,
commentary, committed timestamp and an explicit actor field. It reads the
bounded API result transiently without retaining unrelated answers.
Raw absence alone never supplies attribution. The effect-time
installed Kent CLI must report the requested target version `2.13.0`; source
events from either of the two pinned versions remain interpretable because
their relevant Question protocol files are byte-identical. Missing or ambiguous
actor evidence and non-null Agent effect-approval attribution block. The typed answer must
select option 1 from the exact `Approve` / `Decline` suggestions, and the
Question text must bind Q, I and the terminal-status-only effect.
The native optional `recommended_option_index` is supported as typed, in-range
display metadata. It neither selects the answer nor grants consent; invalid
types/bounds or unrelated extra arguments block.
Copied transcripts, supplied event files and plain reviewer/approval summaries
are not accepted.

An existing factual recovery Question is not effect consent. To cover that
specific review tail, include a private `review-tail.json` object through
existing `auxiliary_refs`, binding it into C and both audits. Its closed shape
is `schema: native-stop-reviewed-tail-v1`, `subject` (project/task/session IDs)
and a bounded non-empty `records` list. Each record has only
`invocation_locator`, `purpose: same_run_recovery_information` and
`expected_answerer_session_id`. The reader requires the exact original native
Question and its genuine, completed freeform Agent answer from that declared
Session before reconciliation. A pending Question remains pending; the
declaration cannot manufacture completion, human consent or dispatch authority.

When the native shell result clips output that remains in the original spool,
one coverage row may include an owner-private `full-output.json` evidence
reference. Its closed shape is `schema: native-shell-full-output-v1`,
`invocation_locator`, `completion_locator`, `full_output_ref` and
`original_spool_path`. Recovery supports exactly one qualified filtered facade
producer, not arbitrary shell-output substitution. The reader checks:

- Original invocation/completion/Step and exact native lifecycle timestamps
  from the Session's native `steps.log`.
- Native Session short ID and its canonical owner-private `/tmp/kent/<ID>`
  spool, regular private files, unchanged original/preserved bytes and digest,
  original mtime within that call, and one unique admissible spool.
- Exact mathematical equality of the complete UTF-8 output, native prefix and
  native character-omission count. A guessed suffix or matching substring fails.
- The actual pinned inline filter, complete parsed result with its explicit
  error and manifest fields, and independent durable manifest/lease identity.

The spool filename is not a native call ID. Filtered output is not a full
facade receipt: unprinted transport, effect or lease fields are never filled
in. Both audits cover the declaration, complete original output and provenance;
source-method checks are not those audits. Missing, changed, ambiguous or
unattributable originals block. Retain the original spool until settlement;
an OS cleanup does not authorize reconstructed proof.

Before effect, the reader revalidates the proof, original Task/Session,
post-cutoff native tail and original receipt. The original helper lock is
acquired before the run-directory lock. The receipt is inspected only through
its configured resource CLI and must be terminal, clean, unquarantined and
have no endpoint; pool terminal state alone is not stop proof. No device,
input, acquire, renew, release or helper-restart operation occurs. The reader
requires the exact status preimage, matching original binding/receipt
fingerprints and no uncertainty/foreign stop markers.
Historical generation comes from the validated native acquisition, not a
fabricated current pool observation. A legacy status with explicit null
generation is admissible only with the same lease ID/slot and an actual clean
terminal observation; active, missing or conflicting generations block.
Pre-existing ordinary finish/renewal-stop barriers are preserved. Their exact
private bytes and filesystem identities are snapshotted during qualification
and compared again under the lock and after terminal inspection; creation,
replacement, removal or mutation blocks rather than requiring deletion.

Before atomic status replacement it exclusively persists one bounded
`reconciliation-<operation UUID>.json` record containing the original
preimage bytes, C/I/Q and approval references, terminal observation, exact
postimage bytes and their hashes. It then atomically writes the terminal
status and fsyncs the run directory. The status links to the record by the
intent operation UUID; the record is never made eligible for ordinary pruning.
Retry may continue only from the original preimage plus a matching durable
record, or report already-applied only for its exact postimage and record.
Any other combination blocks. Reconciliation preserves the original error and
unknown business effects; `finished/confirmed` means custody and cleanup only,
not test success or application success. Actual reconciliation still requires
its separately approved exact proof and current owner context.
The native tail admits serial, completed reconciliation attempts followed by
the genuine current pending invocation only for identical request,
qualification and executable arguments. A retry therefore reaches the same
CAS/idempotency checks; overlapping or foreign attempts remain blocked.

`run` acquires one lease, renews while the direct command runs, waits for its
synchronous finalizers, and releases. It preserves command failure and reports
cleanup failure separately; lease loss cannot produce success. Ordinary
signals stop known owned work with bounded waits. It does not prove that an
escaped daemon or arbitrary descendant stopped.

For `start`, the caller allocates an exact RUN path below runs_directory and
records that path before dispatch. The helper owns acquisition and renewal
across tool calls. Repeated `start` for the same live exact binding is
idempotent; it must not create a second request after an ambiguous response.
Stopped helpers cannot be restarted with an expired receipt.

`start` and `check` return JSON containing `run_directory`, public lease
identity/state and `endpoint`. Read the exact endpoint for every device call.
`check` requires both a live helper observation and a currently active lease.
It never renews or revives a stopped helper.
`finish` requests ordinary release and returns its verified outcome. It never
kills a process discovered from a stored numeric PID. Keep failed/unknown
receipts and diagnostics for explicit recovery.

## Native owner observation

The smoke helper binds the original Project, native Task UUID and Session UUID.
It uses bounded `kent task sessions` pagination and `kent task show` readback.
Renew only while the exact Session is `running` and the Task is nonterminal.
Stop renewal permanently on Question, idle, missing owner, terminal/unknown
state, invalid response, timeout or CLI failure. Do not immediately release
merely because observation fails; existing lease expiry is the fallback.

Renewal cadence is derived from CLI `expires_in`, no greater than one third
of the observed remaining lease lifetime. CLI operations are bounded.
When a resource-lease subprocess exits nonzero, the client reports the requested
operation and exit code without copying subprocess stdout or stderr. Acquisition
failures additionally identify the existing pool-status command using the
executable and pool from the client configuration and advise inspecting the
original run before retrying. The displayed `<executable>` and `<pool>` are
literal placeholders, not values or a claim that the executable is on PATH.
This guidance does not classify the failure cause or establish cleanup. The
client performs no additional observation, service startup, or retry. Existing
exit codes, result schemas, receipt handling, renewal, and cleanup behavior
remain unchanged.

Same-Session continuation may check and retain an exact healthy binding.
Cross-Session handoff ends the old lease and acquires a new one; no implicit
owner transfer. Revalidate installed artifact and application state as required
by the project procedure.

## Limits and effects

This is cooperative ownership, not an atomic Step fence or OS sandbox.
Native `running` includes drain. Polling may miss short pause/resume windows;
stuck native running state can keep renewal active. Wrapper death stops its
heartbeat but may leave a descendant capable of direct device access.
CLI expiry cannot fence an arbitrary stale direct ADB caller from a reused
serial. Report unresolved work honestly; do not promise otherwise.

Installation, emulator startup, credentials, workflow activation and publication
require their own applicable authority. Source synchronization does not refresh
locked Session instructions. New consumers must use a compatible fresh context;
old active consumers retain their original adapter and pool boundary.
