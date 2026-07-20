# Roadmap

## Phase 1: Global toolkit

Status: complete.

- Install one toolkit-specific model-invoked skill and explicit engineering prompts.
- Register reusable read-only subagent roles.
- Keep the Kent default system prompt intact.
- Upgrade Kent CLI/TUI, service, and Desktop as one 2.3 compatibility set.
- Restart Kent once after configuration changes and the coordinated upgrade.

## Phase 2: Workflow generator

Status: complete for the `Engineering Delivery v4` and
`Engineering Canary v1` contracts.

- Define a project profile schema.
- Generate project-local workflow instances from shared fragments.
- Apply an explicit execution-target policy to every generated workflow.
- Validate live workflows and export audit snapshots.
- Encode Kent fan-out constraints in generation and validation.
- Validate worktree setup hooks against the Kent 2.3 payload contract.
- Reject unsupported graph drift before mutation and refuse graph mutation when
  task records already exist.

## Phase 3: Delivery workflows

Status: current Canary, Smoke Lab, and full Delivery canaries complete.
Engineering Delivery v5 is the project default in Appsome and Puber; legacy
Feature Delivery remains linked for rollback.

- Generate the current `Engineering Delivery` and infrastructure-only
  `Engineering Canary` lab instances for Appsome and Puber.
- Re-export existing workflow snapshots and verify their preserved Source HEAD
  policies after upgrading Kent.
- Validate two-step implementation continuation, verification fan-out/Join,
  and report-only cleanup in both Android projects.
- Validate interrupted-node recovery without losing the locked target or
  completed Join context in Appsome.
- Preflight each selected execution revision against its profile-owned scripts,
  procedures, and adapters.
- Observe ordinary default tasks across delivery-ready, Smoke, Fix, CI, and PR
  feedback paths.
- Move the Appsome project adapter from `release/4.29.0` into `master`; until
  then, start generated Appsome workflows only from audited adapter commit
  `b6fd03e1f15dc49bbe9431955062699f8bf6bfb0` or its descendants.
- Design work-kind dispatch before migrating refactor and bugfix flows.

## Phase 4: Auxiliary workflows

Status: planned.

- Generate maintenance, smoke, intake, release, and rebase flows.
- Support single and split release topologies.
- Add Web, iOS, embedded, and generic project profiles.
