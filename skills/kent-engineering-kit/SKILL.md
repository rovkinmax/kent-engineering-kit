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

- The shared workflow layer owns lifecycle, approvals, fan-out, Join, portable
  transition parameters, deterministic waiting, and task-owned cleanup.
- The project owns build commands, architecture rules, device details, source
  integrations, release policy, credentials, and concrete procedures.
- `required_adapters` declares runtime dependencies;
  `kit_managed_adapters` is the explicit subset synchronized from kit
  templates. Other adapters are project-owned and never replaced.
- Use canonical profile role keys. Project overrides may specialize a role but
  must preserve `contracts/role-contract.md`.
- Reject `model:` and `tools:` frontmatter in role prompts. Configure execution
  policy through Kent.
- Run deterministic verification through the profile command, normally
  `.kent/scripts/workflow-verify`.

## Workflow Authoring And Rollout

- Honor the profile's `minimum_kent_version`, execution-target policy,
  capabilities, role mapping, work kinds, commands, adapters, and procedures.
- Before starting a generated workflow from a branch, tag, or commit, run
  `~/.kent/bin/kent-preflight-revision` with `--project` and `--ref`. It checks
  the selected revision's project contract, procedures, executable commands,
  and required adapters directly from Git objects without checking them out.
- Preview generated workflows without `--apply`.
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
- During lab iteration, reconcile a generated workflow in place only while it
  has no task records in any linked project and the Kent CLI can express the
  change without deleting nodes/edges, changing edge sources, or removing an
  approval.
- Once tasks reference a workflow, treat its graph as frozen and create another
  free-form experimental label for semantic changes.
- Preview retirement with `kent workflow delete <bare-workflow-uuid> --json`.
  The preview is
  non-destructive. Only the user may repeat it with `--confirm`; deletion
  removes the workflow definition, project links, and task database rows but
  intentionally retains repository files and managed worktrees.
- Generated managed-worktree Cleanup hands deletion to the post-session Task
  Janitor. Never replace it with direct `git worktree remove`; inspect its
  report when safety preservation leaves a resource in place.
- Before confirmation, recreate every Backlog task in the replacement workflow
  with its title, body, source URL, labels, relevant comments, and old short ID.
  Do not move records between incompatible graphs. Completed/canceled history
  may be discarded when the user accepts it.
- Inspect and clean retained worktrees separately and never edit the Kent
  database directly.

## Runtime Operation

- Treat Kent's execution target, root, and resolved commit as authoritative.
- Use `kent worktree` for managed worktrees. For another session use
  `~/.kent/bin/kent-worktree ... --session <id>`.
- Diagnose `interrupted` state before moving a task. Resume is asynchronous;
  re-read task state after a short delay.
- Read the active context manifest and append one evidence event before every
  agent transition. `files_read` excludes the manifest recorded separately.
- Fix and Smoke reconcile their ignored checkpoint before repeating work and
  persist it before transition.
- Branch identity is resolved by the configured deterministic Script after
  Plan. The task short ID remains lifecycle identity; current Git branch comes
  from Git.
- Pending CI and an unchanged open PR belong to deterministic Script watchers.
  Agent roles wake only for a classification, retry, or decision.
- Approval is for a real decision or completed external action, never passive
  waiting.
- Operational roles are assigned directly. Standards, Specification, and
  Compliance remain leaf reviews.

## Specialized Contracts

- `contracts/workflow-contract.md`: graph, transitions, lifecycle, delivery,
  retry, publication, retirement, and cleanup.
- `contracts/role-contract.md`: role ownership and Kent config boundary.
- `contracts/worktree-contract.md`: managed worktrees, setup, checkpoints, and
  evidence.
- `contracts/mobile-smoke-contract.md`: runtime authorization, interaction
  proof, evidence, and resource recovery.
- Jira projects declare kit-managed `jira_api` plus a project-owned credential
  namespace. Common writes are exact-target and approval-gated; project-owned
  extensions may add release/version operations.
- Sentry-backed projects declare kit-managed `sentry_issues`; tracked profiles
  contain tenant coordinates and a credential namespace, never token or
  1Password item identity.
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
