# Objective

Review `$ARGUMENTS` independently against repository standards and intended
behavior.

# Method

Load the canonical
`${KENT_PERSISTENCE_ROOT:-$HOME/.kent}/skills/engineering-methods/SKILL.md`
through its installer symlink, then follow only `references/review-change.md`
relative to that skill directory and apply it to this prompt's objective and
request. This prompt explicitly selects Change review; if its binding or
reference is missing, foreign, or unreadable, report the gap without
substituting another method or copy.
