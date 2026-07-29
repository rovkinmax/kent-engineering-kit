# Mobile Smoke Contract

This contract separates safe inspection of a test application from actions
that require explicit user authorization.

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

Local UI navigation is not an external side effect. If activating an action
could mutate account or server state, use a non-mutating observation or a
deterministic test instead, or request a scoped exception.

Capturing a focused screenshot for validation does not require user
authorization. On a declared stage/test environment, retaining it as a scoped,
audited task artifact also requires no additional approval. Publishing it
outside the workflow evidence boundary or committing it to source control
requires the normal authorization for that external action.

## Interaction Proof

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
  shared resource.
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
  not ask again.
