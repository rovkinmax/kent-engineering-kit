# Objective

Stress-test and clarify `$ARGUMENTS` until the user and agent share one precise
understanding.

# Method

- Inspect the environment for facts instead of asking the user to retrieve them.
- Ask only about decisions, preferences, trade-offs, and inaccessible context.
- Ask exactly one decision question at a time.
- With every question, include concrete options and a recommended answer.
- Resolve prerequisite decisions before dependent ones.
- Record durable terminology or architecture decisions through the project's
  declared domain documentation when appropriate.
- Do not implement the result until the user confirms that the decision tree is
  sufficiently resolved.

# Output

At completion, summarize:

- agreed outcome;
- key decisions and reasons;
- remaining unknowns;
- out-of-scope items;
- recommended next action.
