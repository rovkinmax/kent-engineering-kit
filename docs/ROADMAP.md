# Roadmap

## Phase 1: Global toolkit

Status: complete.

- Install one toolkit-specific model-invoked skill and explicit engineering prompts.
- Register reusable read-only subagent roles.
- Keep the Kent default system prompt intact.
- Upgrade Kent CLI/TUI, service, and Desktop as one 2.3 compatibility set.
- Restart Kent once after configuration changes and the coordinated upgrade.

## Phase 2: Workflow generator

Status: complete. The generator has been exercised through the current
profile-schema-3 `Engineering Delivery v5` contract.

- Define a project profile schema.
- Generate project-local workflow instances from shared fragments.
- Apply an explicit execution-target policy to every generated workflow.
- Validate live workflows and export audit snapshots.
- Encode Kent fan-out constraints in generation and validation.
- Validate worktree setup hooks against the Kent 2.3 payload contract.
- Reject unsupported graph drift before mutation and refuse graph mutation when
  task records already exist.

## Phase 3: Delivery workflows

Status: full Delivery v5 canaries complete. Engineering Delivery v5 is the
project default in Appsome and Puber. Obsolete infrastructure experiments were
removed after promotion; legacy Feature Delivery remains linked because it
owns real task history and provides rollback.

Context Diet v1 iterations A and B are implemented in the current candidate
line:

- global AGENTS retains only cross-project authority, safety, session,
  worktree, and context invariants;
- project AGENTS files are compact indexes plus repository gotchas;
- node context manifests gate Plan, Implement/Fix, Review, Smoke, and Delivery
  reads;
- task evidence is append-only and records project instruction bytes,
  duplicate reads, repeated questions, verification loops, and nullable Kent
  telemetry;
- mobile leases can resume with the checkpoint's existing token instead of
  self-blocking.

Iteration C remains deferred until real-task metrics exist: experiment with
Kent compaction thresholds and other global config one dimension at a time.
That iteration requires a Kent restart; A and B do not.

- Keep `Engineering Canary` and `Engineering Smoke Lab` generation available
  for bounded future validation without permanent project links.
- Re-export existing workflow snapshots and verify their preserved Source HEAD
  policies after upgrading Kent.
- Keep generated workflow tooling compatible with canonical bare UUID selectors
  and both historical prefixed and current bare IDs in audit snapshots.
- Enforce the shared role/model ownership contract in every project profile;
  migrate projects independently so active workflow worktrees are not touched.
- Canary the kit-owned global mcporter adapter with PUB-26, then migrate Puber
  and Appsome references from duplicated project wrappers. Keep task-backed
  revisions on their frozen local adapters until their work completes.
- Puber role-prompt cleanup and the Puber/Appsome project model overrides are
  staged. Appsome role prompts already omit model selection. Re-export its
  audit snapshots and add any missing project-contract wording only after its
  active sessions finish.
- Delivery v5 is task-backed and does not inherit new generated writer prompts.
  Puber Delivery v6 validated recovery-aware Plan against a local checkpoint
  and exact source-comment IDs, then stopped before implementation because
  fresh sessions also affected non-writer approval recovery. Its corrected
  replacement limits `fresh_per_slice` to Implement/Fix, preserves compacted
  continuity for Plan/Smoke/Compliance/PR/CI/Cleanup recovery, and uses
  `continue_fix` for bounded continuation. Validate the replacement before
  considering default promotion or an Appsome rollout.
- Validate two-step implementation continuation, verification fan-out/Join,
  and report-only cleanup in both Android projects.
- Validate interrupted-node recovery without losing the locked target or
  completed Join context in Appsome.
- Standardize active user-feedback delivery. Task comments are durable but are
  not injected into an active run; use session steering so the node can release
  resources and return through `needs_changes`. Add a canary proving that
  manual task movement is unnecessary for writer and Smoke feedback.
- PUB-26 dogfooding showed provider-failure gaps in writer, reviewer, and Smoke
  nodes. A resumed writer can preserve the worktree yet reopen its scope and
  repeat checks. A read-only reviewer can finish all inspections but lose the
  result during final response synthesis. Smoke can finish lock, build,
  install, launch, and target-selection attempts but fail before reporting
  whether destructive acceptance actions began. Task-local resumable Fix and
  Smoke checkpoints are now implemented through the profile-owned atomic
  checkpoint command. Writer checkpoints record the pinned
  baseline, completed scope, fresh green commands, and the single next
  permitted action. Reviewer checkpoints record inspected authority, evidence,
  and structured findings before final transition synthesis. Smoke checkpoints
  additionally record the exact device lock, target-confirmation state, and a
  destructive-action ledger for disposable test data. Validate that resume
  reconciles the checkpoint instead of repeating completed work, graph-owned
  review axes, or user-data mutations. Managed-worktree canaries remain.
