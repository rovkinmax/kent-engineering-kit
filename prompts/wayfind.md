# Objective

Map a large or uncertain effort described by `$ARGUMENTS` without prematurely
turning unknowns into implementation tasks.

# Method

Load the canonical
`${KENT_PERSISTENCE_ROOT:-$HOME/.kent}/skills/engineering-methods/SKILL.md`
through its installer symlink, then follow only `references/wayfind.md`
relative to that skill directory and apply it to this prompt's objective and
request. This prompt explicitly selects Wayfinding; if its binding or reference
is missing, foreign, or unreadable, report the gap without substituting another
method or copy.
