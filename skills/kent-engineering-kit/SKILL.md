---
name: kent-engineering-kit
description: Use the shared Kent project profile, adapter, verification, and workflow contracts.
---

Read the active node context manifest, `.kent/workflow-profile.toml`, and
`.kent/project-contract.md` before project work. `AGENTS.md` owns authority,
safety, sessions, worktrees, and language. Role prompts own behavior; Kent
configuration owns model, reasoning, verbosity, tools, and delegation.

## Boundaries

The Kit is platform-neutral. Shared workflow owns lifecycle, approvals,
fan-out/Join, waiting, cleanup, and portable parameters. Projects own build,
architecture, devices, integrations, credentials, and release procedures.
Maintainer contracts are normative generator sources. Do not edit Kent's
database directly. Existing task-backed graphs are frozen: semantic changes
use a new non-default version and a managed-worktree canary. Do not install,
activate, publish, push, restart, or mutate live Workflow/Task/default state
without a separately approved effect gate.

## Operational CLIs

`verify-release-portfolio` is read-only by default and binds Kit plus four
unique project commits and exact repository identities. It reads profile,
release source, manifest, preview, and runtime inputs from selected Git blobs.
Optional report output uses a locked atomic write/readback contract.

`retire-workflow-batch` is D9. Its canonical plan binds an absolute Kent
executable and SHA-256, persistence database/schema, project root, Session
roots, and typed resource identities. The driver constructs only fixed
readbacks and `workflow delete <uuid> --json` (or `--confirm --json`).
Sessions, worktrees, branches, and retained resources are never deleted.
Session manifests are descriptor-relative, no-follow, bounded metadata
(relative path, type, mode, bytes, SHA-256), never raw contents.

`reconcile-canonical-workflows` accepts only typed `graph-only`,
`metadata-only`, or `graph-and-metadata` intent. It constructs one complete
graph apply document and closed metadata updates; plans contain no shell,
argv, SQL, executable, command, or probe. Prepare and every phase re-read
quiescence, terminal anchors, link/default invariants, and any declared D9
journal. Exact pre/post settlement is required; rollback is a confirmed
forward restore.

`activate-primary-checkout` verifies exact primary root, `main`, clean baseline,
ancestry, tracking/config/link/prompt preconditions, and source prompt digest.
It persists `prepared -> activation_committed` before one safe fast-forward.
Recovery settles exact baseline/target without replay. Matching regular prompts
may be backed up once before adopting the byte-identical Kit symlink.

## Plan and journal rules

Plans are canonical compact JSON with duplicate-free closed objects, bounded
lists/argv, unique identities, and a raw SHA-256 supplied by the caller.
Mutation requires `--confirm` equal to that digest. Journals use one advisory
lock, deterministic temporary path, file and directory fsync, atomic replace,
and exact canonical readback. Effects use absolute executables, an exact
replacement environment, bounded output, inherited lock ownership, and
bounded guardian acknowledgement. Recovery never signals a PID: it settles
only an exact postimage or preimage and blocks ambiguous state. A settled
preimage can retry only in a later explicit invocation; same-cycle replay is
rejected.

## Verification

Use `.kent/scripts/workflow-verify` or the repository profile command.
Generated workflows declare an explicit Kent 2.6.1 execution-target policy.
Run `./scripts/validate` for source-only deterministic verification; use
`--installed-state` only when that separate effect is authorized.
