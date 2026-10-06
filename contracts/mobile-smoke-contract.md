# Mobile Smoke Contract

This contract separates safe inspection of a test application from actions
that require explicit user authorization.

## Installation And App-Data Safety

- Distinguish three operations explicitly:
  - **fresh binary**: build the current task artifact;
  - **preserve app data**: replace the installed binary in place with a
    compatible signer and non-downgrade version;
  - **destructive reset**: uninstall, clear package data, downgrade, or replace
    an incompatible signer.
- A normal Smoke run may build a fresh binary and use the project install
  adapter's preservation-only operation. It must not interpret "fresh APK" as
  permission to create a fresh app profile.
- `adb uninstall`, `pm clear`, install with downgrade permission, simulator
  erase, clean install, or any equivalent destructive reset requires a
  separate explicit authorization naming that action. A general authorization
  to test, navigate, sign in, or exercise an external flow does not imply it.
- The deterministic Android install adapter inspects the candidate and
  installed package, then uses preservation-only `adb install -r` for
  compatible or absent packages. It adds `-t` only for manifest-declared
  instrumentation or test-only APKs. A valid instrumentation manifest may omit
  versionCode and versionName: an absent minor code with an absent or zero
  major code uses Android's effective versionCode zero and an absent name is
  reported as null. Malformed or ambiguous metadata is never defaulted.
  Existing packages require a known installed version and compatible signer;
  downgrade, unknown version, unknown or mismatched signer, transport failure,
  and install failure block replacement. It never deletes package data,
  grants downgrade permission, or retries destructively.
- Before install, record only the app's authentication state as
  `authenticated`, `unauthenticated`, or `unknown`. Record the same state after
  launch. Never store credential values, authenticated content, or secret
  material in checkpoints, comments, reports, screenshots, or evidence.
- A resumed Smoke session reconciles its checkpoint before rebuilding or
  reinstalling. When the checkpoint proves the same APK digest was installed
  successfully and the preserved flow remains authenticated, skip duplicate
  build/install and continue from the next bounded scenario.

## Default Authorization

On an acquired test emulator or simulator, focused runtime Smoke may inspect
and navigate an already-authenticated application UI without asking merely
because the application is logged in.

Allowed by default:

- bounded semantic inspection needed to locate the task's target;
- task-scoped screenshots or visual inspection when semantics are
  insufficient. On a project-declared non-production stage/test environment
  with synthetic data, screenshots may be retained in the ignored evidence
  directory without another user question. Limit capture to the relevant app
  screen or control and use a reduced resolution or crop when available;
- focus movement, scrolling, Back, and opening or closing screens, dialogs,
  drawers, and menus;
- reversible local preference or settings changes required by the declared
  scenario, when the original state is captured and restored;
- package-scoped liveness, crash, and ANR checks.

These actions must remain inside the task's declared Smoke scope. Open-ended
exploration is not authorized.

## Explicit Authorization Required

Unless the task body or a durable task comment explicitly authorizes an
exception, Smoke must not:

- persist screenshots from production, an environment whose non-production
  status cannot be established, a physical device, another app, or a screen
  containing credentials or secrets;
- persist broad or raw UI trees, full device logs, network payloads, or
  authentication material;
- perform account-, server-, purchase-, subscription-, playback-progress-, or
  other externally observable state changes;
- enter credentials, approve MFA, change permissions, or provision secrets;
- use a physical device or start an additional emulator.
- uninstall an app, clear its data, allow a package downgrade, erase a
  simulator, or perform another destructive reset.

Local UI navigation is not an external side effect. If activating an action
could mutate account or server state, use a non-mutating observation or a
deterministic test instead, or request a scoped exception.

Capturing a focused screenshot for validation does not require user
authorization. On a declared stage/test environment, retaining it as a scoped,
audited task artifact also requires no additional approval. Publishing it
outside the workflow evidence boundary or committing it to source control
requires the normal authorization for that external action.

## MCP Outcome Evidence

This gate applies only when a Smoke claim relies on the safe result from
`kent-mcp-call`; it does not replace the existing authorized visual or other
bounded observation paths.

- Safe modes other than `--quiet` emit `schema: kent-mcp-result-v1` with
  separate `transport`, `processing`, `action`, `assertion`, and
  `assertionKind` outcomes. Require a valid complete outcome object, successful
  transport, and successful processing before considering its assertion.
- `action` is always `unknown` in this adapter contract. Do not infer action
  completion, navigation, or authentication from process exit, a successful
  parse, a digest, extracted hashes, marker booleans, literal checks, or agent
  narrative.
