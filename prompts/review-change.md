# Objective

Review `$ARGUMENTS` independently against repository standards and intended
behavior.

# Preparation

1. Before consulting shared Kit sources, first read the installed
   `kent-engineering-kit` skill's `SKILL.md`, then follow its source-binding
   instructions to locate and read them. If the skill or required binding is
   unavailable, block only the dependent action, report the gap, and do not
   rely on a substitute.
2. Resolve and pin the comparison baseline.
3. Establish the requested diff or document scope.
4. Find the governing specification, task, plan, or acceptance criteria.
5. Find applicable `AGENTS.md`, architecture, and coding-standard sources.

# Review

Examine both axes:

- Standards: repository rules, architecture, maintainability, and regression
  risks.
- Specification fidelity: missing requirements, incorrect behavior, and scope
  creep.

Apply the criteria in `agents/standards-reviewer.md` and
`agents/spec-reviewer.md` for both ordinary and delegated review from the
qualified Kit checkout. If the skill-defined binding is unavailable, report the
gap; do not silently substitute criteria. Borrowing criteria grants none of
another role's Session-control permissions.

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
