# Objective

Review `$ARGUMENTS` independently against repository standards and intended
behavior.

# Preparation

1. Resolve and pin the comparison baseline.
2. Verify the diff is non-empty.
3. Find the governing specification, task, plan, or acceptance criteria.
4. Find applicable `AGENTS.md`, architecture, and coding-standard sources.

# Parallel Review

Run two read-only Kent roles in parallel:

- `standards-reviewer`: repository rules, architecture, maintainability, and
  regression risks;
- `spec-reviewer`: missing requirements, incorrect behavior, and scope creep.

Give each role the exact baseline, diff command, commit list, and relevant
sources. Do not ask either role to edit files.

# Synthesis

Present the two reports separately. Do not collapse findings into one ranking;
a change may pass one axis and fail the other.

End with:

- finding count per axis;
- missing verification;
- unresolved product questions;
- the smallest recommended next step.