- Promote an authenticated or passed checkpoint from this result only when a
  task's known provider/state schema supports a `json_boolean` assertion that
  passed for the intended destination/state, the caller can account for all
  schema-defined error conditions, and the required interaction/navigation
  evidence is present. The consumer must know what the selected boolean means;
  an arbitrary true field is not provider-success proof. If the schema or its
  error conditions cannot be established or expressed, keep the state unknown.
- `--assert-json-true` selects exact JSON boolean values. False is a failed
  assertion; missing, null, or a non-boolean value is unknown. Per-selector
  results contain only fixed outcome enums. Do not persist or echo the response
  body, selected values, pointers, raw parse errors, or provider stderr to
  resolve ambiguity. This adapter does not guess fields such as `ok` or
  `error`.
- A negative destination assertion does not by itself prove that the app is
  unauthenticated or identify the screen currently shown. A positive state
  assertion can support a checkpoint only under the known-schema and
  interaction requirements above.
- `--quiet` remains empty and process-exit-only. An assertion based on literal
  text is byte-presence evidence, not semantic state evidence. Digest and
  extraction results, including false or mixed `markersPresent`, describe
  processing or observation only and cannot establish authentication or pass a
  checkpoint. If another authorized bounded observation path supplies the
  required positive evidence, it remains usable with the existing interaction
  proof rules.
- Parsing, literal assertion, and extraction inputs are limited to 1 MiB;
  selector/predicate count and size and extracted-hash count are bounded by the
  adapter contract. These interpretation bounds do not limit the initial
  temporary stdout/stderr capture by the tool process and must not be described
  as bounded transport storage.

## Interaction Proof

- Apply the project/task form-factor constraint before locking a runtime target.
  Never send a mixed phone/TV/watch/automotive candidate list to
  `acquire-any`. Resolve eligible serials deterministically, acquire one exact
  serial, and verify its identity after acquisition. If no eligible target is
  available, release any temporary resource and return a blocker.
- Evidence setup is fail-fast. Project-provided evidence commands may produce
  their declared artifacts within the authorized Smoke scope. Manual
  evidence-file creation or editing requires the first-class patch tool
  permitted by the effective instructions. If required manual work cannot be
  completed because that tool is unavailable, report a blocker; do not
  substitute shell writes or shell `apply_patch`, change tool permissions, or
  bypass the restriction. Use `set -euo pipefail` for multi-step shell setup.
  A failed prerequisite must prevent later lock acquisition, installation, or
  input.

- Prefer semantic targeting for control behavior. Prove directional navigation
  separately when D-pad, keyboard, or remote focus behavior is itself in scope.
- Establish the starting focus and inspect the relevant UI source or semantic
  node ordering. When focus order and conditional visibility are known, derive
  the exact bounded route to the target, execute it in one deterministic call,
  and verify the destination once.
- If the computed route does not reach the expected target, inspect the new
  focus and replan. Use small adaptive bursts when an exact route cannot be
  derived, and single-step checks only near the target or after unexpected
  movement.
- Group a deterministic input burst and its focus observation into one tool or
  shell call. Do not require a model round-trip between individual key events.
- Do not locate a control by sending a fixed blind loop of D-pad, keyboard, or
  remote-control events.
- Before activating a mutable control, confirm its identity and focus and
  capture its original state with the narrowest safe observation available.
- After activation, prove the control state changed and prove the intended
  local effect. A visible label or screen alone is not evidence that a toggle,
  checkbox, picker, or preference changed.
- When accessibility semantics do not expose mutable state, use the allowed
  bounded visual inspection. Do not ask merely because a screenshot is needed.
  On a declared stage/test environment, retain the scoped screenshot when it
  materially supports the decision; otherwise retain only the derived
  assertion. Never retain a broad UI artifact.
- Restore the original state and verify the restoration before releasing the
  shared resource. Every task-started test runner, instrumentation process, app
  process, temporary fixture, and kept-open tool shell/TTY must be terminal or
  restored. Poll and explicitly close every Smoke-owned background shell
  session; an idle shell waiting for stdin still blocks managed-worktree
  cleanup. If a failed or interrupted command leaves task-owned runtime work
  active after release, reacquire or resume the exact resource, clean only that
  task-owned state, record the recovery, and release again.
- If the adapter cannot establish focus, before/after state, or the required
  effect, return a blocker or finding. Do not infer success from the number of
  input events sent or from the Smoke agent's own narrative.
- A user-reported contradiction to the recorded runtime result invalidates the
  affected evidence until a focused rerun resolves it.

## Evidence Allocation

- Before device work, classify acceptance criteria by evidence type. Runtime
  proves rendering, focus, navigation, integration, restoration, and liveness.
  Deterministic tests prove pure defaults, classification, filtering, paging,
  and state-transition logic when those behaviors are not directly observable.
