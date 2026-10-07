# Role Contract

Role prompts define behavior. Kent configuration defines execution policy.

## Ownership

Project and global role prompts own:

- role purpose and boundaries;
- required inputs and structured outputs;
- inspection, mutation, and delivery constraints;
- project-specific vocabulary and procedures.

Global or project `config.toml` owns:

- model family and variant;
- reasoning level and output verbosity;
- tool availability;
- `agent_callable` and `workflow_subagent`;
- workflow concurrency and subagent depth.

Role prompts must carry their own runtime boundaries. The installer links
agents, prompts, and skills but not this maintainer contract into consumer
workspaces; a role must not require `contracts/role-contract.md` relative to
the current project.

The kit supplies contract-complete global implementations for canonical
operational roles. Kent documents workspace config as higher precedence and it
may specialize the same role name with platform or repository-specific
instructions and execution settings. Every effective implementation of a
canonical role must preserve this contract; generated workflow correctness
must not depend on a project-only behavioral extension.

Do not add `model:` or `tools:` fields to role-prompt frontmatter. This includes
legacy provider aliases such as `sonnet`, `opus`, and `haiku`, current Kent
model names, and Claude-era tool lists. Describe behavioral restrictions in
the prompt body, while Kent configuration enforces actual model and tool
availability.

The release-decision role is intentionally tool-less. Operational release
drivers are deterministic Scripts: they consume canonical digest-bound plans,
require explicit mutation confirmation, and report preimage/postimage
settlement without granting shell, patch, edit, delegation, or workflow
subagent access.

## Session Communication

Shell-capable roles may use `kent run steer <session-id> '<message>'` only to
contact a specified, existing, active Session about the current assignment.
Include relevant context, a specific question or observation, and the
expected response. Mark verified facts, agent proposals, and human decisions
separately; cite the source of a claimed user decision. Do not select unrelated
Sessions, impersonate the user, or imply that a steer grants new authority to
the recipient. The recipient retains its own contract. Messages do not replace
formal reports, Workflow transitions, or independent review.

Only grill and the six roles with explicitly bounded Session communication
may, after their steer, use one read-only `kent run watch <session-id>` for
the same specified active run's next outcome. Give only the local observer
process a real deadline; backgrounding the command does not bound it. If
the observer cannot be bounded safely, a mutual wait is possible, or no
relevant outcome arrives, return "awaiting response" and close only the
observer, not the recipient's run. A returned Question or Approval must
not be answered, and an unrelated or interrupted outcome is not evidence
of independent criticism. `kent run wait` remains prohibited for these
roles.

Steer permission does not permit `kent run stop`, continuation of other
Sessions, Task or Workflow management, approvals, or creating children.
Apart from Task Supervisor's own Grill-child contract below, only grill has
the narrower additional permission to attempt
`kent run --session <session-id> '<bounded critique request>'` once for an
explicitly specified, demonstrably idle ordinary Session that is not owned by
a Workflow Task if either (a) the previous run completed normally, or (b) the
previous run was manually stopped by the user, canceled, or interrupted and a
new explicit human decision naming that exact Session was made after that
outcome. A previous manual stop, cancellation, or interruption does not inherit
earlier target selection; earlier target selection is insufficient. Verify the
target's type, state, and previous outcome from reliable Kent evidence, and
for (b) verify the human decision and its timing. Unknown prior outcomes,
target type or state, missing fresh authority, or an ambiguous result block
continuation rather than triggering a retry. This exception is not permission
for other roles to resume Sessions.

The six roles with previously blanket `kent run` prohibitions may also start
`kent run --agent grill '<bounded critique request>'` only outside Workflow
and only when Kent permits their child depth. This is independent criticism,
not delegation of the caller's responsibility. In Workflow, these roles remain
leaf Sessions: they may steer an existing active Session or return the request
to their caller, but cannot launch grill. Grill has
`agent_callable = true` and `workflow_subagent = true`. Plan may invoke one
bounded read-only grill leaf after discovery and a concrete draft, before
freeze and formal reviews. Revalidation repeats critique only for substantive
design or authority changes as defined in `contracts/plan-contract.md`.
Grill remains childless and supplies no independent review receipt. Workflow
review leaves cannot launch it. Do not raise the root
`max_subagent_depth` or grant tools to a tool-less role to circumvent these
limits. These narrow Kent Session-state effects do not authorize other
mutations prohibited by a role.

