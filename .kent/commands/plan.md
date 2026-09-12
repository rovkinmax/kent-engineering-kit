# Plan and Independent Preview Review

1. Resolve exact task authority and inspect the selected Kent-managed root.
   Preserve `fixed_point` as the immutable task baseline, not the current
   remote target. Select the profile work kind and mapped procedure.
2. Inspect relevant behavior before proposing changes. Prepare one bounded
   preview naming files, graph delta, rollout, rollback and restart impact;
   link exact human source IDs for any supersession. Keep writer-owned plan
   steps separate from workflow-owned review/verification/delivery stages.
3. Freeze the preview and compute its SHA-256. Inside Plan, delegate exactly
   one independent read-only preview review to an existing callable
   `architecture-designer` or `researcher` leaf. Supply the exact artifact,
   hash, task authority and bounded affected surfaces; forbid edits/effects
   and further delegation. Retain reviewer identity, hash, verdict and
   findings. A BLOCK or FAIL is not a PASS.
4. Resolve findings within scope and refresh that first review as necessary.
   Send the exact preview/hash and first PASS receipt to the separate
   **Plan Review** (`plan_review`) node. That node performs the second
   independent read-only review of the same artifact and verifies the
   first receipt's hash. Do not claim the graph itself computes this binding.
5. Only after both independent reviews PASS on the same preview hash may
   `plan_review_accept` request explicit human approval before Plan Contract.
   No approval before both PASS; no third routine preview review. Preserve
   approval and both receipts with the one authoritative preview.

Revalidation reruns this sequence: refresh the first independent review and
the separate Plan Review, bind both to the revalidated preview hash, then
obtain approval. Material scope changes need a new preview; ordinary fixes
within approved scope and missing agent bookkeeping do not create a new
product decision. Reconstruct missing evidence only when safely bounded and
disclose gaps; never invent old reviews or approvals.

For explicitly authorized report-only qualification, `plan_path=not-applicable`
is valid. Keep preview/review/approval in task evidence without tracked writes.
Do not create a `.todo` artifact merely to satisfy a plan-path field.
Retain report-only previews, reviews and requests in Task records or the
ignored `build/kent-workflow/<task>/` area, never as arbitrary files under
the restricted `.kent/runtime/<task>/` directory. Preserve the original
preview ScopeHash separately from Plan Contract's normalized-plan digest.

Use generated transition parameters and evidence commands, not custom
control files. User-facing approvals/blockers are concise Russian decisions;
repository artifacts remain English. Source approval never implies Git/live
workflow/consumer/installation effects.
