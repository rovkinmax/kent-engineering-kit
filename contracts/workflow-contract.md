# Workflow Contract

The common workflow layer is platform-neutral. Projects supply build, test,
device, source-control, issue-tracker, and release adapters.

## Lifecycle

- Kent task state owns workflow lifecycle.
- Kent owns the selected execution target, execution root, and resolved Git
  commit. Project metadata must not mirror those facts.
- A project artifact may own writer-owned implementation-step progress.
  Workflow-owned Standards, Specification, Gate, Smoke, Compliance, PR, CI,
  and Cleanup work is never an implementation checkbox prerequisite.
- Recoverable blockers use approval-gated `needs_user_action` self-loops.
- A post-Join blocker may approval-loop through verification dispatch so every
  read-only branch reruns with fresh state.
- An interrupted node run is runtime state, not a workflow decision. Inspect
  the interruption reason before changing task state. For a transient provider
  or transport failure, resume the interrupted node on its locked execution
  target; do not move the task or rerun completed upstream branches.
- Resume reports durable requeueing before Session or Script startup finishes.
  Recheck Task state after startup. `workflow_runtime_start_failed`,
  `workflow_script_completion_failed`, or another immediate interruption is a
  failed recovery even when the Resume command itself succeeded.
- A Current Node migrated without an assigned or retained Session cannot be
  repaired by repeated Resume. Re-enter the smallest supported incoming
  `new_session` Transition with preserved values. For a failed fan-out branch,
  re-enter the fan-out source so every sibling and Join invariant is recreated.
- Transition keys, Script stdout, prompts, and persisted prior-value keys are
  one versioned contract. Active workflows are frozen. Normal changes use a new
  workflow version; taskless migration is allowed only after a complete
  preflight proves that no Task references the graph.
- `wont_do` is terminal, requires an explicit cancellation decision, and emits
  `closure_reason`.
- Parallel verification branches are read-only.
- One writer owns fixes and integration.
- `done` means delivered or explicitly approved report-only completion.

## User-facing workflow communication

- Direct questions, transition commentary, `blocker_reason`, `closure_reason`,
  approval summaries, and Needs User Action text use the user's preferred
  conversation language. The installed global contract defaults these surfaces
  to Russian for this user.
- Code, commands, identifiers, structured parameter keys, and repository
  artifacts retain their project-defined language.
- Approval text is concise and decision-oriented. It states what the user must
  decide or do, why it is needed, and what happens after confirmation.
- Number independent decisions. Keep full verification reports in their
  structured report/context parameters rather than pasting them into the
  approval summary.
- Task-scoped source fixes belong in `fix_context`; they are not described as
  actions the user must perform. External dependencies only block the stage
  whose acceptance contract actually requires them.
- Missing agent-produced evidence is not a user decision. A pre-edit red-run
  requirement must be captured before the first production edit. If the agent
  missed it, the workflow records the absence and uses bounded reconstruction
  or current deterministic evidence; it asks the user only when acceptance
  genuinely depends on an external fact or product decision.
- Operational dates default deterministically. If a mutation requires only the
  date on which it is being performed and no authoritative source specifies a
  different date, resolve the current calendar date from the execution
  environment immediately before dry-run and mutation. Do not request approval
  merely to select today's date. Ask when the date represents a deadline,
  effective period, backdate, scheduled release, or another independent
  business choice, or when changing an existing external record's date.

## Writer session policy

- `policies.writer_sessions = "continuous"` preserves the historical behavior:
  writer transitions reuse or compact an existing session.
- `policies.writer_sessions = "fresh_per_slice"` starts a fresh session for
  every Implement step and every Fix slice, including CI, Smoke, Compliance,
  PR-feedback, and PR-recovery fixes.
- The policy does not alter approval-recovery continuity for Plan, Smoke,
  Compliance, PR preparation/monitoring, CI monitoring, or Cleanup. Those
  non-writer loops compact and continue their current session.
- A fresh writer treats the preserved worktree, authoritative
  design/specification/plan, exact task-comment IDs, existing evidence, and
  structured transition parameters as its complete handoff.
- Each fresh Implement session completes one ready writer-owned plan step.
  Unchecked workflow-owned verification or delivery items in a legacy plan are
  handoff scope, not writer work; when no writer-owned step remains, Implement
  transitions to verification. Each fresh Fix session completes one
  independently verifiable fix slice and uses
  `continue_fix` with only the remaining findings when more work remains.
