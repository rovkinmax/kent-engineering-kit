# Objective

Resolve ambiguous domain terminology or a durable architectural decision in
`$ARGUMENTS`.

# Process

- Inspect existing domain docs and code usage.
- Surface contradictions between product language and implementation.
- Test definitions with concrete edge-case scenarios.
- Ask one terminology or trade-off decision at a time.
- Update the repository's glossary only after the user chooses a canonical term.
- Offer an ADR only for decisions that are expensive to reverse, surprising
  without context, and based on a genuine trade-off.

Keep glossary definitions free of implementation details.
