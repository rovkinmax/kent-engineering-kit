# Model Policy

## Current Source Policy — September 5, 2026

[`config/subagents.toml`](../config/subagents.toml) is the authoritative managed
configuration fragment and the single source of truth for role allocation.
The mixed policy restores `gpt-5.6-luna` for eight subagent roles and retains
`gpt-6-astra` for the root, the disabled built-in reviewer, and the remaining
seven roles: eight Luna and nine Astra selectors in total. Former Sol and
Terra assignments use Astra; the historical July policy below is not the
current allocation.

### Model Selection, Reasoning, and Budgets

This split is an operator choice, not a claim of measured Kent quota savings.
Evaluate it against task quality, provider failures, retries, latency, context
growth, and review/Fix-loop counts. Model selection and `thinking_level` are
separate settings; changing the model does not authorize changing reasoning.
Do not attribute an observed outcome to model selection without accounting
for the reasoning setting and workflow behavior.

The root retains `model_context_window = 872000`; the fast role restores
`model_context_window = 372000` alongside its Luna selector. These are
intentional operator-chosen harness budgets, not statements about provider
maximum context capacity. No other role receives an explicit window.

This bounded change preserves every other setting: per-role reasoning,
low verbosity, tools, prompts, callability, priority mode, the disabled
built-in reviewer, workflow concurrency of 4, and maximum subagent depth of 1.
It adds no compaction keys and preserves operator-owned root compaction
settings. Role-derived context budgets and compaction thresholds follow Kent's
derivation rules when a role's model or explicit window changes; those derived
thresholds are not promised to remain unchanged.

### Configuration Adoption and Runtime Evidence

Configuration adoption is a separately approved reconciliation of the managed
fragment with global and project settings. Check each intended launch root
for explicit overrides and declared inheritance, preserving unrelated user
settings. Historical attached workspaces and retained worktrees can keep
older overrides; do not assume they inherit this policy.

File-level adoption is not effective-runtime evidence. Existing and resumed
sessions retain their locked settings; neither a source patch nor a
configuration-file update refreshes those locks or establishes runtime
activation. New-session verification and the documented service/GUI restart
require a separate gate that accounts for active work.

### Validation, Rollout, and Rollback Boundaries

Source validation checks the managed policy and its regression tests. This
package has no workflow graph delta and does not authorize installation,
global or project configuration edits, restart, or a runtime canary.

Approve installation/configuration effects and restart/new-session
verification explicitly; approval for file changes alone is not restart or
runtime-canary authority.
`scripts/install` creates links for Kit assets; it does not merge
`config/subagents.toml` into the effective configuration. Preserve user changes
when reconciling configuration and verify effective settings separately.
Confirm model and reasoning in newly created sessions after the approved
restart; existing or resumed sessions are not rollout canaries.

Before installation, source rollback is the inverse of this bounded package's
patch, preserving unrelated changes. It is not a global model downgrade and
must not undo the separately delivered hotfix. After installation, rollback
requires a separately approved operational plan that accounts for linked
assets and effective configuration; a source-only inverse is insufficient.

### Sources and Evaluation

