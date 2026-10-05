# Appsome Kent Project Contract

This file contains only Appsome-specific deltas for the shared Kent Engineering
Kit. Global lifecycle, authority, writer, review, recovery, evidence, PR,
waiting, and cleanup semantics come from the installed kit roles and generated
workflow.

## Context Sources

- Repository rules and gotchas: `AGENTS.md`.
- Profile and node budgets:
  `.kent/workflow-profile.toml` and `.kent/context/*.md`.
- Android workflow index:
  `.kent/skills/appsome-android-workflow/SKILL.md`.
- Jira: `.kent/adapters/jira/jira-api.sh`.
- Sentry issues: `.kent/adapters/sentry/sentry-issues.sh`.
- MCP policy and Figma wrapper: `.kent/adapters/mcp/`.
- Mobile lease, preservation-install, and evidence adapters:
  `.kent/adapters/mobile/`.

The active manifest is the read allowlist. Load a recipe or external source
only when its trigger matches the current node and task.

## Planning And Identity

- Appsome feature artifacts live under `.todo/<feature>/`; `plan.md` owns
  writer-step progress and `meta.json` stores identity/source metadata only.
- Jira-backed implementation scope contains only root issues explicitly named
  by the task source/body or an exact human-authored task comment. Parent,
  linked, cloned, sibling, and dependency issues are evidence or blockers, not
  implicit requirements. Plan records included roots, related evidence,
  dependencies, and deferred issues separately.
- Jira-backed Plan inspects normalized issue relations before heuristic
  cross-repository search. A linked sibling implementation is bounded product
  evidence and records immutable commit/paths plus
  `Checked`/`Adopted`/`Rejected`/`Conflicts` conclusions.
- Kent keeps `OSM-*` as task identity. After Plan, a Jira task branch resolves
  to `feature/<Jira-key>` from the source URL or first authoritative task-body
  Jira URL. Comparison, dependency, and evidence keys do not define identity.
- When that canonical branch is intentionally retained for comparison, one
  standalone human-authored `branch_name: feature/<Jira-key>-<suffix>`
  directive may select a collision-free branch while preserving Jira identity.
- Missing Jira identity keeps the Kent branch. Existing local or remote
  collisions block without ref reuse.
- An explicitly Sentry-backed Plan stores bounded normalized issue/latest-event
  context before marking that exact issue seen. Candidate search never adds
  implementation scope implicitly.

## Planning Lifecycle Feasibility

Before accepting the execution plan, compare the required downstream stages
with actual Task authority and the selected workflow. Diagnose technical
ordering within already-authorized scope; do not ask again for a decision
whose original source is available. In particular, distinguish:

- mocked `.uitest` harness reset and its explicit data boundary from ordinary
  stage-app data, device wipe or live backend actions;
- named emulator UI evidence from authenticated subscription/filing behavior;
- diagnostic-only CI publication from a workflow route requiring a PR;
- retaining an existing delivery branch from Janitor's branch-deletion policy.

Inspect facts without running builds, devices or publication in Plan. Request
only genuinely missing effect authority, with the known consequences of that
decision. An unexecutable route is a capability gap, not another permission
question. These checks grant no reset, emulator-start, push, PR or cleanup
permission and do not override existing data-preservation rules.

When a generated Fix/Compliance node exposes `replan`, an obsolete accepted
execution plan returns to retained Plan Revalidation before source edits.
Use the shared accepted-snapshot and provenance contract; preserve immutable
baseline, existing work and all project review/acceptance gates. Persist
remaining findings and evidence pointers in the feature plan before normal
writer continuation. Do not change source scope merely to reorder progress.
Older locked graphs lacking this route require separate compatible rollout,
not manual Task movement or repeated approval of the same unresolved blocker.

If a requested diagnostic job cannot run from the planned PR, compare its actual
triggers and narrowly scoped project-source adaptations before proposing an
external operator Session. Such a proposal is not authority to edit triggers:
explain any changed source pin, run count or side effects and obtain the precise
decision if it exceeds existing scope. A PR event does not inherently guarantee
a single diagnostic run.

## Build And API Sources

- Main compile task: `:app:compileDevDebugKotlin`.
- The primary checkout may use `./gradlew`; every worktree uses
  `./tools/agentw`, whose non-secret fallback writes only `sdk.dir`.
- Deterministic workflow verification uses the command selected by the profile.
- The related `osome-kmp-mobile-shared` checkout is authoritative for generated
  API types. Follow the search and regeneration order in `AGENTS.md` before
  declaring an endpoint, model, or field absent.
- Preserve generated provider, status, action, flow, placement, and intent
  enums through the Appsome domain. Do not replace them with normalized string
  routing when a typed generated contract exists.
- Plan every SDK/schema version change and every required adaptation outside
  the root issue as an explicit dependency-impact slice. Unrelated product
  behavior remains a separate task even when an SDK upgrade makes it compile.

## Mock-Scenario Evidence

`.kent/commands/mock-scenario-policy.md` applies to all six supported work
kinds; applicability follows each changed behavior, not the task label. Plan
inventories stable behavior IDs and expected scenarios. Implement refreshes the
inventory against the final source delta and records selected-case execution;
every changed source path must be mapped, and non-applicability requires a
concrete per-behavior rationale plus alternative verification.

