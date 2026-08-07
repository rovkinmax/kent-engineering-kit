# Roadmap

Current baseline: Kent 2.5.0, August 7, 2026.

The kit is intentionally iterative. Stabilize real delivery first; normalize
names, remove experiments, and introduce a strict compatibility version only
after the composition is proven across projects.

## Shipped Foundation

- Platform-neutral global authority, safety, session, worktree, and context
  rules.
- Canonical role prompts with model/tool/delegation policy in Kent config.
- Profile schema 3 with work kinds, execution targets, capabilities, role
  mapping, context manifests, procedures, and adapters.
- Generated Engineering Delivery, Canary, and Smoke Lab workflows.
- One-writer Implement/Fix slices with deterministic verification fan-out,
  independent Standards/Specification reviews, Join, Gate, optional Smoke,
  final Compliance, PR/CI/merge waiting, and task-owned Cleanup.
- Deterministic Script nodes for verification dispatch/reporting, branch
  identity, CI waiting, merge waiting, and post-session Janitor cleanup.
- Append-only evidence ledger and ignored resumable Fix/Smoke checkpoints.
- Shared MCP, Jira, mobile lease/evidence, revision-preflight, worktree, and
  GitHub merge-strategy adapters.
- Approval-gated post-merge package publication for projects that declare it.

Current project defaults:

- Appsome Engineering Delivery v19;
- Puber Engineering Delivery v17;
- Osome SDK Engineering Delivery v9;
- Osome Slack Reader Engineering Delivery v1.

Existing tasks remain frozen on their recorded workflow revision.

## Active: Context Diet v1

### Instruction ownership

- Global `AGENTS.md` owns only universal invariants.
- Role prompts own reusable role behavior.
- Generated prompts own dynamic task inputs, exact transitions, and
  edge-specific mutation authority.
- Project `AGENTS.md` owns repository-wide gotchas.
- Project contracts contain workflow-relevant project deltas only.
- Context manifests own per-node read budgets and exclusions.
- Project skills/procedures own conditional platform and domain detail.
- Maintainer contracts are normative generator sources, not runtime preload.

The current candidate removes repeated CI, baseline, reviewer, writer, Smoke,
and merge-policy prose from runtime layers. Prompt and document byte budgets
are covered by tests to prevent silent regrowth.

### Evidence tooling

- `context.files_read` excludes the separately recorded manifest path.
- Evidence append is idempotent per Kent run, so provider recovery returns the
  original event instead of adding a near-duplicate.
- Evidence and checkpoint writers fail immediately when started without JSON
  input instead of waiting forever on interactive stdin.
- Metrics retain instruction bytes, repeated reads/questions, verification
  loops, and nullable model-call/compaction counters.

### Project adoption

- Appsome, Puber, SDK Generator, and Slack Reader project contracts are reduced
  to actual local deltas.
- Android/KMP project skills are compact indexes over lazily loaded recipes.
- Slack Reader loads its consumer skill only for public CLI, schema,
  authorization, download, and error-contract changes.
- Kit-managed checkpoint/evidence scripts are synchronized to all four
  projects.

Rollout requires commits containing the project instructions/scripts and new
workflow revisions. It does not require a Kent restart.

## Active: Delivery Reliability

- CI waiting and merge waiting remain deterministic Script nodes.
- CI Monitor may rerun one exact unchanged-head UI/instrumentation/connected/
  smoke/unit job up to twice after the original attempt when tests actually
  started. Assertion, fixture, emulator/device, transport, timeout, and external
  `5xx` failures are reproducibility candidates.
- Analyzer, compiler, configuration, dependency, package, publication,
  release, pre-test, user-cancelled, superseded, ambiguous, and exhausted
  failures are not automatically retried.
- Every attempt and failure fingerprint remains evidence; a later green result
  does not erase the earlier transient failure.
- Pending CI or an unchanged open PR never becomes an approval request.
- Post-merge late failures never return the delivered branch to Fix.
- Provider-visible output recovery may still finish a node without a final
  answer. Checkpoints preserve completed work and evidence append is idempotent
  for the Kent run; automatic transition finalization remains an upstream
  reliability follow-up.

OSM-53 is the current live validation of the three-attempt UI-test policy.

## Next

1. Finish OSM-53 and classify its final CI attempt.
2. Review and commit the Context Diet candidate in the kit and four consumer
   repositories.
3. Generate one non-default replacement workflow per project.
4. Canary managed-worktree execution, evidence append, Fix checkpoint, CI
   watcher, and Cleanup.
5. Promote validated revisions for new tasks and retire superseded workflows
   after Backlog migration.
6. Make `kent-engineering-kit` itself an Engineering Delivery consumer using
   `scripts/validate`, no runtime Smoke, and a normal GitHub PR/CI tail.

## Follow-ups After Adoption

- Split Appsome's project-owned Jira adapter into reusable read-only and
  separately gated release-mutation boundaries only after another project
  needs the same extended operations. Explicit adapter ownership already
  prevents the generic kit template from replacing it.
- Observe real instruction-byte, repeated-read, question, verification-loop,
  model-call, and compaction metrics before tuning global context compaction.
- Experiment with a lower compaction threshold only after measurements show
  that late compaction causes avoidable rereads or failures.
- Extend Web, iOS, and IoT profiles from actual repositories rather than
  speculative platform abstractions.
- Normalize experimental workflow names and delete obsolete duplicates after
  the common layout is stable.
- Version the profile/compatibility contract only after the working
  composition and migration policy are fixed.
- Add a supported structured task-comment listing path that exposes canonical
  comment IDs. Kent 2.5 `kent task comment list` still omits them.

## Restart Boundary

No Kent restart:

- role prompt, skill, contract, project documentation, procedure, adapter, or
  generated workflow changes;
- project commits and new workflow revisions.

Kent service/Desktop restart:

- global or project `config.toml`;
- model/reasoning/tool/delegation routing;
- concurrency, compaction threshold, or other service configuration.
