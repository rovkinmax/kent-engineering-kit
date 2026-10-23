# Kit Self-Development Contract

## Authority and source

`AGENTS.md` owns governance and safety. The task and verified original human
decisions define scope. Use one authoritative preview/plan, two independent
same-hash PASS reviews and explicit human approval for Kit changes.
`.kent/commands/plan.md` owns that sequence; `.kent/workflow-profile.toml`
maps work kinds, roles, procedures and commands.

Kent owns lifecycle and execution identity. Preserve task `fixed_point` as
the immutable delta baseline, distinct from a moving integration target.
Target-only changes are not Task regressions without merge/replay evidence.
Task-scoped repairs stay within approved boundaries; new scope/effects return
to reviewed approval. Recover agent bookkeeping without inventing consent.

## Current workflow

`.kent/workflows/kit_development.py` builds one current source graph at
`.kent/workflows/kit-engineering-delivery.current.spec.json`. The profile
selects optional complexity routing; continuous writers, task branch identity,
delivery reliability and the complete command closure remain required.
Default rendering is read-only JSON. `--check`/`--check-current` verify current
snapshot and generated parity; `--write-spec`/`--write-current-spec` write
only that source snapshot. `--bootstrap` explicitly materializes helpers.

Plan's callable leaf and the separate Plan Review node provide independent
preview reviews. Only `plan_review_accept` adds the project approval boundary.
Implement/Fix discrepancies return to retained Plan Revalidation without
granting expanded scope. Current task-scoped PR correction needs no extra
approval; new scope/risk/effects still do.

Deterministic verification and independent Standards review run read-only
through dispatch/Join/Gate. There is no CI monitoring, Smoke, publication
or final Compliance stage. Reviews assess current-stage obligations; retain
later acceptance work with its responsible owner and milestone. Gate does
not relabel old BLOCKED evidence after authority changes. Review-ready
delivery is not final business acceptance, merge, release or Task Done.

The current schema-3 Kit profile explicitly opts into delivery reliability v1.
Its generated graph uses delivery-context-v2 post-PR packets with the exact
observed `pr_base_branch`; fresh PR observations compare GitHub `baseRefName`
before success, merge or Cleanup. This profile has CI monitoring disabled, so
it does not acquire source-CI recovery routes. Schema-4 profiles using current
runtime/prepare-CI versions select the typed source-CI v3 failure diagnostic
and its two existing CI-monitor recovery routes; older profile families keep
their declared v1 context and legacy source-CI carriers.

Generated commands come from checkout-local authoritative templates/modules.
The builder preflights the entire closed copy set before materialization.
Do not patch generated copies or overwrite modified, symlink or unknown
targets. Explicit bootstrap creates regular mode-0755 files; Git checkouts
may use private 0700. Require owner execute, byte parity and no group/other
write or special permission bits. `prepare_cleanup` and `compile_verify`
are project-owned commands, not generated copies.

Source generation does not authorize activation or change a live Workflow.
Same-UUID adoption requires the shared
[compatibility contract](../contracts/workflow-update-compatibility.md).
The published renderer accepts only qualified `VerifiedSourceAdmission`.
The source-CI archive boundary uses the explicit Script archive interface and
the current profile/native graph selectors; project reader paths are bounded
static inputs, not caller-supplied graph or deployment proof.

## Verification and evidence

Writers/Fix run affected tests and focused production-shaped wrapper/helper
checks. The configured verifier owns one fresh full checkout-local
`./scripts/validate` run after writer bookkeeping, with an existing Python
3.11+. No `--installed-state`, dependency installation or global PATH changes.
Compile stdout contains only `{"transition":"passed"}`, `failed` or `blocked`
in that same object shape; logs go to stderr.

The compile child emits two stderr-only `KENT_VERIFICATION_IDENTITY` records.
The closed `kit-verification-identity-v1` contains `schema`, `source_sha256`,
`environment_sha256`, `head`, `workspace_path`. Source identity includes
workspace/HEAD, tracked bytes/modes/deletions, nonignored untracked source,
in-root symlink targets and actual plan/procedure/helper/validator inputs;
Git administration and ignored outputs are excluded. Environment identity
includes OS/architecture, resolved Python/tool bytes and versions, and hashed
effective environment. A validated private TMPDIR uses its stable containment
contract, not the random basename.

The read-only identity reader is bounded to 20,000 entries/256 MiB. Missing
HEAD, unsafe symlinks, unstable inputs, environment drift or tampered report
identity invalidate a pass; validator failure stays failed. This cooperative
freshness check is not native authentication or permission isolation.
Uncertainty requires fresh verification, not cache reuse or skipped checks.
Carry the complete typed report/log and matching identity references through
existing required carriers; optional narratives reference those artifacts.

Runtime checkpoints/evidence use ignored `.kent/runtime/`; verifier/private
temporary outputs use ignored `build/kent-workflow/`. Resolve task-fixed
legacy/R2 mode via project memory. R2 accepted state is external; use protected
compare-and-swap progress updates and never edit immutable revisions or create
a `.todo` fallback. Preserve legacy task paths.

Evidence is append-only. Use genuine current Session/Step identities and
truthful checks/context; optional telemetry need not be invented. Historical
schema-1 proof retains its original bytes/identity. Conflicting or fabricated
proof blocks; do not reconstruct or reseal it. Current Step is inherited,
not independently authenticated. The configured Cleanup helper exclusively
owns the final ordinary append/seal and retained preparation; follow
`.kent/commands/cleanup-task.md`, including no standalone blocker append.

## Effects and report-only work

Use the standard managed task worktree. Preserve installed primary, main,
index, symlinks, global configuration and all consumer pins/workflows.
README preflight owns source refresh; native default-branch selection is
local source selection, not a remote fetch. Source authority grants no commit,
push, merge, install, live workflow or consumer effect.

Explicit report-only/no-repair authority permits `plan_path=not-applicable`
and no tracked/staged writes. Retain evidence in Task records or ignored build
outputs. `report_only`/`no_pr` cleanup additionally requires exact HEAD equal
to a current published branch tip; preserve its bootstrap source branch.
