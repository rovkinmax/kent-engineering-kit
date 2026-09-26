# Objective

Review `$ARGUMENTS` independently against repository standards and intended
behavior.

# Preparation

1. Resolve and pin the comparison baseline.
2. Verify the diff is non-empty.
3. Find the governing specification, task, plan, or acceptance criteria.
4. Find applicable `AGENTS.md`, architecture, and coding-standard sources.

# Parallel Review

Resolve reviewer callability and tool permissions from the effective project
and global configuration before starting. If two callable, read-only
project-specialized roles can cover both axes, assign one to each. Otherwise,
run two separate `researcher` sessions, each with its corresponding
`agents/standards-reviewer.md` or `agents/spec-reviewer.md` contract. Use two
independent sessions in parallel:

- Standards: repository rules, architecture, maintainability, and regression
  risks.
- Specification fidelity: missing requirements, incorrect behavior, and scope
  creep.

Give each session the exact baseline, diff command, commit list, relevant
sources, and its applicable review contract. Prohibit edits, external effects,
and further delegation. Do not invoke workflow-only roles that are not
callable, change configuration or permissions to enable them, or duplicate a
review pass owned by an active generated Delivery workflow. If effective
policy cannot support both independent read-only reviews, report the capability
blocker and do not claim the review is complete.

# Synthesis

Present the two reports separately. Do not collapse findings into one ranking;
a change may pass one axis and fail the other.

End with:

- finding count per axis;
- missing verification;
- unresolved product questions;
- the smallest recommended next step.