Immediately before compile-verifier handoff, provide `review_context` as the
policy's closed JSON envelope string, with a workspace-relative packet path,
digest, and non-empty retained narrative. Keep packets, execution receipts,
and logs under ignored `build/kent-workflow/<task>/`. Fix/re-entry recovers the
durable packet pointer and refreshes evidence after relevant changes; a lost
envelope is writer-recoverable, while missing or invalid evidence blocks.
The compile child validates evidence before compilation can pass and preserves
the existing `passed`/`failed`/`blocked` transition-only stdout contract.

## Runtime Smoke

- Conditional routing follows `.kent/commands/smoke-policy.md`; execution
  follows `.kent/commands/smoke-test.md`.
- Use only an eligible Android emulator selected by exact serial. Physical
  devices require explicit authorization.
- Acquire the shared lease before install, launch, input, logs, or Mobile MCP.
  On resume, reuse a still-owned checkpoint token through the adapter's
  `resume` operation.
- Build a fresh task APK, but preserve app data by installing only through
  `.kent/adapters/mobile/android-apk-install-preserve`. Unknown signer,
  downgrade, signer mismatch, or install failure blocks replacement. Uninstall,
  package-data clear, downgrade, signer replacement, or another destructive
  reset requires a separate explicit authorization.
- A resumed Smoke run skips duplicate build/install when its ignored checkpoint
  proves the same APK digest was already installed and the required
  authenticated state remains available.
- Checkpoints and task comments record only authorization scope and
  `authenticated`/`unauthenticated`/`unknown` state, never credentials.
- Pass the project mobile evidence audit before transition.

## External Systems And Delivery

- Jira reads are available to Engineering Delivery. Create, edit, comment, and
  transition mutations require an exact user/workflow approval, a dry-run
  preview, and adapter `--allow-mutate`. Natural-language Jira writes default
  to English; another language additionally requires `--allow-non-english`.
- An explicitly Sentry-backed task may mark only its exact issue seen/unseen
  after durable context exists. Resolve is eligible only after the fix is
  merged; mute is eligible only after an explicit no-action decision.
  Resolve, mute, and unresolve require exact approval, dry-run, and
  `--allow-mutate`.
- MCP calls use `~/.kent/bin/kent-mcp-call` and
  `~/.kent/bin/kent-mcp-list`.
- Credentials, local reference paths, private MCP config, and broad/raw
  authenticated responses stay outside Git and workflow evidence.
- Appsome PR delivery uses rebase and requires method-specific feasibility.
- Release preparation/publication remains separate from Engineering Delivery.
  If no authoritative source gives another ordinary release date, use the
  current execution date.

## Selected Release Publication

The selected non-default workflow is
`0f166719-6f60-479a-9abe-d140ab84c2cf`, native revision 6, with 22 nodes
and 54 edges. Its source contract explicitly represents native
Agent approvals. It supports `prepared_release` and publication-only
`current_master_hotfix`. The latter requires an already target-version master;
it does not prepare or merge a release PR.

`.kent/commands/release-publication.md` owns mode boundaries, exact approval
semantics, cleanup procedures and admission blockers. The builder, spec,
snapshot and manifest selected by `.kent/workflow-profile.toml` form the active
source set. Historical S07 files retain their old identity as audit sources,
not operational authority. Their strict Script materialization guarantees
must not be attributed to the current workflow.

This source migration does not apply a graph, change a default, relock a Task
or authorize release execution. Publication remains blocked pending separate
native qualification and compatible installed-tooling admission.

## Runtime Source And Activation Boundary

The Delivery continuity cohort is selected by the exact published Kit pin
in the runtime-cohort tests and the hash-bound v28 activation. Profile
`prepare_ci` version `2.0.0` owns Delivery CI input preparation; runtime-v2
verification and waiting consume the resulting typed contracts. The private
compile child exposes only a transition on stdout, with diagnostics on stderr.

Task-runtime cleanup admits safe, owned ordinary files independently of their
names and formats, including plan snapshots and temporary payloads. Opaque
contents are not read or parsed. Managed cleanup retains them with terminal
evidence until Kent confirms worktree deletion; primary cleanup removes the
ledger last. Reserved CI evidence remains validated. Unsafe types, foreign
or tracked files, unsealed state and observed drift still block cleanup.
Nested directories are outside this file-only contract. Locked Tasks retain
their selected source; delivering this cohort does not recover a live Task.

Portable Delivery v28 source is
`.kent/workflows/specs/appsome-engineering-delivery-v28.json`, generated by
`.kent/workflows/builders/appsome_delivery_v28.py`. Its unbound Plan is
deliberately blocked; a verified activation provisions the pinned adapter
cohort before dependent Scripts, even on older release-based worktrees.
Source alone is not a live workflow/default assignment. Historical v26 source
is retained unchanged. Schema-3 release source qualification requires a
compatible Kit reader independently of the generic runtime cohort. Its source
test override does not reconfigure installed CI/preflight launchers, relock a
Task or establish operational readiness. Use explicit compatible executables
for candidate qualification; publication/provisioning and default-launcher
activation require separate approval. Keep historical S07 exact-pin tests
distinct from candidate schema-3 tests; do not weaken either to hide drift.
See `.kent/workflows/README.md` for the source/runtime distinction.
