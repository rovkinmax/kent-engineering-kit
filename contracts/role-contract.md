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
review sessions themselves from creating any child role. Delegation depth is a
root setting rather than a per-role child policy, so the no-child guarantee is
behavioral rather than tool-enforced.

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

Changing this contract or generator prompt does not mutate a task-backed live
workflow. Existing graph definitions remain frozen. A project revision may
enforce the rule through its project contract for new tasks that select that
revision, but cross-project graph-level enforcement requires a separately
validated replacement workflow.

## Project Adoption

Every repository with `.kent/workflow-profile.toml` is checked independently.
Platform adapters may define different role prompts, but they follow the same
ownership boundary. Model changes are rolled out through Kent configuration
after active workflow sessions finish and require a Kent service/Desktop
restart; prompt-only cleanup and validation do not.