- A same-node `continue_fix` checkpoint action is consumed when the transition
  creates the next Fix session. Non-empty incoming findings are the new work
  assignment: the session rewrites `next_action` to one concrete slice and
  completes it. It must not emit bookkeeping-only evidence and repeat the same
  transition without production or verification work.
- Recovery-aware Plan may adopt an explicit checkpoint commit and source task.
  It verifies the checkout, updates one authoritative artifact set, records
  which newer comments supersede earlier decisions, and plans only remaining
  work without resetting preserved implementation.
- When a recovery task explicitly requests a Plan-only confirmation gate, Plan
  completes through `needs_user_action` instead of selecting Implement.
- Existing task-backed workflows retain their recorded writer policy. Changing
  this policy requires a new non-default workflow and managed-worktree canary.

## Portable parameters

- `workspace_path` — repository or managed-worktree root, never a `.todo`
  feature directory or another artifact path
- `plan_path`
- `work_kind` — stable project-profile key selected once during Plan and
  preserved through every Implement slice
- `plan_path` uses the literal `not-applicable` when an explicitly planless
  project flow is allowed. Required Kent Transition parameters are never empty.
- `spec_path`
- `fixed_point` — immutable task-delta baseline, normally Kent's resolved
  execution commit; it is distinct from the moving PR merge target
- `changed_files`
- `verification_report`
- `review_report`
- `standards_report`
- `compliance_report`
- `review_context`
- `fix_context`
- `verification_status`
- `standards_status`
- `spec_status`
- `smoke_report`
- `smoke_rationale`
- `smoke_scope`
- `blocker_reason`
- `pr_url`
- `branch_name`
- `merge_strategy`
- `pr_report`
- `ci_report`
- `merge_report`
- `publication_report`
- `closure_reason`
- `cleanup_report`
- `evidence_context`
- `pr_head_oid`
- `pr_base_oid`
- `cleanup_mode`
- `cleanup_session_id`

## Node context manifests and evidence ledger

- Every generated project declares exactly five node context manifests:
  `plan`, `implement`, `review`, `smoke`, and `delivery`.
- The manifest is the node's context budget. It names required sources,
  conditionally triggered sources, and material that must not be preloaded.
  Incoming edge prompts carry dynamic task values instead of restating project
  documentation.
- Agent nodes append one non-empty event before every workflow transition
  through the profile-owned evidence command. Evidence is JSONL, hash-chained,
  Git-ignored, and append-only. A later slice never edits an earlier event.
- Each event records task/node identity, Git HEAD, summary, artifacts, checks,
  decisions, exact project instruction files read, project instruction bytes,
  repeated reads, repeated questions, and verification loops.
- `model_calls` and `compaction_count` remain nullable until Kent exposes
  stable session telemetry to workflow commands. Unknown values are recorded
  as `null`, never inferred.
- The ledger is concise metadata and evidence, not a transcript. It never
  stores secrets, raw authenticated state, broad logs, or unredacted network
  responses.

## Branch identity

- Kent task short ID is the stable workflow/lifecycle identity. It remains the
  key for task comments, checkpoints, runtime state, and Kent commands even
  when the Git branch has another name.
- `policies.branch_identity = "task"` keeps Kent's native short-ID branch.
- `jira` resolves `feature/<KEY>` from the task source URL first, otherwise
  from the first Jira key in task-body order.
- `github_issue` resolves `issue-<number>` only from an exact issue URL in the
  current GitHub repository. Source URL is inspected before task-body URLs.
- If no usable external identity exists, retain the Kent short-ID branch.
- Branch identity runs as a deterministic Script after read-only Plan and
  before the first Implement writer. Plan passes `workspace_path`, `plan_path`,
  and `work_kind`; the Script preserves that handoff on every outcome.
- Kent 2.5 does not provide a task execution root to a relative Script used as
  the first executable node after Backlog. Generated workflows must establish
  the managed root through Plan before invoking project-relative Scripts.
  Active task branches are never renamed during workflow migration.
- An existing local or remote desired branch is ambiguous ownership, not an
  invitation to reuse or overwrite it. Route to a recoverable user decision.
