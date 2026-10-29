# Kit Source Development

## Applicability and authority

Use this instruction only when the active native Workflow explicitly selects
Kit self-development. It does not select a route, declare native topology or
authorize an effect. The selected Workflow and project procedure own stage
order and result requirements. Follow the installed Kit skill's source-binding
instructions to read the authoritative `contracts/kit-change-policy.md` from
the Kit checkout; do not copy its classification rules here.

## Scope and implementation

Classify the proposed source change using the policy before editing. The
authorized scope owner identifies the established behavior, exact files and
checks. Mixed, sensitive or unknown changes require the policy's strong route.
Do not infer eligibility from a task label, path, diff size or passing test.

One writer owns the approved source set. Inspect existing staged, unstaged and
untracked work before editing; preserve unrelated changes and distinguish
inherited state from this slice. For a defect, establish a deterministic
reproducer or failing regression before changing production behavior. Inspect
actual callers and test the supported interface they use.

Use the repository's existing tests and verification procedures. Select checks
from the actual changed behavior, callers and accepted scope; report commands
and results that really ran. Do not add a general-purpose runner, test
registry, or substitute suite merely to avoid existing project checks. Follow
the active project profile and procedure for any full verification requirement.

## Candidate freshness and independent review

The final reviewer examines the actual current candidate, relevant callers,
tests and verification configuration, including applicable staged, unstaged
and untracked files. The review and tests must refer to the same candidate.
After a material source, test, verification-input or scope change, rerun the
affected checks and obtain fresh independent review. Do not reuse stale review
or test results.

Treat source-candidate freshness as a review obligation only. Consumer runtime
selection and compatibility remain governed by the authoritative policy and
the applicable project; a source review does not change either.

## Effect boundary

Source approval alone does not authorize commit, push, publication, PR
creation, installation, native Workflow activation, Task migration or cleanup.
Each effect requires its own exact authority, compatible owner and applicable
procedure. Editing source does not change installed instructions, live Kent
topology, current Task state or retained Session instructions.
