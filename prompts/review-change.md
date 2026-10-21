# Objective

Review `$ARGUMENTS` independently against repository standards and intended
behavior.

# Preparation

1. Resolve and pin the comparison baseline.
2. Establish the requested diff or document scope.
3. Find the governing specification, task, plan, or acceptance criteria.
4. Find applicable `AGENTS.md`, architecture, and coding-standard sources.

# Review

Examine both axes:

- Standards: repository rules, architecture, maintainability, and regression
  risks.
- Specification fidelity: missing requirements, incorrect behavior, and scope
  creep.

Apply the criteria in `agents/standards-reviewer.md` and
`agents/spec-reviewer.md` for both ordinary and delegated review. Resolve those
files from the qualified Kit checkout using the installed
`kent-engineering-kit` skill's source-binding rule, not consumer-relative
lookalikes. If binding fails, report the gap; do not silently substitute criteria.
Borrowing criteria grants none of another role's Session-control permissions.

Ordinary review may cover both axes directly, disclosing its independence
limits. Delegate only when independent sessions are required by governance or
useful for the scope. In that case, check effective callability and tools first:
use qualified read-only specialists, or callable `researcher` sessions with the
corresponding `agents/standards-reviewer.md` or `agents/spec-reviewer.md` contract.

Give each delegated session the exact baseline, diff command, commit list, relevant
sources, and its applicable review contract. Prohibit edits, external effects,
and further delegation. Do not invoke workflow-only roles that are not
callable, change configuration or permissions to enable them, or duplicate a
review pass owned by an active generated Delivery workflow. If effective
policy cannot support required independent reviews, report the capability gap.
For ordinary review, complete the available pass and disclose its independence
limits without manufacturing a new approval requirement.

# Synthesis

For each finding state its trigger, affected behavior, location and differential
evidence after inspecting relevant guards and callers. Cite the governing rule
for a rule violation or the violated behavior for a correctness defect.
Distinguish confirmed defects, missing verification required at this stage and
non-blocking preferences; do not invent confidence scores or finding quotas.

Preserve each axis's result and all applicable rule, requirement and source-report
references. Deduplicate findings sharing the same cause and remedy into a
prioritized synthesis; a change may pass one axis and fail the other.

End with:

- finding count per axis;
- missing verification;
- unresolved product questions;
- the smallest recommended next step.
