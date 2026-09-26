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
Only grill has the narrower additional permission to attempt
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
`agent_callable = true` and `workflow_subagent = false`; do not raise the root
`max_subagent_depth` or grant tools to a tool-less role to circumvent these
limits. These narrow Kent Session-state effects do not authorize other
mutations prohibited by a role.

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