## Execution Continuation

Delivery roles consume completed results and distinguish success, failure,
truncation and a genuine pending observer handle. An identical successful
read needs changed state, a missing fact or required fresh safety evidence.
One bounded no-progress diagnosis targets the missing fact; completed reads
do not prove a hung shell. Required ownership/preflight checks remain.

Writer/Fix roles distinguish step budgets from full approved Task/file scope.
Proven earlier-step regressions inside that scope receive bounded repair and
fresh focused evidence; closed approved file boundaries still constrain them.
Do not turn ordinary repair progress into a new product decision or absorb
baseline debt, foreign changes or unproven target-only failures.

CI attribution requires bounded differential investigation before escalation.
Unknown attribution proves neither externality nor permission to edit.
Retain the concrete evidence gap and existing diagnosis route; real access,
service, authority or decision obstacles remain blockers. Retry limits and
merged-PR rules remain unchanged. Role prompts carry these rules independently
of this maintainer-only contract.

## Task Supervision

`task-supervisor` is a separate operational role, not the built-in edit
reviewer, a workflow node, or an extension of grill. Its explicit permissions
below do not expand the Session Communication permissions of other roles.
The installed role prompt carries these boundaries without depending on this
maintainer document in a consumer workspace.

The caller selects Tasks and their project identities, or explicitly requests
a bounded backlog selection. The latter permits inspecting that project's
backlog and selecting within the supplied criteria and start limit, not
unlimited execution or project-wide authority. Recover scope/defaults from
the assignment instead of asking the human to repeat accessible facts.
Selection permits resolving their current owning Sessions, not unrelated recipients or
project-wide authority. Prefer a dedicated interactive Session: observe until
the requested outcome is evidenced or the user stops supervision, without an
overall time limit. Bound individual inspections/waits and recovery attempts,
back off on unchanged normal work, and do not classify long planning as a hang.
Terminal Tasks, publication, merge and installation are distinct outcomes.
Installation requires project-owned adoption evidence and separately approved
effects; requesting that outcome does not authorize config/install/restart.
The role provides no scheduler, restart mechanism or uptime guarantee.

The operational cycle is a whole-selection scan, incident classification,
one bounded diagnosis/action slice per incident, verified settlement and
return to observation. Scan all selected Tasks before deep diagnosis or a
long wait; a focused follow-up is not a substitute. Keep failed reads as
explicit unknowns, not silently dropped Tasks. Waiting for a Task, operator
or Grill must not starve the rest. Restore the selection, outcome, authority
and outstanding intents after an answer, restart or compaction.
An explicitly requested reporting interval and next deadline also survive
answers/compaction/restart. Bound observer waits by the deadline and report
while alive even without changes. After a gap give one current catch-up,
not fictional missed reports or uninterrupted observation.
Accessible facts, applicable existing decisions and explicitly delegated
operational choices are resolved by the supervisor. Technical product work
stays with its owner; new scope/cost/risk and mandatory independent human
judgment stay human. Operational discretion does not accept unknown results
or override governance. Cite the mandate for an agent-made operational choice.

An explicit request to start a selected other Task permits one public native
initial start only when project policy allows agent enactment. Recheck
unexecuted/startable state, no retained/live or competing execution, dependencies,
and source/branch selected by the human or an already-authorized unambiguous
project default. Respect human-only restrictions and ask-on-first-execution.
Do not invent replacement targets, ignore dependencies, adopt existing refs or
recommend a command already known to fail. Record intent and read back native
root/source/execution; reconcile ambiguous effects before retry. This exception
does not grant other Git mutation, arbitrary Task movement or reconstruction
through Resume. PR merge and qualified recovery have separate narrow contracts.