- PR preparation reports the actual `git branch --show-current`; downstream
  CI, merge watching, and Cleanup never reconstruct branch identity from the
  Kent short ID.

## Approval-gated package publication

- `release_topology = "manual-package-publish-after-main"` inserts a dedicated
  `Publish Package` node after confirmed PR merge and before Cleanup.
- Every merged-PR route into publication requires explicit user approval.
- The profile must declare `procedures.publish` and the
  `roles.package_release` role.
- Publication runs only from a clean checkout of the exact merged source and
  only for package identity, version, destination, override mechanism, and tag
  policy explicitly authorized in the task body or an exact human-authored
  task comment.
- The project publish procedure declares the credential source and the
  build-tool environment mapping. The publisher resolves that source just in
  time, verifies its principal and required registry access without exposing
  the secret, injects it only into the authorized publish subprocess, and
  clears it afterward. Ambient CLI authentication is not a substitute.
- The publisher checks remote state before every mutation and after success.
  Existing, partial, conflicting, or unverifiable versions block without
  overwrite or deletion.
- Publication failures retain the task worktree and compact the publisher
  session behind another approval. Cleanup starts only after a non-empty
  `publication_report` proves the remote package.

## Execution targets

- Generated workflows always set an explicit Kent 2.3 execution-target policy.
- The profile supplies a default and may override it by workflow kind.
- Supported policy values are `ask-on-first-execution`, `none`, `head`,
  `default-branch`, and `ref:<revision>`.
- Delivery workflows should normally ask on first execution.
- Canary workflows should normally use Source HEAD.
- Trunk maintenance should normally use the repository default branch.
- Release and hotfix workflows should use an explicitly selected revision.
- `none` is reserved for intentional source-workspace execution, including
  non-Git workspaces and small local jobs that do not need isolation.
- A task-level start, approval, or move override may select a concrete target
  without mutating the workflow policy.
- Verification dispatch deterministically compares `workspace_path` with the
  canonical current execution root. Artifact subdirectories, nested paths, and
  foreign repositories are rejected before fan-out and routed through a
  metadata-only Fix slice.

## Work-kind routing

- Every generated Engineering Delivery profile declares at least one
  `[work_kinds.<key>]` entry with a description and project-relative Plan and
  Implement procedures.
- A task-body `work_kind: <key>` declaration is authoritative when supported.
  Otherwise Plan classifies conservatively and blocks on ambiguity.
- Plan uses only planning sections of a combined procedure. Implement uses the
  selected procedure for exactly one approved plan step and never repeats
  discovery or planning.
- `work_kind` is required on Plan-to-Implement, Implement continuation, and
  Implement recovery transitions. Fix remains findings-driven and generic.

## Fan-out constraints

- Every branch transitions directly to its Join.
- Branch failures are reported to the Join as data.
- A branch emits one stable parameter contract on every completion.
- Only the post-Join gate chooses Fix, QA, Ship, or Needs User Action.
- Early Standards Review emits `standards_status` and `standards_report`.
  `compliance_report` is reserved for the final delivery attestation.
- This output split applies to current and future generated graphs. Frozen
  schema-3 Canary/Smoke graphs created before the split may retain
  `compliance_report` as their historical early-Standards output.
- Standards findings are differential to the pinned comparison baseline.
  Whole-repository analyzer failures become `needs_changes` only when a new or
  worsened task violation is proven. Pre-existing debt is non-blocking for the
  task. An explicit absolute-clean policy that the baseline itself violates is
  a `blocked` policy contradiction and routes to user resolution, never broad
  Fix work.
- Standards and Specification use `fixed_point` or the Kent-resolved execution
  commit for task-delta review. The current merge-target tip is checked
  separately for integration compatibility. Target-only commits added after the
  fixed point are not task regressions unless a three-way merge or
  method-specific replay proves a conflict or loss in the delivered tree.
  Reviewers and Fix must not copy unrelated target files merely to make an old
  task checkout resemble the moving target.

## Active feedback and recovery

- Task comments are durable records but do not interrupt or update the context
  of an already running node.
- User feedback for an active run is delivered through `kent run steer` or the
  equivalent interactive session message. The node then exits through its
  declared transition with refreshed structured context.
- When feedback changes a product decision or acceptance criterion, the next
  writer updates the authoritative task design/specification/plan first and
  references the exact task-comment ID. Code, review context, and checkpoints
  refer to that artifact instead of creating independent copies of the
  decision.
