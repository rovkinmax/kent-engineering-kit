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
- For Android runtime Smoke, require the declared project-local
  `mobile_resource_lock` adapter before any install, launch, input, or log
  action. Use an explicit serial for every direct adb and target-specific
  Mobile MCP call.
- Confirm Mobile MCP targeting through documented serial presence, explicit
  selection acknowledgement, and a target query when available. Do not depend
  on an undocumented `ACTIVE` label. Treat device-side timestamp syntax as
  adapter-specific and validate it before using it as a runtime evidence
  boundary.
- Require the project-local `mobile_evidence_audit` before completing runtime
  Smoke. Persist only scoped, sanitized evidence; unexpected authenticated or
  sensitive state becomes a redacted blocker.
- Preview generated workflows without `--apply`.
- Apply versioned workflows non-default first. Use `--set-default` only after a
  managed-worktree canary passes.
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
