---
name: engineering-methods
description: Use proactively for uncertain project paths, technical failures, material decisions, architectural alternatives, and assigned change reviews. Select only the relevant method without replacing the active workflow or role contract.
---

Use the method that fits the current bounded question:

- Uncertain multi-stage work: [wayfinding](references/wayfind.md).
- A technical failure: [diagnosis](references/diagnose.md).
- A material decision or trade-off: [decision grilling](references/grill.md).
- A real interface or architecture choice: [design alternatives](references/design-alternatives.md).
- An assigned change review: [change review](references/review-change.md).

Honor an explicitly selected method, including a manual prompt entrypoint.
Do not replace that selection or require a sequence of methods. Read only the
relevant reference. When its purpose is complete, return to the continuing
assignment and its selected procedure. Ordinary errors do not automatically
require replanning.

Keep original user decisions, accepted constraints, pending human questions,
and agent proposals distinct. Recover a decision from its original message or
Question answer when accessible; a locator or summary is not consent. A method
does not convert an agent proposal into a requirement, a question into an
adopted decision, or a recommendation into permission. Preserve the active
role's authority and the selected workflow's review obligations.

## Source binding

Resolve `${KENT_PERSISTENCE_ROOT:-$HOME/.kent}/skills/engineering-methods/SKILL.md`
through its installer symlink. Resolve every reference relative to that
canonical skill directory, not the consumer's working directory or another
copy. If the binding or selected reference is missing, foreign, or unreadable,
block only the dependent method and report the gap; do not fall back to a
lookalike. An isolated qualification root may be used only when it is
explicitly supplied as test context.
