---
name: kent-engineering-kit
description: Use the shared Kent project profile, adapter, verification, and workflow contracts.
---

Read the active node context manifest, `.kent/workflow-profile.toml` and
`.kent/project-contract.md` first. Load only their required and applicable
conditional sources. `AGENTS.md` owns authority, governance, safety and
language; roles own behavior, configuration owns execution settings.

Projects supply build, architecture, integrations, devices, credentials and
release procedures. The shared workflow owns lifecycle, approval/review
ordering, fan-out/Join, cleanup handoff and portable parameters. Maintainer
contracts are generator sources, not a mandatory node reading catalog.

For R2 documents, missing accepted-plan caches or original decision recovery,
read [Durable context and recovery](references/durable-context.md). Resolve
task-fixed storage before choosing paths. Summaries/locators aid discovery;
they supply neither original consent nor fresh effect evidence.

Use supported Kent interfaces and project procedures for Task/Session
operations. Requeueing is not startup, a pending Question is not completion,
and unknown effects require settlement before retry.

Workflow updates follow
[compatibility policy](../../contracts/workflow-update-compatibility.md).
Source changes do not update installed/live state or locked Sessions. Installation,
activation, publication, restart and default/link changes require their exact
effect authority and capable owner; a source-only approval grants none.

Run the profile's verification command. Kit source verification is
`./scripts/validate`; `--installed-state` is a separate authorized inspection.
