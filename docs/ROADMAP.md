# Roadmap

## Phase 1: Global toolkit

- Install one toolkit-specific model-invoked skill and explicit engineering prompts.
- Register reusable read-only subagent roles.
- Keep the Kent default system prompt intact.
- Restart Kent once after configuration changes.

## Phase 2: Workflow generator

- Define a project profile schema.
- Generate project-local workflow instances from shared fragments.
- Validate live workflows and export audit snapshots.
- Encode Kent fan-out constraints in generation and validation.

## Phase 3: Delivery workflows

- Generate `Engineering Delivery v1` for Appsome and Puber.
- Run project canaries before changing defaults.
- Migrate feature/refactor/bugfix flows.

## Phase 4: Auxiliary workflows

- Generate maintenance, smoke, intake, release, and rebase flows.
- Support single and split release topologies.
- Add Web, iOS, embedded, and generic project profiles.