- PUB-31 showed that a single broad Smoke request can grow to roughly 80k
  estimated provider tokens, with a compacted continuation consuming roughly
  43k more despite only one missing manual observation. Future Smoke prompts
  must use bounded acceptance stages, durable checkpoints, and direct
  manual-evidence handoff instead of exploratory continuation.
- PUB-31 also proved that GitHub `MERGEABLE/CLEAN` does not imply
  rebase-and-merge feasibility. Its PR reported `canBeRebased=false`; an
  isolated forced replay reproduced conflicts hidden by the already-merged
  final tree. Add explicit merge strategy resolution and method-specific
  checks, then canary them in a future non-default Puber workflow. Keep
  task-backed Delivery v7 frozen.
- PUB-31's post-repair Standards pass exposed another calibration defect:
  full-project Detekt failed with 118 issues, the reviewer admitted the issues
  might predate the task, but Gate still created broad Fix work and the writer
  began refactoring a shared UI component. Standards and Gate must require
  baseline-differential proof before `needs_changes`; baseline debt or an
  absolute-policy contradiction must never authorize repository-wide cleanup.
  Machine-readable baseline/candidate reports later proved three actual
  worsened metrics despite the total improving from 127 to 118, so differential
  policy must compare declaration-level measurements rather than approximate
  line counts.
- Add deterministic guards before the next Puber canary: a GitHub merge-policy
  resolver that cannot guess ambiguous methods, and verification-dispatch
  validation that rejects `.todo` or foreign `workspace_path` values before
  fan-out.
- PUB-26 validated state preservation and in-place compaction for an
  interrupted Fix node: a manual move through its approval-gated
  `compact_and_continue_session` self-loop preserved the worktree, injected the
  exact authoritative task-comment ID, refreshed the session lock, and emitted
  a completed compaction over 24 prior model turns. Kent retained the same
  session ID and cumulative request count. The recovered broad Fix later grew
  to about 229k estimated provider tokens and still failed on an OpenAI server
  error before persisting its transition. Therefore this validates recovery
  mechanics, not execution reliability. The next canary must combine compaction
  with durable checkpoints, authoritative artifact updates, and bounded
  remaining work. Still validate a no-mutation ledger before using recovery
  for destructive Smoke work.
- Preserve the PUB-26 checkpoint on local branch
  `recovery/pub-26-history` at commit
  `09c6dd332d214f358d5dbe564e44dc980457789b`. Keep PUB-26 retained until its
  v6 successor has locked that revision and imported the exact authority
  comments into a refreshed artifact set.
- Add supported effective-policy observability for workflow sessions. Current
  task and session inspection proves the model family but does not expose the
  effective reasoning level, so Sol alone must not be reported as proof of
  Sol/medium. Also canary `reviewer.frequency`, callability, and workflow
  subagent eligibility after the next restart.
- Re-canary Kent worktree hooks independently from project correctness.
  `setup_script` and `postprocess_hook` remain advisory integrations; project
  build entrypoints must retain their idempotent SDK/bootstrap fallback and
  must not depend on hook invocation.
- Kent 2.5 migration proved that Resume success means only durable requeueing:
  migrated Agent nodes can immediately fail without an assigned Session, while
  Script nodes can execute an old task-worktree copy and emit a now-invalid
  Transition key. Keep task-backed graphs frozen, preflight project-owned
  command wrappers, verify post-Resume Task state, and use the documented
  fresh-session/fan-out recovery path. Missing agent-owned red-run evidence is
  never a user approval.
- OSM-47 exposed an unnecessary release-date approval. Operational timestamps
  now default to the execution environment's current calendar date when no
  authoritative source specifies another value; business dates and changes to
  an existing external record remain explicit decisions.
- Appsome runs 30977709227 and 30978052315 showed self-hosted Gradle jobs
  receiving a runner shutdown signal and ending with
  `The operation was canceled`. Release unit passed on its third attempt;
  PR unit passed on its second retry and Detekt on its first. CI Monitor now
  owns up to two exact-job retries for this proven infrastructure signature
  while preserving genuine sibling failures.
- OSDK-4 proved that an approval-gated publisher cannot assume the interactive
  `gh auth` token is the package credential. Publication now resolves the
  project-declared secret just in time, verifies its actor and registry access,
  maps it into the build's expected environment only for the publish
  subprocess, and clears it afterward. A future profile-schema iteration may
  validate this mapping structurally after the contract has seen more use.
- Validate the updated Standards calibration in a newly created session.
  Ordinary technical identifiers are not secrets or PII by default, and
  security severity requires an applicable rule or concrete threat model.
  Resumed sessions retain their old locked prompt and are not evidence that the
  new calibration was consumed.
- Preflight each selected execution revision against its profile-owned scripts,
  procedures, and adapters.
