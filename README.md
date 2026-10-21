# Kent Engineering Kit

**Give AI-assisted engineering a repeatable process—from the first question to a reviewed pull request.**

A personal toolkit for Kent: reusable prompts, specialist agents, engineering
skills, and project workflows. Use it to think through a design, investigate a
bug, challenge a plan, or carry a change through implementation and verification.

The Kit adds engineering practices to Kent; it is not a replacement for Kent.
It works across platforms because your project supplies its own build commands,
tests, architecture rules, and release procedures.

[Get started](#get-started) · [Operations guide](docs/OPERATIONS.md) · [Project setup](prompts/project-bootstrap.md)

## What it helps you do

| When you need to… | The Kit provides… |
| --- | --- |
| Understand an unfamiliar codebase | Guided exploration of its structure, terminology, and constraints |
| Choose an approach | Design alternatives, architectural analysis, and grill critique |
| Make a change | Planning, focused implementation, and test-driven slices |
| Check the result | Independent reviews, project-owned verification, and optional runtime checks |
| Deliver and follow up | Configurable PR/CI workflows, task supervision, and managed-worktree cleanup |

You can use individual tools without adopting a full project workflow.

## Two ways to use it

### Bring a specialist to one problem

After [installing and configuring the roles](docs/OPERATIONS.md#install-the-kit),
ask grill to challenge a decision:

```sh
kent run --agent grill \
  'Review this repository and challenge my plan to add a second cache. Identify tradeoffs and unresolved decisions.'
```

The result is a critique to help you decide, not a code change.
For ongoing work, open a task-supervisor session and tell it which Tasks and
projects to accompany:

```sh
kent --agent task-supervisor
```

Prefer a guided conversation? Start with prompts for
[mapping an uncertain project](prompts/wayfind.md),
[comparing designs](prompts/design-alternatives.md),
[diagnosing a problem](prompts/diagnose.md),
[reviewing a change](prompts/review-change.md), or
[migrating Tasks](prompts/migrate-tasks.md).

### Give a project a delivery workflow

A project profile connects the Kit to your repository's tools and procedures.
A typical delivery path looks like this:

```text
Understand → Plan → Implement → Verify → Pull request → Cleanup
```

The selected profile determines the actual stages, approvals, reviews, and
optional runtime or release steps. You keep control of consequential decisions;
the project defines how success is checked.

Reviews check the obligations due at the agreed delivery stage. A review-ready
PR can still have explicitly deferred external checks; it is not proof of full
product acceptance or permission to merge or release.

The Kit coordinates the work. Your repository remains responsible for its
architecture, credentials, integrations, build environment, and release policy.
Installing the Kit does not activate a workflow for a project.

## Get started

1. **Install the shared tools.** Follow the
   [installation guide](docs/OPERATIONS.md#install-the-kit), including role
   configuration. Installation creates links under your Kent directory to this
   checkout, so keep it in a stable location.
2. **Try one focused task.** Use a specialist or one of the prompts above on a
   real design question or bug.
3. **Connect a project when you need repeatable delivery.** Use
   [project bootstrap](prompts/project-bootstrap.md) to inspect the repository
   and propose a suitable setup. Review the proposal before creating or
   activating workflows; the [project setup guide](docs/OPERATIONS.md#connect-a-project)
   explains that boundary.

Compatibility requirements belong to the selected project's profile and the
[workflow contract](contracts/workflow-contract.md). Mobile and MCP integrations
are optional, not prerequisites for every project.

### Automatic session names

The managed `config/subagents.toml` fragment configures Kent's nano model for
session naming without changing the coding model. Before merging that fragment
into `~/.kent/config.toml`, supply `nano.connection` using an existing connection
from your own global configuration. The shared fragment intentionally omits
this personal setting. The resulting global configuration should contain:

```toml
[nano]
connection = "your-existing-connection"
model = "gpt-6-luna"
thinking_level = "low"
model_verbosity = "low"

[nano.features]
session_naming = true
```

Roll out only after approving the configuration change: keep a private backup,
merge these settings without replacing unrelated configuration, and restart Kent
at a safe moment. Do not install a service just to restart a manually launched
server. The installer does not rewrite your configuration.

After restarting, verify that a new unnamed session gains a title after its first
answer and associated tool work finish, and that a manually named session keeps
its name. Naming makes additional model requests. Existing names are preserved;
this is not a bulk rename of old sessions. See Kent's
[nano reference](https://kent.sh/auxiliary-models/) for behavior and overrides.

To roll back, restore only the prior `nano` settings from your backup, preserving
unrelated changes, and restart again. If `nano` was previously absent, remove the
added tables. Reverting the source PR alone does not change the installed config.

## Find your way around

| Resource | What belongs there |
| --- | --- |
| [Prompts](prompts/) | Guided tasks you explicitly choose |
| [Skills](skills/) | Reusable disciplines agents load when relevant |
| [Agents](agents/) | Specialist roles and their responsibilities |
| [Adapters](adapters/README.md) | Opt-in platform and integration support |
| [Operations](docs/OPERATIONS.md) | Installation, project setup, and maintenance |
| [Contracts](contracts/) | Precise workflow, role, and safety rules |
| [Workflow engine](workflowkit/) | Profile parsing, graph generation, evidence transport, and runtime checks |
| [Project templates](templates/project/) | Executable helpers adopted through project setup |
| [Scripts](scripts/) | Installation, generation, synchronization, and validation entry points |
| [Role configuration](config/subagents.toml) | Models, tools, and delegation settings, separate from role behavior |
| [Kit project](.kent/) | This repository's own profile, procedures, and current generated workflow |
| [Tests](tests/) | Contract, integration, and failure-path checks |

## Developing the Kit itself

Start with the [development and source-selection preflight guide](docs/OPERATIONS.md#developing-the-kit).
It covers verification and the separation between source changes and the
installed Kit. Changes follow [global governance](global/AGENTS.md#authority-and-decisions);
a source PR does not update installed tools or live workflows.

The Kit's own workflow has one [current source snapshot](.kent/workflows/kit-engineering-delivery.current.spec.json),
built by its [project builder](.kent/workflows/kit_development.py). Historical
source revisions belong in Git, not parallel workflow snapshots. Generated
project helpers remain checked in so a selected revision has its executable
dependencies; update them through the qualified generation tools.

For source-only validation, run `./scripts/validate`. The development workflow
uses the configured verifier to retain the result, log, and source identity.
Neither check installs the Kit or changes running Tasks.