Before escalation identify the exact decision delta. Accessible facts and
already-made decisions need provenance, not another human choice. Technical
ordering within approved scope belongs to the owner/planner; new scope, cost,
risk and independent judgment remain human. Check the supported owner route
before recommending it. A new decision is not closed merely because a steer
was delivered: verify reconciliation or transfer to a capable owner. Repeated
approval IDs for the same cause/unmet condition are one incident; do not approve
to poll or repeatedly ask whether an existing prohibition applies. A missing
route is a capability gap, not another product-ordering decision.
Before asking the human to open another operator Session, compare supported
project-level alternatives read-only and disclose their authority delta, such
as diagnostic trigger/source/run-count changes. This neither authorizes those
edits nor grants operational delegation. A missing current route does not
prove that an external executor is the only viable design.
Match diagnostic intent to actual transition prerequisites: a prerequisite-ready
edge is not a diagnostic route. Separate independent factual prerequisites;
manual Stage checks do not imply provider sandbox, and CI authorization does
not invent a billing-access gate. Preserve accepted limits and preflight the
exact source before a constrained run. Verify the owner's actual capabilities,
accepted alternative evidence and remaining mandatory gates without projecting
the supervisor's Git bans or claiming Smoke PASS. Semantic outcomes, not exit0,
prove runtime results. Verify CLI grammar and distinguish initial source pin,
current Task/PR head and moving target.

The role may inspect relevant evidence, steer the verified active executor or
a human-selected auxiliary operator, answer a demonstrably factual Question or
an explicitly delegated operational choice with provenance and agent authorship,
and perform a guarded `kent task resume` after classified recoverable
interruption. A factual answer may unblock already-authorized effects but
cannot create authority. New human decisions and required independent human
judgments remain human; a supervisor may enact an already-made decision.
Re-read the pending Question and execution immediately before answering;
first-pending-question targeting is not an atomic question-ID operation.
Changed, concurrent or unreliable targeting blocks the answer. Reconcile an
ambiguous effect before any retry.

For a selected other Task, the supervisor may use public `kent task approve`
when verified human authority or an unambiguous standing authorization covers
the exact current approval's consequences and its prerequisites are met.
Recheck the approval ID, recipient and effects, not just its title. Changed
scope, a later stop, unresolved prerequisites, competing execution or new
human judgment blocks enactment. Respect actual CLI/project restrictions and
human-only endpoints; no identity substitution or direct database mutation.
Read back approval and execution state; do not confuse enqueueing with resumed
work or blindly retry an ambiguous result.

An original human Session message or native Question answer may supply
authority when its authorship, content, context and applicability are
verifiable. Reference its real locator through agent-authored audit context;
never fabricate `--author user` provenance or downgrade a verified decision to
mere intent because an agent conveyed it. Receivers verify the original, not
the summary. Permission for a bounded action includes necessary in-scope
preparation, not unknown later results or unlimited effects. Already-permitted
reads/diagnostic preparation need no duplicate consent solely because a source
is access-controlled or the work is called planning. Required governance and
independent judgments remain binding. Global guidance grants other roles no
new enactment permission.

Automatic Resume requires retained execution, consistent node/locked target,
reconciled effects and no competing executor/recovery. Allow one automatic
attempt per incident, with actual execution readback; enqueueing is not proof
of continuation. Repeated failure without progress needs diagnosis and
escalation. Never automatically resume a deliberate user stop; a fresh exact
human instruction may authorize continuation after the same checks, without
reviving terminal/canceled Tasks or bypassing an approval gate. Missing
execution does not authorize repeated Resume or manual task movement.

Resource recovery remains project-owned. Distinguish existing/lost tokens,
missing leases and foreign/corrupt ownership. Prefer recovery by the active
executor. Prefer project adapters and runbooks; if no suitable wrapper exists,
already-authorized operations may use documented standard tools with verified
project policy, bounded procedure, genuine identity, effective serialization,
postconditions and handoff. A missing wrapper alone is not a blocker, while
an unprovable safety precondition is. No identity spoofing,
concurrent checkpoint writes, TTL-only foreign-resource reclamation, destructive
resets or invented adapter capabilities. A missing lease is recoverable only
through a procedure that establishes availability and safely acquires it.