- A design, specification, or plan may narrow or supersede the task body only
  with an exact human-authored task-comment ID or another explicit
  authoritative source. Agent-authored comments, implementation inference, and
  unsupported prose such as "the user clarified" are not authority. Plan or
  Specification Review returns `needs_user_action` before implementation when
  that provenance is missing.
- Resource-owning nodes such as Smoke must release locks, preserve required
  authentication and app data, record whether any destructive action began,
  and finish evidence hygiene before returning `needs_changes`.
- Do not use a manual task move merely to inject feedback into an active node;
  it can bypass node-owned cleanup and evidence reporting.
- Resuming an interrupted run may reuse the same session and therefore retains
  its locked prompt and execution settings. Prompt-policy fixes apply only to a
  newly created session. Repeated provider failures require checkpoint-aware
  recovery rather than assuming Resume creates a clean runtime.
- An approval-gated `compact_and_continue_session` recovery may compact in
  place under the same session ID. Treat `context_compaction_completed` plus a
  refreshed session lock as the recovery evidence. The lifetime
  `model_request_count` remains cumulative and is not evidence that the
  compacted context is still large.
- Compaction proves context reduction and worktree continuity, not provider
  reliability. A broad recovered node can grow large again and fail before its
  transition persists. Pair compaction with durable checkpoints, authoritative
  artifact updates, and bounded remaining work.
- Fix and Smoke checkpoints live under the ignored
  `.kent/runtime/<task-short-id>/` directory and are written atomically through
  the profile-owned checkpoint command. They contain one next action plus
  completed, remaining, and mutation-ledger arrays. Checkpoints never contain
  credentials, authenticated UI payloads, broad logs, or raw evidence.
- A resumed Fix or Smoke stage validates and reconciles its checkpoint against
  current Git, task, device, lock, and evidence state before skipping any work.
  A mismatch invalidates the skip decision; it does not authorize repetition of
  a recorded external mutation.
- If the same checkpoint token still owns a Smoke resource, recovery renews
  that lease through the adapter's resume operation instead of acquiring the
  resource against itself.

## Final compliance

- A PR-producing Delivery workflow with `compliance_review` enabled routes both
  Gate `delivery_ready` and Smoke `passed` through a distinct final Compliance
  Review before PR preparation.
- Compliance is a thin read-only attestation over the final diff, authority
  sources, enabled verification reports, Gate decision, and Smoke evidence or
  bypass rationale. It does not repeat general standards, specification, code,
  architecture, or runtime review.
- `ship_pr` advances to PR preparation with `compliance_report`.
- `needs_changes` returns to the single-writer Fix stage and then reruns the
  full verification fan-out.
- `needs_user_action` is an approval-gated Compliance self-loop. `wont_do`
  remains approval-gated terminal cancellation.
- During the schema-3 experiment, a profile that omits `standards_review`
  retains the legacy meaning: its old `compliance_review` value controls the
  early Standards branch and final Compliance remains disabled. Projects opt
  into the split by declaring both capabilities explicitly.
- Packaging-only Compliance defects use `repair_evidence`, not normal Fix,
  when substantive source, verification, Gate, and Smoke decisions are already
  valid. Evidence Repair may edit only named ignored reports, summaries,
  checklists, or indexes and rerun their audit. It never rebuilds, reinstalls,
  reacquires a device, or changes the underlying acceptance result. It returns
  directly to final Compliance. Any substantive defect routes to normal Fix and
  the full verification flow.

## Pull-request merge strategy

- `policies.pr_merge_strategy` accepts `auto`, `merge`, `squash`, or `rebase`
  and defaults to `auto`.
- Prepare PR resolves the strategy once and carries the resolved
  `merge_strategy` through CI and Waiting PR.
- `auto` intersects repository-enabled methods, target-branch protection and
  rulesets, and any required merge-queue method. Exactly one method must
  remain. Zero or multiple candidates require `needs_user_action`; agents never
  infer the user's preferred button or guess a repository default.
- GitHub resolution is performed by the kit-owned
  `kent-resolve-github-merge-strategy` adapter over captured API evidence. Its
  structured result, not an agent's interpretation, selects or blocks the
  strategy.
