# Execution-history and compatibility policy

Workflow mutation eligibility depends on execution history and the exact delta,
not task presence or a Backlog label. A task proven never started may remain
attached to an approved update of the same Workflow UUID, preserving its task
identity and initial node identity. Terminal tasks retain statuses, history and
anchors. Backlog presence alone does not require cancellation, recreation or
migration; terminal presence alone does not require a new Workflow identity.

Started nonterminal execution requires impact-scoped compatibility evidence;
it does not automatically prohibit updating the same Workflow UUID. This
includes running, queued, paused, approval-waiting, interrupted and historically
started tasks reset to Backlog. The graph is shared: do not assume task-local
frozen snapshots. Preserve each affected task's already accepted scope,
retained instructions, carried values, pending decisions and execution identity.
Explicitly approved prospective behavior changes may be compatible;
compatibility does not require every future behavior to remain identical.

Classify tasks as proven never-started, started nonterminal, terminal or unknown.
Unknown history prevents the never-started shortcut, but is not active execution
and does not automatically block a proven irrelevant or compatible delta.
Block only when relevant compatibility cannot be established. Classification
uses authoritative lifecycle history, execution attempts, retained Sessions,
Script runs, approvals, resolved execution state and resource provenance.
Empty current-runtime lists alone are insufficient. Preassigned targets or
worktrees alone do not prove execution; validate provenance and compatibility.
If supported records cannot establish history, report unknown.

For the exact delta, establish affected tasks and reachable changed paths,
including retries, backedges and pending transitions. Verify compatibility
between retained/in-flight producers and new consumers: transition keys,
parameter meanings, Script outputs, retained Session instructions/settings,
approval payload snapshots and their decision/effect scope, and committed
runtime cohorts. Matching IDs or parameter names alone is insufficient.
Unchanged or unreachable surfaces need only scoped non-impact evidence, not
universal requalification. Optional additive inputs may be compatible; removing
a transition still emitted by a retained Session is not.

Approval does not itself prove compatibility. Do not answer, replace or
invalidate pending approvals as a side effect. Execution-target metadata needs
separate impact assessment: prove what remains pinned for existing execution
and what future Sessions/worktrees resolve, rather than assume isolation.

Eligible updates require separately approved complete atomic graph operations,
preserved identities/anchors, qualified committed runtime closure, a coordinated
stable change boundary covering relevant progress/resume/approval and allocation,
exact pre/post verification and a recovery boundary. A coordinated quiet window
or supported native condition may establish that boundary; graph-version CAS
alone does not freeze task state. Do not force-stop an in-flight Script if
compatibility of its outputs and a valid effect boundary can be established.
If incompatible or unproven, defer the effect or separately approve a supported
isolated-version/migration approach; no automatic reset, resume or migration.
Any later forward restore must be compatible with task state at restore time,
not merely reproduce an old graph.

This policy does not expand any client, driver or plan schema. The generic
client's rejection of task-referenced or linked semantic updates, terminal-only
canonical reconciliation and stricter retirement gates remain implementation
limitations, not claims that never-started tasks are running.
