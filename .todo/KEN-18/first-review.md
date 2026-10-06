# KEN-18 first preview review receipt

Preview: `.todo/KEN-18/plan.md`.
Raw SHA-256:
`b841dd0be0bc1365337849577e706e67552be5e3b2fb528a8fb9541eceae4cec`.
Reviewer: architecture-designer Session
`a78c41dc-eb69-480a-9bc2-ec67901e8e54`.
Verdict: PASS, 2026-10-03.
Original complete review: final output in that Kent Session; independent
read-only review, no children or edits. Hash rechecked at end of review.

This is the first review only. Separate Plan Review must independently PASS
the same raw preview bytes before human approval. No approval is recorded.
Raw preview hash is distinct from implementation's normalized contract digest.
Do not mutate the frozen preview to update its checkbox progress.

## Findings and dispositions

No blocking preview defects. Review confirmed four human decisions via native
Question history and native first/retained assignment semantics from pinned
Kent v2.8.0 source. Preconditions are not claimed complete:
exact model availability/budgets, actual retained settings readback and
deterministic first/continuation qualification remain writer-owned pre-edit
checks. Unknown capability blocks; no runtime patch or model substitution.

Nonblocking implementation details:

- Changed authoritative templates require existing schema-3 reviewed update
  synchronization before bootstrap/parity. Bootstrap alone uses update=False
  and will reject changed copies. Do not delete, manually edit or bypass copies.
- Keep raw review binding separate from normalized accepted-plan digest.
- Assert adapter outcomes, including disabled-opt-in regression coverage,
  not just selector fields in graph JSON.

Readiness inventory required by preview step 7 is explicitly Kit, Appsome,
Puber and product SDK. An ineligible or unchanged consumer is a documented
outcome, not authorization to edit it. SDK primary contains unrelated dirty
changes and remains a preservation boundary. Integration target observations
are moving inputs, not immutable baseline replacements.

## Plan-stage checklist

- [x] Draft, explicit scope, dependency inventory and four human decisions.
- [x] Bounded grill and dispositions in frozen preview.
- [x] Freeze raw preview hash.
- [x] First independent same-hash PASS retained.
- [x] Append Plan ledger event: sequence 1,
  `f52d9c44ee630b10832e743a171a59d5106bd0bea2dbdc649eb722cffdd165cf`.
- [x] Prepare exact preview/receipt handoff to separate Plan Review; native
  task transition record is authoritative for successful delivery.

Production files remain unchanged. No tests/full validator/canary run during
Plan; this receipt proves reviewed planning, not implementation acceptance.

## Instruction-read bookkeeping correction

The append-only sequence-1 event omitted two conditionally relevant instruction
files and misplaced the first workflow-contract read. Preserve that event
unchanged; its per-run idempotency forbids manufacturing another run identity.
The actual project instruction read order, excluding the manifest, is:
`AGENTS.md`, `.kent/project-contract.md`, `.kent/workflow-profile.toml`,
`.kent/commands/plan.md`, `contracts/workflow-contract.md`,
`docs/MODEL-POLICY.md`, `contracts/plan-contract.md`, `README.md`,
`docs/SOURCES.md`. Workflow-contract initial inspection was targeted search;
some later reads were repeated/narrowed after truncated tool output.
External skill guidance is not a repository-relative ledger instruction file.
This correction changes bookkeeping only, not frozen preview or review scope.