Explicitly authorized startup of an existing suitable virtual device does not
require developing a custom launcher, but does require conflict checks and
project lease compliance. It does not authorize creating devices, wipe,
configuration edits or physical-device use. Host preparation cannot bypass a
pre-action lease requirement. A private unshared lock is not exclusive-startup
proof; verify the intended process/target and handle partial startup.
Release/reacquire is not atomic handoff; the executor must acquire its own
runtime lease before app/device work. No PID-only kills, foreign reclamation
or promises that boot leaves userdata byte-identical.

Record compact intent/result evidence through existing authorized mechanisms
or Task comments, never per-poll chatter, secrets or a parallel lifecycle.
Comments are audit, not live control; cooperative deduplication is not a lock.
Retained evidence preserves incident budgets across supervisor invocations.
Do not add source/config edits, generic operational children, general Git delivery,
Workflow mutation, arbitrary move/complete, stopping runs or direct
continuation of Workflow Sessions to the recovery permission. Only the
bounded Grill, standalone diagnosis, authorized PR merge and qualified native
recovery exceptions below expand the former blanket restrictions.
Initial start uses its separate explicit-request contract above. Technical
restoration to an already-authorized state may be communicated to its owner
after identity/conflict/procedure checks; it does not grant direct source
mutation or Git writes beyond the authorized PR merge exception.

### Bounded Grill consultation

Outside Workflow and within Kent's child-depth limit, the supervisor may
launch one bounded read-only Grill child for a material unresolved decision.
Supply evidence, authority, alternatives and a concrete critique request.
Reusing its own normally completed Grill child requires materially changed
facts or substantive follow-up; a stopped child needs fresh authority.
Grill remains childless, cannot enact operations or grant authority, and does
not replace either formal governance review. Routine facts/ordering and
unchanged blockers need no critic. No children outside the explicit
Grill/standalone contracts, critic substitution, depth increase or config
changes are permitted. Continue whole-selection observation while criticism is
pending; bound local observers.

### Standalone runtime diagnosis

Outside Workflow and within the existing child-depth limit, one bounded
`runtime-smoke-tester` assignment per unchanged incident may diagnose already-
authorized runtime work when direct facts and capable owner/replan routes are
insufficient. Code reads/parser fixtures need no helper. Verify exact authority
for environment/data/actions/run limits and the effective installed diagnostic
mode before launch; an old locked role does not acquire it from a caller prompt.

Require a compatible project standalone procedure with genuine child identity,
exclusive resource ownership, own durable minimal state/evidence, reliable token
capture and cleanup without Task checkpoint/lifecycle writes. A waiting owner
still owns resources. Existing documented procedures may suffice; unavailable
permitted persistence or unknown safety is a preflight blocker. No invented
Task ID, prompt override, default-child fallback, new adapter by assumption or
depth/config change. A lost-token path requiring a Task ID remains unavailable
when the child lacks it.

The runtime role selects a distinct diagnostic mode: bounded diagnosis rather
than full Smoke defaults; no product/config/Git writes, Task lifecycle/checkpoint
actions, children, waivers or official Smoke PASS. Build/install/start/account
changes need applicable diagnostic authority. Preserve shared targeting,
privacy, evidence audit, tool, lease and restoration safety; project commands
or permitted first-class tools own private state/evidence, not shell edits.
Existing Workflow Smoke behavior stays unchanged.

Supervisor records child identity/mandate/intent/settlement, continues fair
whole-selection observation and verifies semantic diagnosis plus owned-process,
restoration and resource settlement. Do not abandon or restart an equivalent
child; reconcile unfinished work before continuation. Read-only clarification
of a completed child grants no runtime retry. The helper grants no extra
run/spend or recovery-effect budget: its technical recovery shares the same
incident budget, while read-only diagnosis is not itself recovery. Preserve
limits after compaction. Kit source rollout alone does not qualify a project's
standalone procedure or authorize a production canary.
Pass incident identity, exact authority locators, allowed actions, remaining
run/recovery allowance, prior attempts and unfinished intents to the helper.
Reserve a delegated recovery attempt; supervisor and child cannot recover that
incident concurrently before settlement. The child receives the shared
one-effective-or-unsettled-attempt definition, reports recovery actions and
settlement, and blocks recovery on missing allowance/settlement without
blocking permitted read-only diagnosis. Native alternatives remain supervisor-
owned. Before acquisition verify identity/conflicts/procedure; the serialized
acquire establishes ownership, which is verified with persisted token before
runtime actions. A free resource does not require a pre-existing lease.

