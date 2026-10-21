# Objective

Stress-test and clarify `$ARGUMENTS` until the user and agent share one precise
understanding.

# Method

- Inspect the environment for facts instead of asking the user to retrieve them.
- Ask only about decisions, preferences, trade-offs, and inaccessible context.
- Ask exactly one decision question at a time.
- With every question, include concrete options and a recommended answer.
- Resolve prerequisite decisions before dependent ones.
- Separate the user's required outcome from implementation options proposed by
  agents. Do not turn an earlier suggestion into a mandatory requirement without
  its original acceptance source.
- Recover applicable decisions from original messages or Question answers
  before asking again. A missing summary is not a missing decision; conflicting
  or inaccessible sources remain an explicit uncertainty.
- Record durable terminology or architecture decisions through the project's
  declared domain documentation when appropriate.
- For terminology, inspect actual code and domain usage, test definitions with
  edge cases, and record the chosen term without implementation trivia.
- Finish when material decisions are resolved. Implementation follows existing
  scope and approval gates; do not add a universal final confirmation.

# Output

At completion, summarize:

- agreed outcome;
- key decisions and reasons;
- remaining unknowns;
- out-of-scope items;
- recommended next action.
