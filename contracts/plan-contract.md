# Plan Review And Contract Revalidation

Generated Engineering Delivery independently reviews a plan before the first
writer. The read-only review checks source authority, product decisions,
acceptance coverage, evidence ownership, dependency boundaries, and executable
step ordering. Unsupported decisions return to the retained Plan session
before code changes.

After review, the deterministic Plan Contract Guard stores an ignored
normalized snapshot and digest. Checkbox state is implementation progress and
is excluded from normalization; all other plan content remains contract data.

Every Implement continuation and transition to verification passes through the
guard. An unchanged normalized plan resumes its intended route. A material
change enters Plan Revalidation in the retained planning context, then repeats
the independent review before accepting the new snapshot.

Operational feedback that selects an execution detail inside the accepted
contract stays in the active node. Feedback that changes requirements,
architecture, acceptance criteria, safety boundaries, or planned evidence
requires revalidation.

The repository digest detects artifact drift. It does not provide task-comment
ordering, a monotonic authority revision, or atomic transition compare-and-swap.
Those require Kent runtime support and must not be inferred from private
database rows or fragile CLI text.
