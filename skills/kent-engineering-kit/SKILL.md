---
name: kent-engineering-kit
description: Use the shared Kent project profile, adapter, verification, and workflow contracts. Use when a repository contains `.kent/workflow-profile.toml` or references the Kent Engineering Kit.
---

Read `.kent/workflow-profile.toml` and `.kent/project-contract.md` before using
generated toolkit workflows.

## Boundaries

- The shared workflow layer owns lifecycle, approvals, fan-out, Join, and
  portable transition parameters.
- The project owns build commands, architecture rules, device details, source
  integrations, release policy, and credentials.
- Role prompts own behavior, while Kent configuration owns model, reasoning,
  verbosity, tools, and delegation eligibility. Reject `model:` and `tools:`
  frontmatter in project role prompts.
- Run deterministic project verification through the executable declared by the
  profile, normally `.kent/scripts/workflow-verify`.
- Use `~/.kent/bin/kent-mcp-call` and `~/.kent/bin/kent-mcp-list` for MCP
  access. Projects own credentials and project-specific server wrappers.
- MCP call logs retain metadata, while ordinary stdout remains in Kent's shell
  transcript. Use `--quiet`, `--digest-output`, or output assertions for
  sensitive calls. Use `--save-raw` or `--raw-dir` only for known-safe
  evidence; never emit or retain an unexpected authenticated UI tree.
- Use canonical role keys from the profile. Role implementations remain
  project-local unless the role is explicitly global.

## Execution

- Require Kent 2.3 or newer before creating generated workflows.
- Apply the profile's explicit execution-target policy to each workflow.
- Use a workflow-kind override when present.
- Before starting a generated workflow from a branch, tag, or commit, run
  `~/.kent/bin/kent-preflight-revision` with `--project` and `--ref`. It checks
  the selected revision's project contract, procedures, executable commands,
  and required adapters directly from Git objects without checking them out.
- Treat canary revisions as rollout evidence, not a commit-ancestry gate, until
  the shared profile and workflow compatibility contract is versioned.
- Treat Kent's selected execution root and resolved commit as authoritative.
- Use `kent worktree` for operations on Kent-managed worktrees.
- For a cross-session operation, invoke `~/.kent/bin/kent-worktree` with an
  explicit `--session`. The wrapper prevents inherited task/session variables
  from overriding the requested workspace.
- Diagnose `interrupted` task runs before changing workflow state. A transient
  provider or transport error should be recovered with the user-only
  `kent task resume` command, preserving the locked target and completed
  upstream transitions. Do not replace it with a manual move or a new task.
- Follow `contracts/worktree-contract.md` when a project needs setup hooks or
  untracked machine configuration.
- For Android runtime Smoke, require the declared project-local
  `mobile_resource_lock` adapter before any install, launch, input, or log
  action. Use an explicit serial for every direct adb and target-specific
  Mobile MCP call.
- With the default global `mobile` server, use stateless targeting: list the
  exact locked serial and pass `platform` plus `deviceId` to every
  target-specific call. Do not use process-local `set` / `get_target` as a
  cross-call gate. If the current MCP schema lacks `deviceId` for a required
  action, use the exact project platform adapter instead of an implicit target.
  Treat device-side timestamp syntax as adapter-specific and validate it before
  using it as a runtime evidence boundary.
- Every Mobile call other than device discovery uses a safe output mode.
  Prefer `--quiet` for actions, assertions for known UI facts, and
  `--digest-output` for before/after equivalence. Use `--hash-matches` plus
  `--marker-present` for opaque semantic-key inventories and final-page proof.
  Do not request a full authenticated UI tree through raw stdout.
- Require the project-local `mobile_evidence_audit` before completing runtime
  Smoke. Persist only scoped, sanitized evidence; unexpected authenticated or
  sensitive state becomes a redacted blocker.
- Preview generated workflows without `--apply`.
- Apply versioned workflows non-default first. Use `--set-default` only after a
  managed-worktree canary passes.
- Read `policies.writer_sessions` before interpreting writer continuity.
  `continuous` reuses or compacts sessions. `fresh_per_slice` starts one fresh
  Implement/Fix session per independently verifiable slice; the worktree,
  authoritative artifacts, exact task-comment IDs, evidence, and structured
  transition parameters are the handoff. Use `continue_fix` only with the
  remaining findings. Non-writer approval-recovery loops compact and continue
  their existing sessions.