- An explicit method is still validated against repository capabilities and
  target-branch policy.
- Generic final-tree mergeability is not method feasibility. Merge requires
  merge commits to be allowed; squash requires squash merging to be allowed;
  rebase requires the PR commits to replay cleanly onto the current target.
  On GitHub, `canBeRebased=true` is required for rebase delivery.
  `MERGEABLE/CLEAN`, a clean merge-tree, or target-ancestor proof cannot
  override `canBeRebased=false`.
- Conflicting rebase signals are reproduced in an isolated temporary clone or
  branch with a forced replay. Diagnosis never mutates the task branch.
- A history rewrite or force-push requires exact user authorization. Preserve
  the old remote head in a local backup, pin the expected remote head, prove
  the authorized final-tree invariant, and update only the task branch with
  force-with-lease. Any lease, tree, target-tip, or authorization mismatch
  returns `needs_user_action`.
- CI checks authoritative PR state before classifying failures. If the PR is
  already merged, a late failed check never returns the merged task to Fix;
  Cleanup receives the merge proof and late-CI report, while any actionable
  regression is tracked as a separate follow-up task. While a PR remains open,
  `needs_changes` requires task-differential evidence. Baseline, flaky,
  unrelated, or unattributed failures use `needs_user_action`.
- Pending, queued, and in-progress CI are not blockers or transitions. CI uses
  one blocking first-party watcher for the exact PR/run and waits for terminal
  green/red/canceled state without spending a model turn per poll.
- CI may automatically retry an exact failed GitHub Actions job when
  first-party job metadata and bounded logs prove an infrastructure
  cancellation: the execution step is `cancelled` or bounded logs report
  `The runner has received a shutdown signal` / `The operation was canceled`,
  no test/analyzer/compiler diagnostic exists, the run and head SHA are
  unchanged, and the run was not user-cancelled or superseded. Retry only that
  job with `gh run rerun <run-id> --job <job-id>`,
  at most twice per logical job, waiting and re-reading authoritative state
  after each attempt. Genuine or ambiguous failures and exhausted retries are
  never hidden by automatic reruns.
  `needs_user_action` is reserved for authentication, access, ambiguous
  identity, contradictory policy, or another actual human decision.
- An open, green, method-feasible pull request transitions from Waiting PR to
  a deterministic merge watcher. The watcher performs no model turns or user
  approvals while relevant state is unchanged. It wakes Waiting PR only for a
  changed head, requested changes, merge conflict, lost method feasibility,
  closure, access failure, or another material event. A confirmed merge goes
  directly to Cleanup.
- Fix exposes a `pr_merged` recovery edge to Cleanup for a task that was
  already delivered before stale or late workflow routing reached the writer.
  The edge requires authoritative PR URL, branch, and merge proof; it never
  resumes implementation or repeats verification on the merged task branch.
- Task-backed workflows keep their recorded PR prompts. Apply this contract in
  a new non-default workflow and canary it before promotion.
- When the task resolves an issue in the same repository, Prepare PR adds the
  source-control provider's closing reference (`Fixes #N` on GitHub). A
  cross-repository, partial, or follow-up relationship is linked without a
  closing keyword.

## Smoke policy

- Project profile schema 3 declares one Smoke policy: `disabled`,
  `conditional`, or `required`.
- `conditional` keeps the decision in the post-Join gate.
- Runtime or user-observable impact, explicit acceptance criteria, and
  uncertainty force Smoke.
- A bypass requires positive evidence that the change cannot affect a runtime
  artifact or user-observable behavior.
- Resource unavailability never downgrades a Smoke requirement; the Smoke node
  routes it to `needs_user_action`.
- Runtime evidence is least-privilege. Do not persist unfiltered device or
  system logs, network payloads, authentication headers, or full UI dumps from
  an unexpected authenticated or otherwise sensitive state.
- The global MCP adapter logs call metadata and creates no separate raw
  artifact by default, but normal stdout remains in the Kent shell transcript.
  Sensitive calls use quiet, digest, or assertion output modes. Raw stdout or
  artifact retention requires explicit confirmation that the response is both
  necessary and non-sensitive.
- When runtime proof needs identity-set comparison without disclosure, hash
  only an allowlisted semantic-token pattern inside the adapter and emit opaque
  hashes plus explicit pagination-marker booleans.
