# Kit Source Change Policy

## Purpose and boundary

This policy governs changes to the Kent Engineering Kit's own source. It does
not govern product implementation in a consuming project, change a live Kent
Workflow, or authorize publication, installation, Task migration, or another
external effect. The selected project profile and Workflow remain the
execution authority.

Use this policy only when an authorized Kit-development execution explicitly
selects it. It does not select or activate a route, retroactively reclassify a
current Task, or waive the requirements of a different selected Workflow.

The policy provides a bounded route for compatible fixes without weakening the
stronger review required for changes to behavior, authority, data, or delivery
contracts. Product Engineering's Plan Review, Standards or Specification
review, synthesis Gate, conditional Smoke, and Compliance remain unchanged.

Review freshness binds a reviewer to the actual Kit source candidate and the
checks performed on it. It is not a consumer-Task version pin or admission
check. Consumer Tasks use the Kit selected by their project/runtime; do not
require a per-Task Kit version or hash, or block work solely because of
whole-Kit drift. Compatible updates do not require all consumers to drain.
Identify concrete breaking interfaces and effects and use their applicable
governance and rollout boundaries.

## Classification

A change is a **compatible fix** only when all of the following are supported
by established behavior, relevant callers, and tests:

- It restores an already-established intended Kit-tool invariant; it does not
  introduce a new capability or redefine accepted behavior.
- Supported CLI, API, JSON, exit-code, environment, and invocation contracts
  remain unchanged.
- Persisted data keeps the same interpretation and compatibility requirements.
- Permissions, authority, external effects, effect ordering, retry safety, and
  recovery behavior remain unchanged.
- Workflow stages, routes, approval requirements, parameters, and retained
  execution compatibility remain unchanged.
- Installation boundaries and runtime or source selection remain unchanged.
- Verification and review obligations remain unchanged.

A non-normative documentation correction may use this route only when it
clarifies an already-established invariant without changing policy, process,
eligibility, or obligations.

Changes to this policy or its classification rules, review/test/CI obligations,
workflow stages/routes/approvals, agent-role responsibilities/tools/delegation/
authority, Kent configuration, supported interfaces, persisted-data
interpretation, effects or retry/recovery safety, and installation/runtime
selection are **sensitive**.
A change that combines compatible and sensitive behavior, or whose
classification is unknown, is also sensitive. The authorized scope owner may
identify a genuine independent split; each slice still requires its applicable
authorization. Do not split work merely to bypass the strong route.

Eligibility is semantic. A bug-fix label, small diff, selected file path, green
test, or author's assertion cannot establish it. There is no general-purpose
automatic semantic classifier.

## Compatible-fix route

The authorized scope owner records the established invariant and its
supporting source, the bounded change, and relevant regression checks before
implementation. One writer owns the approved source set.

The writer runs focused regression tests and relevant project checks. An
independent read-only reviewer assesses the actual full candidate against the
invariant, compatibility boundaries, relevant callers, and tests. The
configured CI must also pass. A compatible fix does not require two
preliminary preview reviews or an additional human approval solely because it
changes Kit source. Existing scope and effect-specific approvals still apply.

The final review covers relevant staged, unstaged, and untracked changes,
verification configuration, and caller surfaces; HEAD alone is not the
candidate. Tests and review must refer to the same current candidate. A
material change to source, tests, verification inputs, or scope after review
requires fresh relevant tests and independent review. Do not relabel or reuse
stale PASS evidence.

If implementation or final review discovers that the candidate changes
sensitive behavior, stop and return it to the strong route before further
implementation or effects. Final review is not retroactive permission.

## Strong route

Sensitive, mixed, or unknown changes require investigation and one bounded
preview naming affected files, behavior or graph delta, tests, rollout,
rollback, and restart impact. Obtain two independent read-only PASS reviews of
the same preview hash and explicit human approval of that scope before
implementation or effects. The final candidate still requires relevant tests,
independent review, and configured CI.

Any material scope or effect change requires renewed review and approval before
the affected work proceeds. Preserve the immutable baseline and actual
decision source. Agent summaries, issue labels, green tests, and a reviewer's
preference are not human authority.

## Evidence and limits

Use the existing Task, Session, Workflow, and project evidence channels when
they apply. Retain the source of the established invariant, authorized scope,
candidate under review, relevant test/CI results, and genuine reviewer
identity. Do not invent producer identity or replace an original human
decision.

This policy creates no universal classification service, permission ledger,
receipt schema, or hash chain. Tests may prove explicit routing and modeled
invalid cases; they do not prove semantic eligibility for arbitrary changes.
Classification remains a review obligation. Unknown behavior or evidence
takes the strong route.

Publication, PR creation, push, merge, installation, Workflow activation, and
Task migration remain separate effects with their own authority. A source PASS
does not imply any of them.
