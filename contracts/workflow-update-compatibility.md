# Execution-history and compatibility policy

## Source-only admission candidates

`delivery-source-admission-v2` keeps its closed binding variants. An
`r2-installed` binding may additionally declare `receipt_workflow_id`, a
canonical Workflow UUID matching the receipt in the exact qualified project
installation binding. Without this field the target Workflow must own the
receipt. The outer frame's project, target Workflow, ingress edge and native
identity remain independently exact. Only the source inspector uses the receipt
owner; it does not grant cross-project/root/profile/digest substitution.

Trusted installed ingress verifies native identity and installed binding/profile,
selects task-fixed mode, and for R2 admits source before initialization effects.
Inventoried legacy Tasks retain their original infrastructure and accepted
contracts. `plan-publication` remains source-only and cannot initialize R2 or
write documents; final generators requiring R2 reject it. Offline candidates
remain explicitly UNACTIVATED until qualified publication/installation and a
separately authorized compatible rollout. Admission cannot be bypassed by
fetching/provisioning/retargeting; later explicitly authorized operations remain
available after successful admission.

For captured graphs without a tracked ingress-header owner, use
`operations.render_source_ingress_candidate(capture, ingress=..., admission=...,
reviewed_prefix=..., reviewed_prefix_sha256=..., qualified_target=...)`.
The complete capture contains `workflow_id`, `expected_version`, `graph` (all
four native collections), and `execution_target_policy`; this replacement
renderer accepts only Agent ingress. Existing Script admission is unchanged.
The target contains
exact `commit_oid` and `tree_oid`. The facade checks the admitted source commit
and actual Git tree, then calls the bounded pure shared renderer. It replaces
only the exact reviewed authority prefix and selector, preserving Plan body,
identities, carriers, context modes and approvals. It rejects unknown prefixes,
duplicate frames, entry bypass and contradictory commit/tree authority.
Its `KENT_QUALIFIED_INITIAL_SOURCE_V1` header is also checked against the
canonical reconciliation target, actual commit/tree and fresh admitted source
commit. Historical headers and unrelated head/default
selectors are not retroactively constrained. Returned source candidates are
ordinary reconciliation inputs, never apply/installation authority.

CI recovery consumers using optional-report `delivery_context` cannot receive
an old retained producer that emits only flat packets. Qualify the exact
producer/consumer cohort, retained instructions, carried values and pending
decisions before rollout; source tests alone do not establish runtime
compatibility or authorize Task migration.

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

The native observer may read frozen approval branches whose parameter
descriptions differ from the current graph. This exception requires identical
list length/order, object key sets, parameter keys/purposes and every other
value, with string descriptions present on both sides. Unknown representations
remain exact-only. It does not classify the difference as cosmetic or safe.
All five original raw JSON columns, including description bytes and whitespace,
remain bound to the approval envelope and fingerprint without normalization.

Native approval context dispatch is closed and bound to matching frozen/current
edge mode and target kind. A `new_session` edge targeting an Agent requires
exactly `target_session={"kind":"create"}` and
`active_source={"kind":"deferred_self"}`, without Session IDs or extra keys.
`continue_session` and `compact_and_continue_session` retain exact-source and
same-Session reuse. Cross-pairs and reuse for `new_session` are rejected.
This representation support does not consume pending decisions or establish
compatibility, installation authority, or runtime cohort qualification.

Observation collects affected decision identities only in the current call
chain, comparing frozen/current/target descriptions; it adds no persisted
snapshot, plan or review schema. Each affected decision requires an existing
`decision:<id>` compatibility disposition with exact evidence explaining the
description change, actual materialized payload, retained producer and future
consumer compatibility. Target-only changes and targets restoring frozen text
still require the affected-decision review. Unaffected decisions acquire no
additional review requirement. An expansion from `delivery_context v1` to
`v1/v2` does not convert carried values or authorize an old decision to use v2.

Prepare and each fresh new-effect dispatch check enforce these dispositions
alongside the existing graph, snapshot, source and review freshness gates,
including the final native process-release check. Already-attempted effect
settlement remains read-only reconciliation without this new review gate or
replay. Final readback uses description-aware observation with the same exact
raw approval fingerprints. Reading evidence alone grants no mutation authority.

Approval is not compatibility. Never answer, replace or invalidate pending
approvals as a side effect. Execution-target metadata needs
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

`canonical-workflow-reconcile-plan-v2` admits one task-backed/linked Workflow
with digest-confirmed compatibility. Preserve Workflow, task, Session, node,
edge and transition-group identities; do not delete rows, reassign retained
execution, or change node kinds. A graph delta may add source-qualified Script
nodes and the exact connected edges/groups required by their routes. Added
Agent, Start, Join, terminal and node-group rows remain unsupported. Every
changed existing graph field has its own reviewed path and evidence
disposition, including keys, prompts, parameters, transition IDs, endpoints,
execution fields and approval flags. Added rows use `graph.added_node:<id>`,
`graph.added_edge:<id>` and `graph.added_transition_group:<id>` paths.

The source-admission frame is bound to one exact native ingress edge and
target kind. Agent and Script ingress variants are closed; the selected entry
route must not have a sibling route that bypasses admission. A v2 frame carries
the complete `{project_id, workflow_id, source_admission}` object. A transition
from approval-required to no-approval additionally requires an
`authority:graph.edges:<id>.requires_approval` disposition and dispositions
for pending decision identities. These review requirements never consume,
answer or invalidate a decision.

For prompt-free Script ingress, the exact ingress transition group's
description is canonical closed JSON with `schema` equal to
`delivery-script-ingress-ref-v1`, an absolute `path`, and `sha256`.
The reference is at most 1000 characters; its document is at most 1 MiB and
contains exactly the complete v2 frame above. The document is exclusively
created mode0600 in a private mode0700 persistent rollout directory. Reading
requires owner, regular-file, single-link, no-symlink, stable-byte and digest
checks. Retain the document while any native graph or retained execution
references it. The reviewed target hash binds the reference and the operator
compares its document with the exact reviewed admission.

Runtime reads this carrier only through genuine native Task/Script/graph
identity and the exact sole ingress edge. The Script edge has no prompt.
Legacy edge-prompt carriers remain an exclusive compatibility variant;
dual carriers or failed reference parsing never fall back to that variant.
Agent ingress is unchanged.

Native graph source projection is applied before target hashing and review. It
removes only validated matching Workflow annotations and source encodings of
native defaults (configured selection, immediate-source new-session context
and empty parameters). It is not applied to native post-effect readback.
Admission v1 remains unchanged. Admission v2 supports either an explicit
`r2-installed` binding or a `plan-publication` binding. The latter validates a
closed `delivery-source-publication-v1` receipt and published source closure;
it does not use `project_memory`, require an R2 profile binding, initialize a
Task, or create a legacy storage fallback.

No migration or cross-project atomicity is implied. Retirement remains a
separate authorized effect. Publication does not authorize activation.

Before recovery, settle an attempted effect with read-only pre/poststate
evidence. Missing pending payload is unknown, not empty. Fresh compatible
admission is required before a new effect; missing mutable bookkeeping does
not prevent reporting an independently proven settled result. A forward
restore must satisfy current source admission and compatibility, not merely
reproduce previously approved bytes.
