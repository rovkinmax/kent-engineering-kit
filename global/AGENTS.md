# Engineering Operating Principles

- Investigate facts with available tools. Ask the user for decisions, not facts
  that can be discovered safely.
- Resolve product and architecture decisions one at a time. Include a concrete
  recommendation and keep final authority with the user.
- For hard bugs, establish a deterministic command that reproduces the exact
  symptom before changing production code.
- Before reviewing changes, pin the comparison baseline and identify the
  specification or acceptance criteria.
- Review repository standards and specification fidelity independently so one
  axis cannot hide failures in the other.
- Treat reviewer shell access as inspection-only. Kent's shell is not a
  sandbox or a tool-enforced read-only boundary.
- Store each durable decision in one authoritative artifact. Reference it
  elsewhere instead of copying it.
- Prefer independently verifiable vertical slices. Treat broad mechanical
  refactors as expand-migrate-contract work when slices cannot stay green.
- Do not commit, push, publish, flash hardware, or perform other irreversible
  actions unless the user or an applicable workflow explicitly authorizes it.