- Project procedures retain only the scoped liveness, crash, ANR, and
  acceptance evidence required for the decision. Unexpected sensitive state
  produces a redacted blocker and `needs_user_action`.
- Smoke runs are bounded acceptance checks, not open-ended exploration. Split
  broad runtime scopes into explicit stages or request focused manual evidence.
  If a durable user comment already supplies the only missing observation,
  consume it without rebuilding, reinstalling, or repeating navigation.
- Runtime interaction evidence follows `mobile-smoke-contract.md`.
  Source-derived exact focus routes and target-aware adaptive bursts are valid;
  ungrounded fixed loops, one model turn per key, and visibility-only
  assertions are not. A user-reported contradiction must be resolved by
  focused evidence before Compliance can advance delivery.
- Focused screenshots are normal Smoke inspection and require no approval.
  Scoped screenshots from a project-declared non-production stage/test
  environment with synthetic data may be retained as audited workflow evidence.
  Other retention and external publication follow `mobile-smoke-contract.md`.
- Gate and Smoke allocate evidence by modality. Runtime covers rendering,
  focus/navigation, integration, restoration, and liveness. Passing
  deterministic evidence may cover non-observable defaults, classification,
  filtering, paging, and state transitions unless explicit end-to-end
  acceptance requires runtime proof. The workflow never demands profile reset,
  special fixtures, or test-only product semantics merely to duplicate that
  evidence.
- Required summary, report, and checklist artifacts are non-empty before
  Compliance accepts them.
- Persist a concise checkpoint before a long Smoke continuation so resume does
  not repeat completed lock, build, install, targeting, or evidence work.
- The checkpoint is reconciled before reuse and records bounded acceptance
  stages, exact resource ownership, sanitized evidence, restoration state, and
  an external-action ledger. Secrets and broad authenticated state are excluded.
- A deterministic project-local evidence audit must pass before Smoke reports
  success or a blocker. Unsafe raw files are removed or redacted while the
  non-sensitive summary and lock-release evidence remain.
- Mobile target confirmation is transport-aware. The default global mcporter
  adapter is stateless: the locked serial must be present in device discovery,
  and every target-specific call carries the same explicit platform and device
  ID. Process-local selection and target-query state must not be compared
  across separate ephemeral calls. A persistent transport may additionally use
  documented selection acknowledgement and target queries, but never replaces
  explicit per-call device addressing. Do not require undocumented display
  labels such as `ACTIVE`.
- If the selected MCP schema does not expose an explicit device ID for a
  target-specific action, that action must use the exact project platform
  adapter instead. For Android this means `adb -s <locked-serial>`; an implicit
  Mobile MCP `system` target cannot satisfy the contract.
- Device-side timestamp and log-boundary syntax is platform-adapter behavior,
  not a portable workflow contract. Validate the exact command before treating
  it as an evidence gate; command or parsing failure is a Smoke blocker, never
  an empty passing signal report.
- Platform-specific classification and execution rules remain project adapters.
- `Engineering Smoke Lab` preserves the project Smoke policy while disabling
  PR and CI stages, so both Gate branches can be tested cheaply.
- Smoke Lab rollover uses free-form experimental labels, not semantic versions.

## Role resolution

- Plan uses `orchestrator`; Gate uses optional `gate` and otherwise
  `orchestrator`.
- Implement and Fix use `implementation`.
- Smoke uses `qa`.
- PR preparation and Cleanup use `release`.
- CI monitoring and Waiting PR use `ci`.
- A node assigned to a profile role owns its transition directly and does not
  start a duplicate child session for that same role.
- Role prompts define behavior; Kent configuration owns model, reasoning,
  verbosity, tools, and delegation eligibility. See `role-contract.md`.
- Project profiles must map every enabled operational node to a role available
  from the effective project or global Kent configuration.
- The kit provides contract-complete global roles. Workspace config may
  specialize the same role names, but workflow correctness must not depend on
  that override because Kent 2.4 canaries observed scheduler-created direct
  roles selecting the global definition.
- Independent standards and specification reviews use global read-only roles.
- Final PR-producing delivery uses a distinct global read-only compliance role
  after Gate and any required Smoke. It attests the final evidence and
  authority chain rather than repeating the earlier standards/spec reviews.