- Use mixed evidence when both categories are present. Do not repeat a passing
  deterministic criterion through runtime unless the task or project contract
  explicitly requires end-to-end proof for that criterion.
- Do not clear an authenticated profile, require a special fixture, or add
  test-only product/accessibility semantics solely to force deterministic
  internals through runtime Smoke.
- If explicit end-to-end proof is required and its fixture or safe semantics are
  unavailable, return the blocker. Otherwise report the runtime and
  deterministic evidence separately and continue.

## Evidence and Recovery

- Keep only the minimum sanitized evidence required for the Smoke decision.
- Retained stage/test screenshots must stay in the project-declared ignored
  evidence directory and pass the evidence audit.
- Every required summary, report, or checklist artifact must be non-empty
  before evidence audit and completion.
- Run the project's evidence audit before reporting success or a blocker.
- An authenticated screen alone is not a blocker.
- Ask only when the required test would cross an explicit-authorization
  boundary or a required external prerequisite is unavailable.
- Do not mark a Smoke checklist item complete or describe Smoke as passed when
  returning `needs_user_action`, `needs_changes`, or any other non-passing
  transition. Kent task/transition state is authoritative over checklist text.
- When the user grants an exception during a task, record its exact scope in a
  durable task comment before continuing so compaction and recovery sessions do
  not ask again. Reuse that authorization for the same account, environment,
  action, and task scope until it is revoked or the scope materially changes.
  Store only the authorization boundary, never credential values.
- Resolve the current Task using the short ID rendered for the Smoke run:
  inspect `summary.id` and `summary.short_id` from
  `kent task show <task-short-id> --json`, and verify the returned short ID.
  Resolve the current Session with `kent session-id`. `KENT_TASK_ID` and
  `KENT_SESSION_ID` are adapter inputs, not assumed Kent exports: set them
  explicitly from this readback. A label, sole lock occupancy, or Session ID
  alone does not establish Task ownership.
- Keep top-level checkpoint `task_short_id` authoritative. Store
  `task_native_id`, `task_short_id`, `lease_owner_id`, `lock_resource`, and
  `lock_token` together in its extensible stage data. A legacy checkpoint
  missing native identity fields may be enriched only after native readback
  matches its existing short ID. Conflicting Task fields block recovery.
  New leases use the verified short ID as `lease_owner_id`. A legacy lease
  written with the native Task ID may be recovered only when readback proves
  that exact native-ID/short-ID pair; retain that owner ID consistently through
  release. Do not silently rewrite foreign ownership.
- Parse lock output according to the command used: `acquire` returns one bare
  token; `acquire-any` returns exactly one `resource=...` record followed by
  one `token=...` record. Reject missing, empty, duplicate, malformed, or
  unexpected records; verify that a selected resource is eligible. Persist
  the exact resource and token in the checkpoint immediately, before any
  device action. Keep the token only in the scoped ignored checkpoint; never
  echo it in reports, comments, or evidence.
- A recovery session reads and validates its checkpoint before acquisition.
  With a recorded resource and token, call `resume` for that exact pair after
  validating the checkpoint Task binding. A failed or mismatched resume blocks;
  do not fall back to resource discovery. With a known resource but missing
  token or lost acquisition output, inspect redacted `status`, prove that the
  exact resource has one valid owner record for the verified Task, then use
  guarded `resume-owned`. It returns the existing token and refreshes metadata;
  it requires the existing lease and valid owner metadata.
- If no resource is persisted, inventory only the project-eligible resource
  list. Recover with `resume-owned` only when exactly one candidate has valid
  metadata for the same verified Task. Zero proven matches proceeds through
  normal fresh acquisition; multiple same-Task matches are ambiguous and block.
  Foreign, malformed, duplicate, or resource-mismatched metadata is never
  ownership proof. A sole occupied resource is not ownership proof.
- Preserve the adapter's caller-TTL policy: a competing lease stays busy until
  its age is greater than the acquiring caller's explicit TTL (`age <= TTL`
  stays busy; replacement requires `age > TTL`). TTL is not stored in the
  owner record. Exact-owner `resume` or `resume-owned` may refresh a still-
  present lease even after its age is greater than an acquiring caller's TTL.
  The first guarded operation wins: resume-first refreshes the lease, while
  replacement-first invalidates recovery with the old owner/token.
  `resume-owned` never creates a missing lock or reclaims a foreign or unknown
  owner.
  After explicit release or trap cleanup, read back `status` for the exact
  resource and require `unlocked`. Failed or locked readback is unresolved
  cleanup, never proof of release. `status` must continue to redact tokens.
