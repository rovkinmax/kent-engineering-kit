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
- Run deterministic project verification through the executable declared by the
  profile, normally `.kent/scripts/workflow-verify`.
- Use canonical role keys from the profile. Role implementations remain
  project-local unless the role is explicitly global.

## Execution

- Require Kent 2.3 or newer before creating generated workflows.
- Apply the profile's explicit execution-target policy to each workflow.
- Use a workflow-kind override when present.
- Treat Kent's selected execution root and resolved commit as authoritative.
- Use `kent worktree` for operations on Kent-managed worktrees.
- Follow `contracts/worktree-contract.md` when a project needs setup hooks or
  untracked machine configuration.
- Preview generated workflows without `--apply`.
- Apply versioned workflows non-default first. Use `--set-default` only after a
  managed-worktree canary passes.
- Use `Engineering Canary` for infrastructure-only validation. It intentionally
  omits device smoke and PR/CI stages.
- Never reconcile graph changes into a workflow that already has tasks. Create
  the next version.

## Fan-out

- Parallel branches are read-only.
- Every branch transitions directly to its Join.
- Failures are returned to Join as structured results.
- Each branch emits one stable output contract.
- Only the post-Join gate selects Fix, QA, Ship, or Needs User Action.

## Safety

- Do not edit Kent workflow database records directly.
- Validate generated workflows in execution mode.
- Treat project JSON exports as audit snapshots.
- Use versioned workflow instances when changing a graph linked to more than one
  project.
- Keep project-local operational roles behind the `default` orchestrator.
  Assign only globally registered roles directly to custom workflow nodes.
