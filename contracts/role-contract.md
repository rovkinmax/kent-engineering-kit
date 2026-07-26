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

Do not add `model:` or `tools:` fields to role-prompt frontmatter. This includes
legacy provider aliases such as `sonnet`, `opus`, and `haiku`, current Kent
model names, and Claude-era tool lists. Describe behavioral restrictions in
the prompt body, while Kent configuration enforces actual model and tool
availability.

## Review Ownership

Generated Delivery workflows own independent final Standards, Specification,
and Compliance review stages. Implementation and Fix procedures may delegate
bounded research, diagnostics, or implementation slices, but must not launch
another copy of those final review responsibilities before the graph fan-out.

Standards, Specification, and Compliance are direct workflow leaf roles. Their
config sets `agent_callable = false` and `workflow_subagent = false` so other
agents cannot target those review roles, while their prompts prohibit the
review sessions themselves from creating any child role. Kent 2.4 exposes
delegation depth only as a root setting and has no per-role depth or child-tool
policy, so the no-child guarantee is behavioral rather than tool-enforced.
Direct workflow assignment must be revalidated after the next config restart.

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
