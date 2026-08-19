---
name: kent-engineering-kit
description: Use the shared Kent project profile, adapter, verification, and workflow contracts. Use when a repository contains `.kent/workflow-profile.toml` or references the Kent Engineering Kit.
---

Read the active node context manifest first, then
`.kent/workflow-profile.toml` and `.kent/project-contract.md`. This skill is an
index for the shared kit; it is not a second copy of every node contract.

## Instruction Ownership

- Global `AGENTS.md` owns universal authority, safety, session, worktree, and
  context invariants.
- Role prompts own reusable role behavior. Kent config owns model, reasoning,
  tools, verbosity, and delegation eligibility.
- A generated node prompt owns dynamic task inputs, exact transition/output
  contracts, and any mutation authority unique to that graph edge.
- Project `AGENTS.md` owns concise repository-wide gotchas.
- `.kent/project-contract.md` owns only workflow-relevant project deltas.
- `.kent/context/*.md` owns the node read allowlist and exclusions.
- Project skills and procedures own platform, architecture, build, runtime,
  source-adapter, and release instructions loaded by a matching trigger.
- Maintainer contracts in `contracts/` are normative generator sources; normal
  workflow nodes do not preload them.

Do not copy a reusable policy into all of these layers. Keep it in its owner
and make other layers carry only the task-specific constraint or a short
reference needed for safe execution.

## Shared And Project Boundaries

- Shared workflow owns lifecycle, approvals, fan-out/Join, parameters, waiting,
  cleanup; projects own build, architecture, devices, integrations, release,
  credentials, procedures.
- Schema 3 retains legacy synchronization; Schema 4 owns commands, exact
  versions, release identity.
- `required_adapters`: runtime dependencies; `kit_managed_adapters`: exact
  subset; remaining adapters are project-owned. Loader is platform-neutral.
- Synchronizer consumes `ProjectProfile`, validates the plan; see
  `contracts/workflow-contract.md` for fields and pairs. No migration/activation.
- Canonical roles preserve role contract; reject `model:`/`tools:`; Kent
  config owns execution.
- Verify via profile command, normally `.kent/scripts/workflow-verify`.

## Workflow Authoring And Rollout

- Before changing this kit, generated/live workflows, workflow
  adapters/contracts, role prompts, or Kent config, follow the global
  Investigation → Preview → two independent read-only reviews → explicit user
  approval → mutation sequence. A materially expanded scope requires a new
  preview and approval.
- Honor the profile's `minimum_kent_version`, execution-target policy,
  capabilities, role mapping, work kinds, commands, adapters, and procedures.
- Approved baseline: Kent 2.6.1 (August 13, 2026); keep CLI/TUI, service, and
  Desktop on one version.
- Before starting a generated workflow from a branch, tag, or commit, run
  `~/.kent/bin/kent-preflight-revision` with `--project` and `--ref`. It checks
  the selected revision's project contract, procedures, executable commands,
  and required adapters directly from Git objects without checking them out.
- Preview generated workflows without `--apply`. For Kent 2.6 graph edits,
  inspect the complete graph, compute the kit-owned local semantic preview,
  then use graph apply for one atomic save. Graph apply itself is not a
  non-mutating preview. Task-backed workflows are frozen: create a new version
  for semantic changes and retain the old one for rollback.
- Apply versioned workflows non-default first. Use `--set-default` only after a
  managed-worktree canary passes.
- Changing a project default affects only new tasks. Keep the previous workflow
  linked for rollback; never move existing tasks between incompatible graphs.
- Use `Engineering Canary` for infrastructure-only validation. It intentionally
  disables Smoke and omits PR/CI stages.
- Use `Engineering Smoke Lab` to test `smoke_required` and `delivery_ready`
  without committing, pushing, or creating a pull request. Use a free-form lab
  label only after the current graph has tasks or needs unsupported structural
  rewiring.
- Reconcile a lab in place only while taskless and without deleting nodes or
  edges, changing edge sources, or removing approvals.
- Once tasks reference a workflow, treat its graph as frozen and create another
  free-form experimental label for semantic changes.
- Preview retirement with `kent workflow delete <bare-workflow-uuid> --json`;
  only the user may confirm. Deletion removes workflow links and task rows but
  retains repository files and managed worktrees.
- Managed worktrees stay below `worktrees.base_dir` and outside the source
  Workspace. Setup failures preserve stderr and resumable recovery; do not
  bypass Kent with direct worktree mutation. Start/Move/Resume accept
  `--branch-name`; task/run watch/wait and `kent question` provide deterministic
  observation and question/approval control.
- Generated managed-worktree Cleanup hands deletion to the post-session Task
  Janitor. Never replace it with direct `git worktree remove`; inspect its
  report when safety preservation leaves a resource in place.
- Before retirement, recreate Backlog tasks in the replacement workflow with
  their source data and old short IDs; do not move them between incompatible
  graphs. Inspect retained worktrees separately; never edit Kent's database.

## Runtime Operation

- Treat Kent's execution target, root, and resolved commit as authoritative.
- Use `kent worktree` for managed worktrees. For another session use
  `~/.kent/bin/kent-worktree ... --session <id>`.
- Diagnose `interrupted` state before moving a task. Resume is asynchronous;
  re-read task state after a short delay.
- Read the active manifest and append evidence before each agent transition;
  `files_read` excludes the manifest. Fix and Smoke reconcile their ignored
  checkpoint before repeating work and persist it before transition.
- Branch identity is resolved by the deterministic Script after Plan. The
  task short ID remains lifecycle identity; current Git branch comes from Git.
- Pending CI and unchanged PRs belong to deterministic Script watchers; agent
  roles wake only for classification, retry, or decision.
- Approval is for a real decision or completed external action, never passive
  waiting.
- Operational roles are assigned directly. Standards, Specification, and
  Compliance remain leaf reviews.

## Specialized Contracts

- `contracts/kit-change-governance.md`: preview, independent review, approval,
  and mutation boundary for kit/workflow changes.
- `contracts/plan-contract.md`: independent Plan Review, normalized snapshots,
  and material-change revalidation.
- `contracts/workflow-contract.md`: graph, transitions, lifecycle, delivery,
  retry, publication, retirement, and cleanup.
- `contracts/role-contract.md`: role ownership and Kent config boundary.
- `contracts/worktree-contract.md`: managed worktrees, setup, checkpoints, and
  evidence.
- `contracts/mobile-smoke-contract.md`: runtime authorization, interaction
  proof, preservation-safe installation, evidence, and resource recovery.
- Jira projects declare kit-managed `jira_api` plus a project-owned credential
  namespace; extensions own release/version operations. Sentry profiles contain
  tenant coordinates and a credential namespace, never token or 1Password item.
- MCP calls use `~/.kent/bin/kent-mcp-call` and
  `~/.kent/bin/kent-mcp-list`; project credentials and server wrappers remain
  project-owned.

## Safety

- Do not edit Kent workflow database records directly.
- Validate generated workflows in execution mode.
- Treat project JSON exports as audit snapshots.
- Use versioned workflow instances when changing a graph linked to more than one
  project.
- Never commit, push, publish, merge, delete remote state, or expose credentials
  without exact task/workflow authority.