- Read `policies.pr_merge_strategy` before PR preparation. `auto` must resolve
  to exactly one method from repository capabilities, target-branch rules, and
  merge-queue policy; otherwise return `needs_user_action`. Carry the resolved
  `merge_strategy` through CI and Waiting PR. For GitHub rebase delivery,
  require `canBeRebased=true`; `MERGEABLE/CLEAN` alone is insufficient.
- Feed GitHub repository capabilities, target-branch protection, applicable
  rulesets, and merge-queue evidence to
  `~/.kent/bin/kent-resolve-github-merge-strategy`. Honor its structured
  `resolved` or `needs_user_action` result; never choose manually.
- Diagnose disputed rebase feasibility only in an isolated temporary clone or
  branch. Rewriting task history requires exact user authorization, a backup,
  final-tree proof, and force-with-lease pinned to the expected remote head.
- A recovery task may start from an explicit checkpoint commit and source task.
  Its Plan session verifies the checkpoint and updates authoritative artifacts
  before any production edit; it never resets preserved implementation.
- Treat `workspace_path` as the canonical task execution root. Verification
  dispatch rejects `.todo` directories, nested paths, and foreign repositories
  before fan-out; a metadata-only Fix re-emits the canonical root.
- Changing a project default affects only new tasks. Keep the previous workflow
  linked for rollback; never move existing tasks between incompatible graphs.
- Use `Engineering Canary` for infrastructure-only validation. It intentionally
  disables Smoke and omits PR/CI stages.
- Use `Engineering Smoke Lab` to test `smoke_required` and `delivery_ready`
  without committing, pushing, or creating a pull request. Use a free-form lab
  label only after the current graph has tasks or needs unsupported structural
  rewiring.
- During lab iteration, reconcile a generated workflow in place only while it
  has no task records in any linked project and the Kent CLI can express the
  change without deleting nodes/edges, changing edge sources, or removing an
  approval.
- Once tasks reference a workflow, treat its graph as frozen and create another
  free-form experimental label for semantic changes.
- On Kent 2.4 or newer, preview retirement with
  `kent workflow delete <bare-workflow-uuid> --json`. The preview is
  non-destructive. Only the user may repeat it with `--confirm`; deletion
  removes the workflow definition, project links, and task database rows but
  intentionally retains repository files and managed worktrees.
- Before confirmation, recreate every Backlog task in the replacement workflow
  with its title, body, source URL, labels, relevant comments, and old short ID.
  Do not move records between incompatible graphs. Completed/canceled history
  may be discarded when the user accepts it.
- On Kent 2.3, retire obsolete workflows through Kent Desktop. In every
  version, inspect and clean retained worktrees separately and never edit the
  Kent database directly.

## Fan-out

- Parallel branches are read-only.
- Every branch transitions directly to its Join.
- Failures are returned to Join as structured results.
- Each branch emits one stable output contract.
- Only the post-Join gate selects Fix, QA, Ship, or Needs User Action.
- Generated Standards, Specification, and Compliance stages own final review.
  Implement and Fix must not duplicate them through nested final reviewers.
- Standards, Specification, and Compliance are leaf sessions. They must not
  call `kent run` or delegate their review to child agents.
- Standards review pins the comparison baseline. A repository-wide analyzer
  failure is task-scoped only when a new or worsened violation is proven;
  changed paths alone are insufficient. Pre-existing debt stays non-blocking,
  while an absolute-clean policy contradicted by the baseline routes to
  `needs_user_action` instead of broad Fix work.

## Safety

- Do not edit Kent workflow database records directly.
- Validate generated workflows in execution mode.
- Treat project JSON exports as audit snapshots.
- Use versioned workflow instances when changing a graph linked to more than one
  project.
- Assign operational workflow nodes directly from the profile's optional
  `gate` and required `implementation`, `qa`, `release`, and `ci` roles. Keep
  `default` orchestration for Plan instead of wrapping every specialist in
  another session. Global kit roles are contract-complete. Workspace config
  may specialize the same role name, but workflow correctness must not depend
  on that override because Kent 2.4 canaries observed scheduler-created direct
  roles selecting the global definition.
