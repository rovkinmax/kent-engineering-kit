# Shared Engineering Stage Responsibilities

This reference describes stage responsibilities, not a workflow definition.
Read only the section selected by the active native Workflow prompt. That
prompt and its project context own the actual stage order, scope, inputs and
result obligations. Kent owns native topology and parameters; project
instructions own product requirements and commands.

These links locate existing role responsibilities; they are not a bundled
instruction set and do not select roles or import their route-specific
transport, evidence or effect protocols. Apply a role's detailed instructions
only when the active native Workflow selects that role and those instructions
are compatible with the selected route and higher-priority rules. If the role
and selected route have conflicting obligations, do not infer a waiver or
silently choose one; block the affected responsibility until compatibility is
resolved separately.

[Specification reviewer](../../../agents/spec-reviewer.md),
[Standards reviewer](../../../agents/standards-reviewer.md),
[Compliance reviewer](../../../agents/compliance_reviewer.md),
[implementation worker](../../../agents/implementation-worker.md),
[Fix worker](../../../agents/fix-worker.md),
[Workflow Gate](../../../agents/workflow-gate.md),
[runtime tester](../../../agents/runtime-smoke-tester.md),
[CI observer](../../../agents/ci-monitor.md), or
[delivery operator](../../../agents/delivery-operator.md). This stage summary
does not replace or copy those full prompts.

## Plan

Clarify the requested outcome, governing requirements, immutable baseline,
affected callers, acceptance criteria, risks, effects and verification needs.
Separate requirements from proposed implementation means. Record unresolved
decisions and bounded scope for review; do not begin implementation before the
active workflow permits it.

## Independent Plan Review

Review the exact current plan against the original request, decisions,
requirements and relevant source facts. Check scope, assumptions,
compatibility, verification sufficiency, rollout and rollback where relevant.
Remain independent and read-only. Report contradictions, missing proof and
later obligations with an owner; do not silently approve a changed plan or
convert findings into implementation scope.

## Implementation and Fix

One writer owns the authorized file set. Preserve unrelated and inherited work,
use the immutable baseline for task-delta claims, and reproduce reported defects
before production changes when applicable. Implement only accepted scope. Run
affected regression checks and relevant caller-shaped verification. A
substantive scope or requirement change returns to the active scope owner.
Keep remaining review, runtime and delivery work with its assigned owner.

## Independent Implementation Review

Inspect the actual current candidate and relevant caller, test and
verification surfaces; the writer's summary or commit alone is not the
candidate. Review only the assigned perspective and remain read-only.

- A specification review checks the request, decisions and accepted plan.
- A standards review checks repository rules, architecture, correctness and
  regression risks.
- A runtime review checks only the approved runtime behavior and evidence
  within its selected scope.

Report confirmed defects separately from missing stage-required proof and
non-blocking preferences. Findings return to the authorized correction owner;
reviewers do not edit source or expand scope.

## Conditional Compliance Review

When selected, compliance review checks direct conformance to applicable
instructions, specifications and accepted decisions against their original
sources and current evidence. Do not duplicate Standards, Specification or
runtime review, or certify effects assigned to a later stage. See the
[compliance reviewer](../../../agents/compliance_reviewer.md) instructions.

## Synthesis

Synthesize current results from the stages the active Workflow enables. Keep
failed, blocked, stale and unknown outcomes distinct; do not relabel old
evidence or substitute one review perspective for another. Identify the
capable owner for each remaining obligation and preserve it for the correct
later stage.

## Learning and Improvement

Writers and reviewers distinguish source-verified reusable findings from
hypotheses and improvement proposals. Persist verified knowledge only through
paths already authorized by the active project and procedure; keep proposals
distinct and use the existing decision route. This reference grants no new
storage, Task-creation or Task-start authority, or rollout/effect authority.

## Conditional Runtime Checks

Perform runtime checks only when the active Workflow and approved scope require
them. Follow the project's declared platform, resource, identity and evidence
procedures. Exercise only the selected behavior, settle owned effects and
resources, and report unavailable prerequisites as blockers rather than
passing results. This section grants no build, install, launch, account or
device authority.

## Delivery Observation

Observe publication, pull-request, CI or merge state only when assigned and
after the authorized owner has completed the relevant effect. Verify current
identity and provider state; pending is waiting, and unknown or stale state is
not success. Do not publish, push, merge, infer an effect from Task status, or
expand the implementation diff.

## Cleanup

Perform cleanup only when the active Workflow assigns a qualified owner and
the exact disposition authority and resource ownership are established.
Preserve dirty, foreign, ambiguous or unresolved resources and effects. Follow
the project's supported cleanup procedure; this reference does not authorize
Task movement, worktree deletion or any other external effect.