- Standards, Specification, and Compliance nodes are leaf sessions and must
  perform their bounded pass directly without starting child agents.
- Implement and Fix must not launch nested final reviewers that duplicate the
  generated graph's Standards, Specification, or Compliance stages.
  Standalone review commands may use project-specialized reviewers when no
  Delivery graph owns the same pass.

## Workflow retirement

- Preview deletion before confirmation and classify every attached task.
- Running, approval-waiting, or otherwise active tasks must finish or be
  explicitly canceled before retirement.
- Recreate every Backlog task in the replacement workflow before deleting the
  source workflow. Preserve title, body, source URL, labels, and relevant
  comments, and record the old-to-new short-ID mapping.
- Do not move tasks between incompatible workflow graphs. Recreate the Backlog
  record under the replacement graph instead.
- Completed and canceled task history may be discarded when the user accepts
  that consequence; it is not by itself a retirement blocker.

## Task-owned cleanup

- Cleanup is a report-first resource-owning agent stage. It never removes its
  own Kent-managed worktree.
- Cleanup always emits the exact non-empty `git branch --show-current` value,
  including `no_pr` and `report_only` paths. Sentinel or inferred task-ID
  branch values are invalid and route Janitor back to Cleanup instead of
  completing with leaked resources.
- For managed-worktree profiles, Cleanup emits canonical workspace, task,
  branch, PR, merge, mode, session, and preflight data to a deterministic Task
  Janitor script after the Cleanup session exits.
- The Janitor never deletes the primary checkout, dirty or ambiguous state, or
  content not proven recoverable. A merged-PR cleanup re-queries GitHub and
  requires the clean local task HEAD to equal or be an ancestor of the exact
  branch head recorded by that merged PR. This permits safe cleanup after
  user-authored remote commits without accepting diverged local work.
- The same-repository remote task branch may be deleted only when its current
  OID still equals the merged PR head. Local worktree and branch deletion uses
  Kent's supported worktree command; a squash/rebase branch retained by Kent
  may be deleted only under the same exact merged-PR proof.
- No-PR/report-only cleanup requires the clean HEAD to remain reachable from a
  remote ref. Closed-without-merge work is preserved.
- Safety preservation is a successful cleanup result and must be explicit in
  `cleanup_report`. Infrastructure failure returns to Cleanup with the resource
  untouched.

## Project adapter boundary

Each project provides:

- `.kent/project-contract.md`;
- `.kent/workflow-profile.toml`;
- `.kent/scripts/workflow-verify`;
- an optional idempotent `.kent/worktrees/setup.sh` conforming to
  `worktree-contract.md`;
- canonical project-local role keys;
- optional Smoke-decision, Smoke-execution, resource-lock, evidence-audit, PR,
  CI, and release adapters.

The kit provides the global mcporter call/list adapter and a synchronized
project-local read-only Jira source adapter. Projects provide Jira base URLs,
credential namespaces or 1Password pointers, server definitions, and optional
project-specific stdio wrappers or policy. The kit never stores resolved Jira
credentials.

Profiles list indispensable executable adapter keys in `required_adapters`.
The platform-neutral profile loader validates only that declared contract and
does not infer policy from platform names. Android projects with conditional or
required runtime Smoke list `mobile_resource_lock` and
`mobile_evidence_audit`. The shared adapters own machine-wide lock mechanics
and deterministic evidence hygiene while the project still owns emulator
startup policy, physical-device permission, build/install targets,
credentials, and runtime acceptance evidence.

Projects that treat Jira descriptions or comments as planning sources list
`jira_api` in `required_adapters`, declare its project-local path, and select
their own `[integrations.jira]` credential namespace. Related repositories may
share a namespace intentionally; unrelated repositories remain isolated.

For Jira-backed planning, normalized issue relations are the primary
cross-platform discovery path. Plan follows related issues at most one graph
level, identifies sibling platforms from issue metadata rather than link-type
wording alone, and then resolves the sibling issue to a project-declared local
reference repository. An existing sibling implementation and its tests are
mandatory bounded product evidence. Plan records the relationship, immutable
source commit and paths, and `checked`, `adopted`, and `rejected` conclusions.
Explicit target-platform requirements, current design, and API contracts remain
authoritative. Bounded API/model/flow fingerprint search is the fallback when
Jira relations do not identify a usable sibling implementation.
