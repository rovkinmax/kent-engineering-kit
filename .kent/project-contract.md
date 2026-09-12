# Kit Self-Development Contract

## Authority and source

`AGENTS.md` owns governance and safety. The task body, source and exact
human-authored comment IDs define scope. A plan may supersede them only by
citing explicit authority; an agent summary never supplies that authority.
Keep one authoritative preview/plan and reference it from review evidence.
Preview the file set, graph delta, rollout, rollback and restart impact.
Two independent read-only reviews of the same preview hash must PASS before
human approval. `.kent/commands/plan.md` owns this ordering and revalidation.

Kent owns lifecycle, execution root and resolved commit. Preserve `fixed_point`
as the immutable task-delta baseline, separate from the moving PR target.
Target-only commits are integration inputs, not task regressions without
merge/replay evidence. Material scope expansion returns to reviewed planning;
task-scoped fixes do not restart governance. Missing agent bookkeeping is not
a user decision: reconstruct it only when bounded, otherwise disclose the
gap and use available evidence. Ask only for real decisions or external acts.

## Project interfaces

`.kent/workflow-profile.toml` maps work kinds, procedures and commands.
`.kent/workflows/kit_development.py` composes the existing lite graph; its
semantic candidate is Kit Engineering Delivery v2, with spec
`.kent/workflows/kit-engineering-delivery-v2.spec.json`. Preserve the v1
snapshot and task-backed Workflow unchanged; future live rollout requires
a separately approved new Workflow UUID and fresh qualification.
The flow has 21 nodes, 51 transition groups and 52 edges, with continuous
writer sessions and task branch identity. Plan's callable read-only leaf and
the separate Plan Review node provide the two preview reviews. Only
`plan_review_accept` adds the human approval specialization.

Deterministic verification and independent Standards review run read-only
through dispatch/Join/Gate. There is no CI preparation/watch/monitor stage,
Smoke, publication or final Compliance layer. Existing repository checks
remain unchanged. Use only existing configured roles and profile commands;
do not add a hook, role, shared engine or alternate lifecycle.

Generated checkpoint, plan-contract and dispatch commands use the existing
schema-3 synchronizer. The builder explicitly materializes byte-identical
evidence-ledger, verify-report, wait-github-pr, task-janitor and sibling
`workflow_runtime_contracts.py` from checkout-local authoritative sources.
These five are not schema-3 synchronizer outputs or runtime-v2 adoption.
`prepare_cleanup` is the project-owned `.kent/scripts/workflow-prepare-cleanup`
command, not a generated template or new node. Cleanup uses it to retain the
known accepted-plan snapshot and prepare the existing terminal evidence seal.
Its effect boundary and input contract are in `.kent/commands/cleanup-task.md`.
Explicit bootstrap materializes generated commands and the sibling module
as regular mode-0755 files. Git preserves executable identity (mode 100755),
not exact group/other permission bits: a private checkout may validly use
0700. Checked-out commands must remain regular, owner-executable and free
of group/other write or special permission bits, with byte parity and no
symlinks. Do not chmod a fresh task checkout merely to reproduce 0755.
Do not edit generated copies; unknown, symlink or modified targets must block
materialization until their update has bounded source authority.

## Verification and evidence

Run checkout-local `./scripts/validate` with an existing Python 3.11+.
Never use `--installed-state`, install dependencies/interpreters, or change
global PATH for this flow. The profile's compile verifier returns only
`{"transition":"passed"}`, `{"transition":"failed"}` or
`{"transition":"blocked"}` on stdout, with logs on stderr.
Runtime checkpoints/evidence belong under ignored `.kent/runtime/`; verifier
outputs and private temporary directories use ignored `build/kent-workflow/`.
Normal `.todo` plans remain tracked.

Read the active context manifest first. Before each Agent transition append
the required non-empty evidence event through the profile command, recording
checks, artifacts, decisions and exact instruction files read. Evidence is
append-only; do not rewrite prior slices or invent unavailable telemetry.
Generated transition prompts own parameter carriers and completion keys.
For terminal Cleanup, the preparation helper owns the final ordinary event
and invokes the existing append/seal commands. Do not duplicate that append
or append after seal, including after leaving the source worktree.
The configured `prepare_cleanup` opts Cleanup and its recovery into this
single-owner contract. Do not append standalone evidence before the helper
or merely to report a blocker; retain those observations in Task records.
Use real current unmodified Kent Session/Run/Step identities; never generate,
export or replace them to defeat deduplication. Preserve and block on
conflicting/fabricated evidence, without reconstructing or resealing history.
This behavioral prohibition does not provide native identity authentication:
qualification must independently verify the final event's actual Kent
Run/Session/Step records before admitting it as evidence.

## Source versus installed state

Development uses one standard Kent-managed task worktree. Preserve the
installed primary at `9363fa48f9f21d2742a41841ab97b18cc4c4e521`, its index,
local main, symlinks, global configuration and every consumer pin/workflow.
README's explicit fetch/preflight precedes Task start: `default-branch`
resolves local `origin/HEAD` and does not fetch. After merged delivery refresh
tracking only; never checkout/fast-forward installed main.

No live device/login actions, consumer-app implementation or rollout,
installed-state adoption or restart is automatically authorized by this
flow. Source-only investigation and deterministic tests of Kit adapters and
prompts remain valid within explicit task scope, including device-resource
and login-guidance tooling. Live workflow changes and Git delivery require
their own explicit effect authority; source approval alone grants neither.
Never merge a PR or push directly to main.

## Report-only qualification

Only explicit task authority may select report-only/no-repair qualification.
It may use `plan_path=not-applicable`; do not manufacture a tracked plan.
Keep the preview, exact hash, both reviews and approval in retained task
records or ignored `build/kent-workflow/<task>/`, not arbitrary restricted
runtime files. Implement and Fix perform no tracked or staged writes; report
candidate defects for separately authorized repair.

Use the existing `report_only` disposition and `no_pr` cleanup mode only
under that authority and when exact HEAD equals a current published branch
tip. Reachability from a remote ancestor is insufficient. Keep the bootstrap
source branch published through qualification. Cleanup uses the existing
post-session Janitor; `.kent/commands/cleanup-task.md` owns the procedure.
