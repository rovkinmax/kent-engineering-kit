# Roadmap

## Phase 1: Global toolkit

Status: complete.

- Install one toolkit-specific model-invoked skill and explicit engineering prompts.
- Register reusable read-only subagent roles.
- Keep the Kent default system prompt intact.
- Upgrade Kent CLI/TUI, service, and Desktop as one 2.3 compatibility set.
- Restart Kent once after configuration changes and the coordinated upgrade.

## Phase 2: Workflow generator

Status: complete for the `Engineering Delivery v2` contract.

- Define a project profile schema.
- Generate project-local workflow instances from shared fragments.
- Apply an explicit execution-target policy to every generated workflow.
- Validate live workflows and export audit snapshots.
- Encode Kent fan-out constraints in generation and validation.
- Validate worktree setup hooks against the Kent 2.3 payload contract.

## Phase 3: Delivery workflows

Status: generated and linked non-default; managed-worktree canaries pending.

- Generate `Engineering Delivery v2` for Appsome and Puber.
- Re-export existing workflow snapshots and verify their preserved Source HEAD
  policies after upgrading Kent.
- Run project canaries before changing defaults.
- Migrate feature/refactor/bugfix flows.

## Phase 4: Auxiliary workflows

Status: planned.

- Generate maintenance, smoke, intake, release, and rebase flows.
- Support single and split release topologies.
- Add Web, iOS, embedded, and generic project profiles.