- [OpenAI model selection](https://developers.openai.com/api/docs/guides/model-selection)
  recommends establishing an accuracy target and a strong baseline, then
  evaluating cheaper or faster models that still meet the quality target.
- [OpenAI Astra migration guidance](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6-astra)
  recommends preserving effective reasoning effort during the initial
  migration before tuning it separately.
- [OpenAI Codex subagents](https://learn.chatgpt.com/codex/agent-configuration/subagents)
  includes Luna Medium examples for `docs_researcher` and `code_mapper`.
  These illustrate task-specific model/effort choices; they do not override
  the managed role settings.
- [Artificial Analysis: Benchmarking GPT-6 Astra](https://artificialanalysis.ai/articles/benchmarking-gpt-6-astra)
  distinguishes Coding Agent and Intelligence Index outcomes, output-token
  usage, and API cost. Those benchmark-specific measures are not Kent
  subscription quota measurements.

These sources inform the operator's choice; they do not prove actual Kent
quota savings. Evaluate the mixed policy on comparable tasks using observed
quality, retries, latency, context growth, and provider pressure before
claiming an improvement.

## HISTORICAL — July 2026 Experiment (Not Current Advice)

The entire July text is retained below with subordinate headings for historical
context only. Its then-active routing, schema and canary claims, candidate
values, merge instructions, and restart guidance are not current operational
advice and do not override the current source policy above.

### Model Policy Experiment

This is an iterative operating policy, not a versioned compatibility promise.
The July 31 candidate routes well-specified implementation and bounded
operational work to Luna/high while retaining Sol for Plan, architecture,
specification fidelity, and verified Fix work. Existing sessions retain the
settings with which they were created. The candidate requires a Kent restart
and newly created sessions before it is considered active.

#### Optimization Target

The current environment uses a subscription rather than separately billed API
tokens. Model selection therefore optimizes:

- subscription quota pressure and `429` frequency;
- latency and retry rate;
- reasoning and output volume;
- context growth and compaction frequency;
- duplicate work between orchestration and review layers;
- defect detection and escape rate.

Dollar-denominated API pricing is not a decision criterion.

#### Kent 2.4 Schema Verification

The candidate below was checked on July 24, 2026 against Kent 2.4.0 source and
the official configuration reference:

- exact built-in IDs are `gpt-5.6-sol`, `gpt-5.6-terra`, and
  `gpt-5.6-luna`;
- all three declare a 372,000-token context window;
- Sol and Terra support `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`
  reasoning; Luna supports the same list except `ultra`;
- root keys include `model`, `thinking_level`, `model_verbosity`, and
  `max_subagent_depth`;
- workflow concurrency is `[workflow].concurrency`;
- supervisor overrides use `[reviewer].model`, `thinking_level`, and
  `model_verbosity`;
- `reviewer.frequency` defaults to `edits`, so disabling the built-in
  supervisor requires an explicit `frequency = "off"`;
- role overrides use `[subagents.<role>]`, including `model`,
  `thinking_level`, `model_verbosity`, and `priority_request_mode`.

These values are schema-validated in the managed and effective configuration.
Do not restart Kent until the user confirms that other active sessions may
stop.

#### Balanced Candidate

| Capability | Candidate |
| --- | --- |
| Default Plan orchestrator | `gpt-5.6-sol`, medium |
| Direct implementation | `gpt-5.6-luna`, high |
| Fix | `gpt-5.6-sol`, medium |
| Standards review | `gpt-5.6-luna`, high |
| Specification review | `gpt-5.6-sol`, medium |
| Architecture design | `gpt-5.6-sol`, high |
| Compliance review | `gpt-5.6-luna`, high |
| Gate | `gpt-5.6-luna`, high |
| Research, platform reference, build diagnosis | `gpt-5.6-terra`, medium |
| Specialized UI, domain, and quality review | `gpt-5.6-terra`, high |
| Delivery | `gpt-5.6-luna`, high |
| Focused runtime Smoke | `gpt-5.6-terra`, medium |
| CI monitoring | `gpt-5.6-luna`, low |
| Fast mechanical work | `gpt-5.6-luna`, high |
| Built-in Kent reviewer/supervisor | disabled; `gpt-5.6-terra`, medium reserved for a separate canary |

Initial shared settings:

- default reasoning: medium;
- model verbosity: low;
- workflow concurrency: 2;
- maximum subagent depth: 1;
- built-in reviewer frequency: off.

Keep Plan on the default Sol orchestrator. Generated workflows assign
Implement, Fix, Gate, Smoke, PR/Cleanup, and CI/Waiting PR directly to separate
profile roles instead of paying for a default wrapper plus an optional child
session. Do not combine this routing canary with a compaction-threshold
experiment.

The candidate targets the largest observed sources of quota pressure:

- Sol is currently used for bounded implementation even after Plan has removed
  product and architecture uncertainty;
- nested review delegation repeats work already owned by graph-level
  Standards, Specification, and Compliance nodes.

One subagent level is sufficient for a workflow node to delegate a bounded
implementation, research, diagnosis, or platform-reference slice. A delegated
role must not create another delegation tree during this experiment.

#### Staged First-Rollout Configuration

Merge these values into the existing global configuration; do not replace
unrelated hooks, tools, worktree settings, or role descriptions:

```toml
model = "gpt-5.6-sol"
thinking_level = "medium"
model_verbosity = "low"
max_subagent_depth = 1

[workflow]
subagents = true
concurrency = 2

[reviewer]
frequency = "off"
model = "gpt-5.6-terra"
thinking_level = "medium"
model_verbosity = "low"

[subagents.fast]
model = "gpt-5.6-luna"
thinking_level = "high"
model_verbosity = "low"
priority_request_mode = false

[subagents.researcher]
model = "gpt-5.6-terra"
thinking_level = "medium"
model_verbosity = "low"

[subagents.standards-reviewer]
model = "gpt-5.6-luna"
thinking_level = "high"
model_verbosity = "low"
agent_callable = false
workflow_subagent = false

[subagents.spec-reviewer]
model = "gpt-5.6-sol"
thinking_level = "medium"
model_verbosity = "low"
agent_callable = false
workflow_subagent = false

[subagents.architecture-designer]
model = "gpt-5.6-sol"
thinking_level = "high"
model_verbosity = "low"

[subagents.implementation-worker]
model = "gpt-5.6-luna"
thinking_level = "high"
model_verbosity = "low"

[subagents.fix-worker]
model = "gpt-5.6-sol"
thinking_level = "medium"
model_verbosity = "low"

[subagents.compliance_reviewer]
model = "gpt-5.6-luna"
thinking_level = "high"
model_verbosity = "low"
agent_callable = false
workflow_subagent = false

[subagents.workflow-gate]
model = "gpt-5.6-luna"
thinking_level = "high"
model_verbosity = "low"

[subagents.runtime-smoke-tester]
model = "gpt-5.6-terra"
thinking_level = "medium"
model_verbosity = "low"

[subagents.delivery-operator]
model = "gpt-5.6-luna"
thinking_level = "high"
model_verbosity = "low"

[subagents.ci-monitor]
model = "gpt-5.6-luna"
thinking_level = "low"
model_verbosity = "low"
priority_request_mode = false
```

#### Active Direct-Role Routing

The kit now defines global fallbacks for:

- `implementation-worker`: Luna/high;
- `fix-worker`: Sol/medium;
- `build-doctor`: Terra/medium;
- `workflow-gate`: Luna/high;
- `runtime-smoke-tester`: Terra/medium;
- `release-manager`: Terra/medium;
- `delivery-operator`: Luna/high;
- `ci-monitor`: Luna/low.

Generated workflows use the profile's `implementation`, `fix`, `qa`,
`release`, and `ci` roles directly. Optional `gate` replaces the default Gate
role when declared.

The July 31 candidate changes only model ownership and the distinct Fix role.
It preserves the proven direct fan-out/Join topology, documentation-only Smoke
skip, and workflow concurrency limit.

Kent documents workspace config as higher precedence, but scheduler-created
direct-role sessions selected the global same-named role definitions during
both canaries. Global canonical roles are therefore contract-complete and
project-specific behavior is carried through the node prompt, project
contract, procedures, and adapters. Workspace same-name overrides remain an
optional specialization until the observed behavior is clarified upstream.

The next isolated quota experiment compares Luna/high implementation and formal
review against the previous Sol/medium and Terra/medium runs. Keep Plan,
Specification Review, and Fix on Sol while comparing first-pass success,
Fix-loop count, escaped defects, user intervention, latency, and provider
pressure.

Keep the built-in reviewer explicitly off during this rollout. Kent 2.4
defaults it to `edits`; leaving the key unset can add hidden model calls after
edit turns. The configured reviewer model is only a canary-ready override for a
separate experiment.

#### Dogfood Baseline

The July 24, 2026 PUB-26 Delivery v5 run reached Smoke after these graph-level
agent counts:

- Implement: 10 runs;
- Fix: 8 runs;
- Standards Review: 9 runs;
- Specification Review: 9 runs;
- Gate: 9 runs;
- Smoke: 1 run in progress when the baseline was captured.

These counts exclude nested subagent sessions. One measured verification cycle
expanded two logical review axes into 11 sessions, so graph-level run count
alone understates quota pressure. The run also experienced provider failures
after a writer, a reviewer, and a Smoke node had completed substantive work but
before their final transitions were persisted.

The replacement workflow should reduce both graph-level runs and hidden nested
sessions. Model changes are not considered successful if they merely make the
same duplicated lifecycle cheaper.

The July 25 post-restart canary proved that the first role grid was active, but
also exposed a topology defect: two direct workflow review nodes created five
child sessions. Standards created three children and Specification created two,
so two logical review axes expanded to seven concurrent model sessions. Kent
2.4 has no per-role subagent-depth override; `max_subagent_depth = 1` still
allows every scheduler-created root reviewer to create one child layer.

The candidate was therefore tightened without stopping active work:

- Standards, Specification, and Compliance are direct workflow leaf roles;
- their `agent_callable` and `workflow_subagent` flags are false, which stops
  other agents from targeting those roles;
- their prompts explicitly prohibit child-agent creation;
- their reasoning level is medium;
- Researcher and Architecture remain callable for bounded orchestration needs,
  but their prompts prohibit recursive delegation.

Kent 2.4 has no per-role depth or child-tool policy. The leaf prompt is
therefore the boundary that stops a direct workflow reviewer from creating a
generic or differently named child; callability metadata alone cannot enforce
that behavior.

The next PUB-26 fan-out consumed the updated prompts without a restart: two
direct reviewer sessions created zero children, compared with five children in
the previous cycle. Specification finished in about five minutes and Standards
in about seven. This validates the prompt boundary for the canary. The
reasoning, callability, and built-in reviewer config changes still require a
later restart, followed by a direct workflow-assignment canary.

After the first-rollout restart, PUB-26 approval reused the existing Smoke
session ID but created a new workflow run, refreshed the session lock, and
reported Sol/medium in the terminal. Do not infer an effective model policy
from session identity alone. Confirm the run-time model/reasoning badge or the
new lock timestamp after restart and approval.

Kent's supported task/session inspection currently confirms the effective
model family but does not expose the workflow session's reasoning level.
Until Kent exposes that field, record Sol/medium only when the run-time UI
badge is observed after the relevant restart; otherwise report Sol with
reasoning unverified. Resuming the same session also retains its locked prompt,
so resumed work is not a prompt-rollout canary.

#### Evaluation

Compare new tasks against recent Delivery runs:

- provider failures, `429`s, interruptions, and retries;
- wall-clock time per node and per completed task;
- context size and compaction events;
- repeated review findings across nested and graph-level reviewers;
- Fix-loop count;
- defects found after review or Smoke.

The July 24 first rollout is an intentional quota-relief bundle: it changes the
default reasoning and verbosity, role model overrides, workflow concurrency,
delegation depth, and fast-role priority mode together. It should be evaluated
as one operational baseline and cannot attribute savings to an individual
setting.

After that baseline is established, change one policy dimension per canary.
Keep or remove priority request mode based on observed latency, quota pressure,
and reliability rather than API price.
