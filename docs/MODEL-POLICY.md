# Model Policy

## Current Source Policy — September 16, 2026

[`config/subagents.toml`](../config/subagents.toml) is the authoritative managed
configuration fragment and the single source of truth for role allocation.
Use existing roles and explicit configuration; do not add a complexity router
or duplicate coder roles. The historical July policy below is not current.

### Allocation and Manual Selection

- Main agent and planner: `gpt-6-astra`, low.
- Simple coder (`implementation-worker`): `gpt-5.6-luna`, xhigh.
- Complex standalone coding: explicitly select Astra medium on the same role.
- Runtime QA (`runtime-smoke-tester`): Luna high.
- Code and specification review (`standards-reviewer`, `spec-reviewer`):
  Astra medium.
- Architecture design (`architecture-designer`): retain Astra high, by
  explicit user choice.
- Fast: Luna high.
- Built-in reviewer/supervisor: Luna xhigh, enabled with `frequency = "edits"`.

Research, Fix, build diagnosis, and release management retain Astra medium.
Compliance review, Gate, and delivery retain Luna high; compliance is a
rule-compliance leaf, not general code review. CI monitoring and the
tool-less release decision retain Luna low.

For an explicitly assigned standalone complex implementation slice:

```bash
kent run --agent implementation-worker \
  --model gpt-6-astra --thinking-level medium \
  "<bounded implementation assignment>"
```

This launches a separate manually selected session, not a workflow transition.
Do not launch a competing writer for workflow-owned work. The role's explicit
372000/353400 working budget remains in effect even with the Astra override;
the override does not acquire the root's larger budget.

Configured workflow Implement nodes continue to select `implementation-worker`
with Luna xhigh for all tasks. Automatic complexity-based selection is deferred
to **KEN-18**, not implemented by these configuration changes.

### Optional Work and Mandatory Gates

No workflow graph or role prompt changes accompany this allocation. Planning
may omit inapplicable optional work with an explicit rationale, subject to the
task and project contract. Workflow-stage skipping must use an existing
authorized route: conditional Smoke is decided by Gate and its
`smoke_rationale`, not by an arbitrary planner bypass. Mandatory verification,
reviews, approvals, and single-writer ownership remain unchanged.

### Model Selection, Reasoning, and Budgets

This split is an operator choice, not a claim of measured Kent quota savings.
Evaluate it against task quality, provider failures, retries, latency, context
growth, and review/Fix-loop counts. Model and reasoning changes are explicitly
paired above; do not attribute an outcome solely to the model.

The Astra root uses `model_context_window = 400000` and
`context_compaction_threshold_tokens = 360000`. Each of the eight Luna
subagent roles explicitly pairs `model_context_window = 372000` with
`context_compaction_threshold_tokens = 353400`. The supervisor has a separate
372000 window; its schema does not provide a compaction-threshold setting.
There are nine Luna selectors including the supervisor, and eight Astra
selectors including the root. No Astra subagent receives an explicit role
budget. Standards review now inherits the root budget; runtime QA receives
the Luna pair. These are operator-chosen harness budgets, not claims about
provider maximum capacity.

Preserve low verbosity, tools, prompts, callability, priority mode, workflow
concurrency of 4, and maximum subagent depth of 1. The root window and
compaction threshold are managed together and must match the installed
global configuration. Compaction mode `native` and other operator-owned
compaction settings remain unchanged. The 400000/360000 pair is a bounded
working-budget experiment, not a proven quota-saving optimum.

Kent 2.7.2 derives Luna's 372000/353400 pair on a fresh role launch but skips
that derivation under a model lock. Without explicit pairs, continuation can
inherit root budgets or conflict with an inherited root threshold. Explicit
pairs preserve the intended role budget without changing a locked model.

### Configuration Adoption and Runtime Evidence

Reconcile global and project settings only with explicit adoption authority.
A project-defined role replaces the entire global role definition in Kent
2.7.2; fields are not merged individually. Project-defined Luna roles therefore
need their own explicit budget pair. Preserve project prompts, tools,
callability, and unrelated user settings.

The September 16 adoption covers registered primary roots:

- AppsomeAndroid: local coder becomes Luna xhigh, quality code review becomes
  Astra medium, and both mobile/runtime smoke aliases become Luna high with
  the explicit budget pair. The target branch is `release/4.30.0`, not master.
- Puber: local coder becomes Luna xhigh; Compose, domain-model, and quality
  code-review roles become Astra medium.
- Kit, agent-default, OsomeAPI-SDK-generator, and osome-slack-reader inherit
  global role definitions where no project override exists. Do not create
  redundant configuration files.

Retained worktrees and historical bootstrap templates are outside this
adoption. Inspect their effective configuration before using them; do not
assume they inherit the updated primary-root policy.

File-level adoption is not a runtime canary. Existing model and prompt locks
are preserved, but working budgets can be re-read on continuation before a
restart. An Astra-locked QA session can compact earlier with its new smaller
budget. Removing the Luna budget from standards review also changes that
role's continuation budget. Restart is not an isolation boundary.

The enabled supervisor adds asynchronous review after successful first-class
edits once the configuration is effective. It does not replace mandatory
workflow review and may add model usage. Do not claim measured savings.

### Validation, Rollout, and Rollback Boundaries

Run source-only `./scripts/validate` and parse the adopted TOML files. Compare
only the approved key deltas against exact working preimages, not Git HEAD
when a project already has uncommitted configuration changes. Static checks
of global/project role resolution are not evidence of a launched model.

`scripts/install` creates links for Kit assets; it does not merge
`config/subagents.toml`. This update does not run the installer or change
workflow graphs, role prompts, task execution, or project profile mappings.
Source/configuration approval is not permission to restart Kent, start a
runtime canary, commit, or push.

Documented global activation requires a safe service/GUI restart and new
sessions. Schedule that separately with active work accounted for. Do not
resume historical sessions merely to test budgets. A separately authorized
canary must check actual model, reasoning, window, and threshold; a fresh
launch does not verify continuation behavior.

Rollback is the inverse of only this package's approved key changes relative
to retained working preimages. Preserve subsequent and pre-existing user
changes; never reset whole files to Git HEAD. Operational rollback needs
separate approval because restored budgets and reviewer frequency also have
runtime effects.

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