- A synthetic August 5, 2026 canary proved that Kent 2.5 may materialize a
  managed worktree yet omit the execution root for a relative Script used
  directly after Backlog. Runtime fails with
  `relative script_path ... requires a task worktree root` and can leave stale
  unlocked execution-target facts. Generated branch identity therefore runs
  after read-only Plan and before Implement, preserving the complete writer
  handoff. Canary that exact topology before default promotion.
- Observe ordinary default tasks across delivery-ready, Smoke, Fix, CI, and PR
  feedback paths.
- The generated evidence-repair lane now handles packaging-only Compliance
  failures. When the
  source diff and substantive verification are already green, Fix should repair
  only missing, empty, or contradictory reports/checklists, rerun the evidence
  audit, and return directly to final Compliance. It must not repeat source
  verification, rebuild/reinstall the app, or reacquire an emulator unless the
  repaired artifact changes the underlying acceptance result. Canary direct
  Compliance re-entry before default promotion.
- PUB-37 showed that Plan can invent product authority by writing "the user
  clarified" into a specification without an exact human-authored comment.
  Enforce authority provenance in Plan, Specification Review, and Compliance:
  task-body narrowing requires an exact human comment ID or another explicit
  authoritative source and otherwise blocks before implementation.
- Waiting PR now hands an open feasible GitHub PR to a deterministic script
  watcher. Unchanged state consumes no approval or model turn; material state
  changes wake the retained evaluator, and merge goes directly to Cleanup.
  Canary interruption/restart behavior before default promotion.
- PUB-37 proved that report-only Cleanup does not match the desired operating
  model: the task reached Done after merge while its managed worktree and local
  task branch remained. A two-phase task-owned Janitor is now generated after
  the resource-owning Cleanup session exits. It proves the PR is merged, the
  remote
  task branch is absent or safely deletable, and every dirty/untracked path is
  either ignored evidence or byte-equivalent to the current merged target
  before removing the managed worktree and local task branch. Preserve and
  reports any unique unmerged content instead of deleting it. Destructive
  promotion remains blocked on a disposable managed-worktree canary proving
  Kent permits deletion from the following script node.
- OSM-33 showed a late-CI routing failure: GitHub merged PR #1537, then the
  existing CI node observed an unrelated UI-test failure and returned the
  already delivered task to Fix. CI must query merge state first, must not
  mutate a merged delivery, and needs task-differential evidence before
  labeling any open-PR failure task-scoped. Track actionable post-merge
  failures as separate follow-up tasks.
- Move the Appsome project adapter from `release/4.29.0` into `master`; until
  then, start generated Appsome workflows only from audited adapter commit
  `b6fd03e1f15dc49bbe9431955062699f8bf6bfb0` or its descendants.
- Roll out required work-kind dispatch, migrate remaining backlog tasks, and
  retire the covered feature/refactor/bugfix/dependency/test workflows.

## Phase 4: Auxiliary workflows

Status: in progress.

- Generate maintenance, smoke, intake, release, and rebase flows.
- Support single and split release topologies.
- Roll out the canonical `release-manager` role and direct specialist routing
  for existing Puber/Appsome release, SDK update, and branch-rebase workflows.
  Validate the staged candidates after the next Kent restart, then retire the
  generic-`default` predecessors.
- OSDK-1 validated the generic Delivery topology in a Kotlin SDK generator:
  managed-worktree bootstrap, writer handoff, deterministic/reviewer fan-out,
  Join, Gate, Fix feedback, human clarification, and report-only Cleanup all
  completed successfully.
- Add Web, iOS, embedded, and generic project profiles.

## Post-adoption backlog

Do not block the first real `work_kind` rollout on these items. Revisit them
after Puber and Appsome have exercised the generalized Delivery workflow:

- normalize experimental workflow names and remove temporary duplicate assets;
- extract cleaner cross-stack procedure templates only from repeated real use;
- evaluate absorbing Appsome SDK Update as `work_kind = "sdk_update"`;
- extend project adapters for Web, iOS, and IoT from actual repositories rather
  than speculative platform abstractions;
- make `kent-engineering-kit` itself a generated Engineering Delivery consumer
  with the common work kinds, `scripts/validate` as deterministic verification,
  no runtime Smoke, and a normal GitHub PR/CI tail;
- version the profile and compatibility contract only after the working
  composition and migration policy are stable.
- Add a supported structured task-comment listing path that exposes canonical
  comment IDs. Kent 2.4 `kent task comment list` omits them, while exact human
  authority provenance currently requires a read-only metadata lookup and
  active-session steer.
- Clarify or wrap `kent run wait` for workflow-owned `shell_command` sessions:
  Kent 2.4 reports `session runtime live run completed without a final answer`
  even after the node has successfully applied its workflow transition.