### Authorized PR merge

The supervisor may enact verified existing human/standing merge authority for
a selected Task's accepted PR result when project policy permits its actor.
Verify exact repository/PR/base/head, accepted scope/result, method, applicable
checks and any narrowly applicable CI waiver. A waiver for one project does
not transfer to another or bypass repository protections and mandatory review.
Recheck just before effect and bind the provider request to the verified head
(GitHub `--match-head-commit` with explicit repository/PR). Follow current-base
validation: the head guard does not freeze the base. No `--admin`, merge-queue
bypass, method fallback, source edits, commit/push/local rebase, conflict
repair, branch deletion or implied install/config/restart. Record intent and
verify provider merge evidence. Queued/auto-merge-enabled is not merged;
unknown settlement requires reconciliation before retry.

### Qualified native recovery

Prefer the active owner or valid retained Resume. A diagnosed failure that
Resume would repeat, or missing retained execution, may use an existing
project-qualified native route on an interrupted selected other Task.
Verify no active/competing owner, pending human gate or unauthorized deliberate stop, exact
nodes/group/context mode, input provenance, locked target/root, checkpoint,
resource ownership and cleanup obligations. A fresh exact human instruction
after a deliberate stop remains necessary.

The only waiting-state exception covers a selected other Task in
`waiting_question`/`waiting_approval` whose exact technical pending condition is
already resolved by verified authority/evidence but its retained route is proved
incompatible. Prefer answer/approve and owner/replan first. Waiting is ownership.
The project-qualified native procedure must settle/supersede the exact old
pending object/execution and exclude late answers or competing continuation
before replacement runs. Preserve human judgment, independent review/approval,
source/root, checkpoint/resources, cleanup and fan-out/Join. Changed route
selectors require exact provenance and equivalent authorized meaning. Unknown
settlement/semantics, unmet prerequisites or unresolved human judgment block
reentry; this is not approval bypass or another recovery budget.

An edge's existence or CLI help alone does not qualify its effect semantics.
The project procedure must prove satisfied prerequisites, equivalent authority,
preserved approval/review and fan-out/Join invariants. No fabricated success,
unfinished-stage bypass, forced terminal state, Cleanup bypass or independent
fan-out sibling. Missing retained execution needs a supported incoming
`new_session` route. Use only native `kent task move` with the verified
transition/values, record intent and read back real ownership/execution.
No graph/database writes, locked-target changes, actor bypass or stopping an
owner to manufacture eligibility.

Resume and native recovery share one automatic recovery-effect budget per
incident without evidenced progress. Diagnose known same-input failure before
Resume and use a qualified equivalent route directly where allowed.
At most one automatic Resume invocation per incident. A proved no-op may include
a completed validation-only Script, qualified against exact executed code,
dependencies/setup, actual attempt-specific failure path/completion, no runtime/
resource/checkpoint/source/external effects or queued/active/deferred late work.
Process creation, read-only validation and settled enqueue/status/audit
bookkeeping alone do not spend the effect budget. Provider requests or actual
effects spend it; unknown settlement reserves it until reconciliation. Failure
or cleanup does not erase an effect; actual work briefly returning to its
blocker is not a no-op.

After a proved no-op at most one equivalent qualified native alternative may
follow under existing authority and proved preconditions. Never a second Resume
or third automatic route, even if the alternative also proves no-op. Reuse a
matching existing qualification rather than demand a complete audit/new adapter
per attempt, but prove current settlement. Do not falsify cursors or provenance
or enumerate transitions. Budgets survive restart/compaction.
Unqualified semantics remain a capability gap.
An equivalent failure forbids another Resume, not a qualified native alternative
after a proved no-op. Do not escalate solely for validation-only failure when
that existing-authority alternative satisfies its contract.

Repeated token capture/persistence/cleanup failures remain one root-cause incident
across lease/Session/approval IDs. Fixture-check the exact owner's command/output
grammar before another acquisition; do not repeat a broken parser and request
the same cleanup. This grants no source edits or forbidden identity/token recovery.

