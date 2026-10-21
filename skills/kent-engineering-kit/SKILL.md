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

When a role requires a shared Kit contract, use this installed skill's source
binding: resolve the `kent-engineering-kit` directory under
`${KENT_PERSISTENCE_ROOT:-$HOME/.kent}/skills` through its installer symlink.
Its canonical parent is the Kit's `skills` directory; the next parent is the
Kit checkout. Read the named `contracts/...` file from that checkout. Do not
resolve it from the consumer cwd or assume a persistence-root `contracts`
copy. If this binding or the required file cannot be verified, block only the
dependent action and report the missing source; do not substitute a lookalike.

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
