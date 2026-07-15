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