Never duplicate a Task's existing Question/Approval with a supervisor Question.
Notify the caller of its exact decision once and continue independent help.
In autonomous/overnight mode, isolated human gates remain at their owners
while observation continues elsewhere. Ask one concrete new decision with
options and a recommendation only without an existing owning decision object
and when a Session-wide pause is necessary or matches the caller's mode;
then wait without polling that stopped incident.
Use native Question UI for a genuinely new interactive decision; do not end
observation merely to present an unrelayed question. Preserve real human stops
and the headless return contract.
The native Question may pause observation of all selected Tasks, not the Tasks
themselves. After an answer or restart, first re-read **all selected Tasks**,
discard stale/resolved work, reconcile unknown effects and fairly complete
the discovered authorized actions before the next human Question. One blocked
action must not prevent independent help elsewhere. Catch-up handles incidents,
not the completion of long-running Tasks or an infinite stream of new events.

Headless invocation is caller-mediated: when input is required, return the
blocker/options and end the run. The caller obtains the decision and explicitly
arranges continuation; no background waiting is implied by a final response.

## Review Ownership

Generated Delivery workflows assign operational ownership directly from the
project profile: `implementation` owns Implement, `fix` owns Fix, `qa` owns
Smoke, `release` owns PR preparation/Cleanup, and `ci` owns CI/Waiting PR. The
`orchestrator` role owns Plan; optional `gate` owns Gate and otherwise falls
back to `orchestrator`. Operational nodes must not launch a second copy of
their own profile role; bounded research and diagnosis delegation remains
allowed.

Auxiliary release workflows separate lifecycle ownership:

- `release-manager` owns version changes, release preparation, tags, and
  external release records;
- `delivery-operator` owns PR delivery, branch operations, and conservative
  cleanup;
- `ci-monitor` owns bounded CI, merge-state, and release-automation monitoring.

Assign these roles directly. A release node must not wrap the same specialist
in a generic `default` session.

Generated workflows also own independent final Standards, Specification, and
Compliance review stages. Implementation and Fix must not launch another copy
of those final review responsibilities before the graph fan-out.

Standards, Specification, and Compliance are direct workflow leaf roles. Their
config sets `agent_callable = false` and `workflow_subagent = false` so other
agents cannot target those review roles, while their prompts prohibit the
review sessions themselves from creating any child role in Workflow. Outside
Workflow, they may request grill criticism only within the Session
Communication boundary. Delegation depth is a root setting rather than a
per-role child policy, so the Workflow no-child guarantee is behavioral
rather than tool-enforced.

Standalone review commands may use project-specialized reviewers when no
generated Delivery graph owns the same review pass.

Reviewers classify security and privacy findings from evidence rather than
labels. Technical identifiers are not secrets or PII by default. A
security/privacy severity requires either an applicable rule that classifies
the data as sensitive or a concrete threat model with access prerequisites and
meaningful impact. Least-data exposure and backend-decoupling concerns without
that evidence remain data-minimization or maintainability judgements.

Standards reviewers also classify repository-wide analyzer failures
differentially. A candidate failure becomes task-scoped only when comparison
with the pinned baseline proves a new or worsened violation. Pre-existing debt
is reported but not assigned to the task writer. If an explicit absolute-clean
policy contradicts the baseline repository state, the review reports a policy
blocker for user resolution instead of manufacturing a broad Fix scope.

The pinned task baseline is the task fixed point or Kent-resolved execution
commit, not a newer merge-target tip. Standards and Specification reviewers
assess target drift separately through three-way merge or method-specific replay
evidence. Missing target-only commits in an older checkout never authorize Fix
by themselves.

Changing this contract or generator prompt does not mutate live workflows.
Graph-level adoption requires a separately approved lifecycle operation under
[Execution-history and compatibility policy](workflow-contract.md#execution-history-and-compatibility-policy).
A project revision may enforce the rule through its project contract for new
tasks that select that revision; retained Session instructions do not refresh
automatically.

## Project Adoption

Every repository with `.kent/workflow-profile.toml` is checked independently.
Platform adapters may define different role prompts, but they follow the same
ownership boundary. Model changes are rolled out through Kent configuration
after active workflow sessions finish and require a Kent service/Desktop
restart; prompt-only cleanup and validation do not.
