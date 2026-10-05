# KEN-23 — Prevent release source-pin drift

## Authority and scope boundary

Work kind: `feature`. The shared outcome adds an explicit source check; the
historical Puber defect is an independently delivered native project bugfix.
Immutable Kit baseline: `4d0e514aebae5295c803693d72f6640ab2c7846d`.

Root authority is KEN-23 task body and original human supervisor Session
`98351c2e-c7e4-4151-9e17-0bc65f9f4289`, message 3475,
Step `10b78040-5140-4940-aec0-c66dcff711c6`, October 4, 2026:
“но проверка Puber release-builder падает на старом несовпадении digest у
неизменённого README с гриль агентом поищи фикс такой, чтобы в будущем это
не стреляло ни на одном проекте и запусти фикс”.
This plan does not supersede or narrow that task. It proposes exact source
implementation and four new native adoption tasks for subsequent explicit approval.
No approval, task start, source publication, consumer effect or restart is
claimed. Plan independently read native events.jsonl records seq3475 and
seq3512 in that supervisor Session; both have payload.role=user and the exact
Step above. Message3512 asks for a partial rollout/restart opportunity but does
not authorize unknown activation effects. It supplies scheduling intent only.

Included source IDs: KEN-23 shared prevention and native project adoption;
Puber historical README pin defect at `f1556858049ef156c6640613fb7cb3cf3b563a34`;
KEN-11 hold `b38d19af-7b84-4781-ba4e-46b16540425a` as a dependent readback
consumer, not an implementation scope. PUB-84, KEN-18, KEN-22 and KEN-21 remain
separate. No changes to their accepted plans, evidence, retained execution or
live state. No SDK/API/generated domain contracts or dependency upgrades.

## Discovery and critique

Original requested grill research: Session
`ca1cc29b-8889-44dc-a00d-fd956bc4afcd`, assistant 202,
Step `e6c071e7-fed6-4b00-b43a-2b11c5b1f807`. The same Session recovered the
report to this Plan Session `5b3cad1d-a812-4ca6-88d9-fb943fd0b979`.
This is the requested initial critique/research, not a formal PASS.
Do not commission duplicate initial grill research.

Plan independently verified original Puber Git evidence:
`ffd93fd69555a29e931a1baa9ac7606a510c341e` README and manifest match
`322e3948b172dc8b5bda36e8cf5facc6d5b8df0a7d60d8010703014ed1210f52`.
`f1556858049ef156c6640613fb7cb3cf3b563a34` changes README without manifest;
both `f640524f31696592f8532bda1162953601b9fa9f` and delivered PR91
`d51d489222e63c9ff215864b48b5022be99692ee` retain actual
`0dfa155161e5096cea91a966713babe8ae69cee7165f2a13adfa8073b27cd95d`.
This is not a PUB-84 regression. No builder red run has yet been executed.

Fresh October 4 read-only `ls-remote` and selected Git blob audit:

| Native project / repository | Delivery target and observed tip | Bindings |
| --- | --- | --- |
| AppsomeAndroid / OsomePteLtd/AppsomeAndroid | release/4.31.0, `532797e49a6b37f0da295d06150f3217297bee16` | 27, all match |
| Puber / rovkinmax/Puber | master, `d51d489222e63c9ff215864b48b5022be99692ee` | 46, only workflow README mismatches |
| OsomeAPI-SDK-generator / OsomePteLtd/kmp-mobile-shared | main, `3d9206a4c2d297f194fb99785eab65c821a3a0d6` | 2, both match |
| osome-slack-reader / OsomePteLtd/osome-slack-reader | main, `1a0bfd7c0d7bbfd54885515b27b8d286570b705a` | 1, matches |

SDK tracking acquisition fetched only origin main; dirty primary/index/local
HEAD were preserved. Its previous sampled revision is not current remote.
Appsome origin/HEAD master is not the delivery target. These audits are
source-only and do not establish full builder/runtime admission.

Critique dispositions: preserve both strict codecs; no auto-repin; independent
Puber delivery first; Git-selected regular files only; project-owned SDK
mapping; fail unknown kinds; no readiness redefinition; vendored CI helper;
documentation and integration-tree checks; retain full release checks.
All are incorporated below. Earlier KEN21 critique is irrelevant.

## Small mechanism and exact interface

One authoritative standalone Python 3.11+ standard-library executable:
`scripts/check-release-source-bindings`. No import of installed Kit, builder,
project runtime, framework, package or plugin; no interpreter/dependency install.
Consumer materialization is a reviewed byte-identical vendored copy at
`.kent/scripts/workflow-check-release-source-bindings`, taken from the accepted,
published Kit source commit. Native writer records that commit and SHA-256.
No new installer/synchronizer/template registry, hook or automatic updater.
Future helper upgrades require a separate project-owned source change.

CLI: `--project <repository-root> --ref <explicit-commit-or-tree>`.
Optional `--kind-map <repository-relative-json-path>` reads that mapping from
the SAME selected Git tree. Missing or ambiguous refs fail. Resolve once to a
tree OID; report both requested selector and resolved tree, plus commit when
available. Never default to HEAD. CI passes the explicit checked-out commit
OID; PR checkout is the synthetic merged commit, not PR head. Writer checks
the exact fresh-target integration commit/tree before delivery.

Read selected `.kent/workflow-profile.toml`, its release spec, then
`source_manifest.path`; validate necessary schema/version/identity/path and
descriptor contracts. Reject duplicate JSON/TOML declarations, malformed
types, missing required fields, unsupported schemas and duplicate kind/key.
Different descriptors may resolve to the same blob when their decoded expected
digests agree (Puber legitimately binds its builder as both builder and source).
Conflicting expected digests for one normalized path fail. Read each blob once,
verify each descriptor, and report checked_count as descriptor count.
Validate normalized path and Git mode before any cached blob reuse, including
different paths sharing an object ID. A successful alias never hides a failed
descriptor. Equal decoded digests across the two strict codecs are compatible.
Require ProjectProfile schema 4 and release spec schema 2 or 3; schema-3
Kit is not a consumer and produces no misleading successful admission.
This is a bounded binding check, not a replacement for full release schema,
closure, graph or job-policy verification.

Kinds: `builder-sha256` gets path from profile.release.builder_path;
`source-sha256` accepts exactly normalized `path=digest`. Custom kinds use a
closed JSON object `{"schema":"release-source-binding-kind-map-v1","paths":
{"principal-policy-sha256":".kent/workflows/specs/osome-sdk-publication-principal-policy.json"}}`
in SDK only. Generic implementation accepts bounded unique declarative
kind-to-path entries; forbids overriding built-in kinds, unused entries and
unknown unmapped kinds. Custom keys are digest-only. No product conditions,
executable adapters or command expressions.

Both accepted digest codecs remain lowercase hex64 or exactly
`sha256:<eight colon-separated hex8 groups>`. No whitespace/case tolerance.
Each external descriptor must require runtime digest and identify a regular
Git blob, mode 100644 or 100755. Reject absolute/escaping/noncanonical paths,
symlinks, gitlinks, trees, missing blobs and unsupported descriptor keys/types.
Only fixed read-only Git plumbing is executed, with no shell, hooks,
replace objects, lazy fetching, inherited GIT routing or automatic maintenance.
No network, builder, Gradle, signing, Kent, index refresh or working-byte reads.
Bound cumulative reads to 32 MiB / 60 seconds, individual command output to
8 MiB / 30 seconds, and descriptor/mapping counts to 4096; fail before
unbounded buffering. Do not expose raw blob contents.

Success reports `source_bindings_valid=true`, selected identity and checked
count. Failure exits nonzero with kind/path/expected/actual when relevant.
No `ready`, runtime-attested, activation or full-admission assertion.
Existing preflight-revision behavior and runtime captures remain unchanged.

## Closed source file set and dependency closure

Kit writer only:
- `scripts/check-release-source-bindings` (new authoritative executable).
- `tests/test_source_bindings.py` (new self-contained temporary-Git fixtures).
- `tests/fixtures/source-bindings/consumer-ci.json` (new bounded four-project
  structural CI fixture, containing event/job/checkout/check-step contracts,
  not copied credentials or broad repository sources).
- `tests/fixtures/source-bindings/puber-workflow-readme.md` (exact historical
  d51d489 README blob, with provenance and old/actual pins in the test module;
  no dependency on a local Puber checkout during normal tests).
- `contracts/release-source-bindings.md` (new normative narrow contract).
- `README.md` (explicit command, limitations and vendored adoption).
- `scripts/validate` (compile and focused suite registration only if needed;
  existing discovery must remain the single full verification entry).
- `.todo/KEN-23/plan.md` and review receipt (planning artifacts).

No workflow graph delta, profile schema change, runtime envelope change,
revision.py/CLI behavior change, adapters or configuration change. This avoids
KEN-18/KEN-22 production overlap; README/validator target changes must be
integrated after accepted concurrent source work, without copying their deltas.

Each adoption writer owns exactly these files in its native managed root:
- `.kent/scripts/workflow-check-release-source-bindings` (identical Kit bytes).
- `.kent/scripts/tests/test-release-source-bindings` (native fixture/wiring test).
- `.kent/workflows/README.md` (writer invocation and scoped pin update guidance).
- Active source manifest listed below (closure additions and only affected pins).
- Existing project CI file listed below (insert check immediately after checkout
  in the designated existing job, before build/dependency setup).
- SDK only: `.kent/workflows/source-binding-kinds.json` (declarative mapping).
- SDK only: `.kent/scripts/tests/test-github-publication-workflows`
  (extend existing historical scope guard to remove exactly the approved
  required-job checker step as well as its existing mirrored bot-merge delta;
  retain OSDK13_BASELINE and unrelated-mutation negative tests).
- SDK only: `.kent/scripts/tests/test-workflow-sdk-publication`
  (extend FIXTURE_SOURCE_PATHS with exactly helper, new native test and map,
  preserving current manifest and missing-closure-file rejection).
- SDK only: `.kent/scripts/tests/test-sdk-runtime-contract-closure`
  (add exactly helper, new native test and map to existing ALLOWED;
  existing adoption spec/CI/docs/manifest/test paths are already allowed).
- Each project's active release spec below: add the same normalized ordered
  CI step to the selected existing required job; no other behavior.
- Appsome only: `.kent/workflows/builders/appsome_release_publication_v22.py`
  (update only REQUIRED_JOBS_SHA256 for the approved step adaptation).
- Appsome only: `.kent/scripts/tests/test-appsome-release-publication-graph`
  (update only audited build_stage_apk.yml digest);
  `.kent/scripts/tests/test-release-publication-v2-2-graph` and
  `.kent/scripts/tests/test-runtime-v2-command-closure` (update only current
  required-job digest expectations and misleading immutable-baseline comment).

Exact active manifests:
Appsome `.kent/workflows/appsome-release-publication-v22.manifest.json`;
Puber `.kent/workflows/puber-release.manifest.json`;
SDK `.kent/workflows/osome-sdk-engineering-delivery.manifest.json`;
Slack `.kent/workflows/osome-slack-reader-release.manifest.json`.
Add helper, test and SDK mapping to normalized additional_paths where not
already covered. Preserve all existing external roots and source closure;
repin only existing bindings whose approved bytes actually change.
No self-digest cycle. No blanket manifest emit/repin.
Active specs:
Appsome `.kent/workflows/specs/appsome-release-publication-v22.toml`;
Puber `.kent/workflows/specs/puber-release.toml`;
SDK `.kent/workflows/specs/osome-sdk-engineering-delivery.toml`;
Slack `.kent/workflows/specs/osome-slack-reader-release.toml`.
All four selected specs record ordered steps, so these are required dependency
adaptations, not optional cleanup. Appsome builder required-job hash and three
native test expectations are fixed at the selected source and require the
bounded updates above; update its manifest builder digest too. Graph bodies,
snapshots, job counts/events and prior release/effect gates remain unchanged.
Source admission intentionally gains one new reason for refusal. Prior
job-contract/runtime receipts do not qualify changed specs/hashes.
Any additional builder/snapshot/spec dependency discovered during native Plan
requires a revised exact preview and reviews before editing, not silent widening.

CI: Appsome `.github/workflows/build_stage_apk.yml`, existing `detekt`;
Puber `.github/workflows/pr-checks.yml`, existing `detekt`;
SDK `.github/workflows/check-pr.yml`, existing `check`;
Slack `.github/workflows/ci.yml`, existing `quality`.
No new jobs or shared Workflow stages. Do not add path filters or change event
selectors/permissions/runner/required checks. All already run on documentation
PR changes; checkout default PR merged revision is retained. Existing release
builders and full checks remain present. Merged delivery is additionally
checked on the fresh target integration tree by the native writer, so this
preview does not introduce push events with unintended Gradle/publication costs.

## Existing repair plus four native adoption tasks

Total consumer task count: FIVE. Repair PUB-85
(`task-f357909b-3724-4128-aceb-e4bc3dd4dbbe`) is already running under the
supervisor's original human start authority, verified through native task show.
Exactly FOUR additional adoption tasks are proposed, not created or started.
Each follows its existing linked native Engineering Delivery workflow:
Puber `7061fc98-aae5-4468-9d89-fa28bfe30c72`;
Appsome `28850ebc-392a-4dcc-a40e-e6245eab9c83`;
SDK `a0a2a900-9f98-46bf-84e4-7661a95189eb`;
Slack `a2dbdf14-8b03-4451-8eb1-db77a4f8b532`.

1. Existing PUB-85 Puber narrow pin repair: bugfix, selected d51d489 master source,
   historical native Plan Session `41be9273-c8f9-44a1-9d72-291393ae9d8b`.
   Native implementation Session observed later:
   `934cd841-ae8d-42a9-96ec-1882a866fdfb`; task state remains authoritative,
   not this historical observation.
   Production boundary ONLY `.kent/workflows/puber-release.manifest.json`,
   ONE existing `.kent/workflows/README.md` pin. Native planning/evidence may
   record checks; no helper/adoption or PUB84/KEN11 edits. Do not create a
   duplicate repair task. Its native governance still precedes edits. Independently
   deliver before shared mechanism or any adoption. No wait on SDK.
2. Puber prevention adoption: project-owned automation/feature routing verified
   by native Plan, closed adoption files above. Start only after narrow repair
   delivery and shared helper publication.
3. Appsome prevention adoption: native supported automation/feature routing,
   fresh release/4.31.0, not master. Start after helper publication.
4. SDK prevention adoption: native automation, fresh main, separately
   authorized Kent-managed task root. Never install over or incorporate dirty
   primary. Start after helper publication and managed-root authorization.
5. Slack prevention adoption: native automation, fresh main.
   Start after helper publication.

This plan's later human approval must explicitly cover the FOUR new starts and
native managed-root scopes; otherwise record these as pending external
dependencies, not completed rollout. Do not start parallel arbitrary writers.
One writer per native slice; Puber tasks sequential; other adoption tasks may
run independently only within the exact approved launch budget.
Task creation/start, Git source delivery and installed/runtime effects are
distinct permissions. Each native Plan preserves its own review/approval gates.

## Writer-owned ordered steps and evidence

- [x] Classify shared feature; preserve task authority and issue boundaries.
- [x] Recover original grill report and verify historical Git mismatch.
- [x] Audit fresh selected target blobs for all four actual consumers.
- [ ] Freeze preview, record its raw SHA-256 and obtain first independent
  read-only architecture review; separate Plan Review supplies second PASS.
- [ ] Obtain human approval on that exact hash before production changes.
- [ ] Native Puber repair writer FIRST captures deterministic historical
  full-builder RED in a clean managed root at d51d489 (or contained fixture
  with identical original committed closure), before any production edit.
  Record command, selected commit, exit/log and README/pin hashes. This is
  writer-owned evidence, not a later user approval requirement.
- [ ] Native Puber writer changes only that pin, records scoped diff, runs
  actual source-only release builder
  `python3 .kent/workflows/builders/puber_release.py --check .kent/workflows/puber-release.json`
  and its graph/manifest validation readbacks; preserve historical RED.
  Fresh-target integration repeats full source-only checks. Normal native
  verification/review/delivery owns PR. Notify KEN11 with actual repair commit
  and successful readbacks; its owner settles the existing hold without
  rewriting historical evidence or changing its accepted scope.
- [x] Kit writer FIRST adds deterministic regression fixtures and captures
  the historical drift test RED before helper production code is written.
  Test-only file creation is permitted; no fabricated historical red.
- [x] Implement standalone helper and contract; register targeted tests and
  validate parity/materialization instructions without changing consumers.
- [x] Run focused suite and production-shaped command fixtures; preserve
  machine-readable results and log pointers under ignored build output.
- [ ] Existing configured verifier owns ONE fresh full `./scripts/validate`
  for unchanged Kit slice; normal independent review and PR delivery follow.
- [ ] Each native adoption writer FIRST captures its wiring test RED, vendors
  accepted published helper bytes, makes only scoped source additions/pins,
  tests documentation-only and selected merged-tree drift, and runs existing
  project full source checks. Record helper commit/SHA, fresh target, selected
  integration tree, scoped manifest diff and check results.
- [ ] Coordinator records four separately delivered adoption receipts before
  claiming all-project prevention. Partial adoption is explicitly partial.

Owners: Kit writer owns helper regression RED/GREEN, CI fixture contracts and
source report; native Puber repair writer owns original builder RED/GREEN and
repair delivery; each native adoption writer owns actual CI/writer wiring,
closure/pin audit and delivery. Workflow verifier/reviewers own their normal
verification and review receipts. KEN11 owner owns hold settlement. Missing
agent evidence is recovered safely or blocks the owning slice, never converted
to a request for the user to produce tests.

Required focused coverage: both codecs; builder/source/custom-policy kinds;
missing, unsafe and unsupported Git entries; malformed schemas/types,
duplicates and unknown kinds; tree selector and commit selector equivalence;
dirty worktree/index independence; selected integration tree mismatch;
strict maps including missing/unused/override; bounded bytes/count/time/output;
no source/index/runtime writes or network/builder/project-command invocation;
no inherited Git routing/lazy fetch/hooks; unchanged preflight readiness;
real four-project CI fixture job/event/check-step coverage and native
documentation-only RED/GREEN. Test project-owned map without SDK branch logic.
Include the legitimate Puber builder/source path alias and conflicting alias
case. CI fixtures and native tests validate CI + spec ordered-step parity and
Appsome builder/test policy digests, not merely presence of a check command.
Native adoption evidence must include inverse-delta proof: remove exactly the
new check step from normalized YAML/spec job contracts and compare with the
approved selected preimage. Every other job/step, condition, permission,
environment, effective shell/directory, checkout setting and event is unchanged.
Appsome update order: CI step, normalized spec, computed builder/test policy
expectations, affected manifest pins. Compute new hashes from verified bytes;
do not copy failure messages. Preserve negative tests and six-job constraints.
Alias tests include equal digests with different codecs, unsafe path/mode
sharing an object ID with a safe cached input, and individual descriptor failure.
The source audit is not a substitute for those writer-produced tests.

First independent review of the original draft:
architecture-designer Session `98239bef-86e4-4167-97c5-132bdad4c399`,
Run `14ae9058-f546-4576-a585-745f2ef330e4`,
Step `ed6e8735-fcff-4d91-a31f-bbf1e1f402b3`,
raw hash `c910b8ddf398cf3a9f9af1597c5802c23d16552157f253d21dc7b852b40fc4e8`,
verdict BLOCK. Findings: valid duplicate resolved-path alias rejected; mandatory
four-spec/Appsome builder/test closure missing. Both dispositions incorporated
above; schema matrix clarified. The old review is not PASS and cannot bind
this revised preview. Refresh bounded critique for this substantive dependency
closure change, then freeze and repeat first review before Plan Review.
The same requested grill Session performed the bounded delta critique after
this dependency expansion: both findings resolved, no new required file
expansion found. Its three refinements (alias cache safety, inverse-delta
policy proof, ordered hash computation/admission wording) are incorporated
above. This critique remains non-PASS research evidence.

Second first-review attempt: same architecture Session,
Run `0ae0f2f3-7e6d-4aca-bf04-226f5108696f`,
Step `fe15caf7-e613-44eb-874c-462d6a23f2e1`,
hash `4337ee5b0c2c80b0e8aed4678df01404faed056a0ca8573ea1a1d7accf5de98e`,
verdict BLOCK. Initial findings were resolved, but SDK historical scope guard,
publication fixture closure and workspace ALLOWED were incompatible with the
new step/additions. Their exact three test-file adaptations are now included.
No baseline replacement, preflight bypass, ignored source omissions or early
commit permission is introduced. Native SDK writer records baseline behavior,
then FIRST constructs a disposable candidate carrying the proposed source
delta with the three original tests; captures their deterministic guard/
fixture/allowlist failures before any production task-root edit, then verifies
the adapted tests against that same candidate. Do not claim baseline itself
fails these new adoption checks or count absent historical Git objects as RED.
Temporary fixture Git commits are test mechanics, not task-root delivery
commit authorization. Preserve negative checks and unchanged historical source.
Original delta critique locator: same grill Session seq235,
Step `df09c2c0-6bcf-4962-ace4-bb91bb89c102`.
The same grill's second bounded SDK delta critique accepts the three files
and adds these incorporated criteria: scope guard removes exactly one full
normalized step in the correct required job/position only after checking all
effective fields; baseline self-test remains, while current candidate must
contain the checker. Negative tests mutate command/job/position/duplicates.
Fixture copies actual selected candidate helper/test/map bytes, not primary
or placeholders; individually missing each added path fails. ALLOWED keeps
exact entries and untracked checking, rejects an unrelated fourth path, and
must pass before normal delivery commit rather than by clearing the diff.

## Rollout, rollback, restart and limits

Source-only first; consumer copies receive changes solely via approved native
PR delivery. No automatic propagation from local installed Kit. Narrow Puber
repair may settle KEN11 before any other delivery. All-project completion
requires four adoption receipts, not four sampled clean manifests.

Rollback is a reviewed source revert per slice, restoring prior helper/CI/map
and affected pins together. Reverting Puber repair knowingly restores the
historical blocker and cannot be called successful qualification.
No deletion/reset, live Workflow graph/default/link mutation, locked Session
rewrite, task replay, signing/devices/resources, cache/dependency installation,
primary fast-forward, configuration or restart. Restart impact: NONE.
Installed activation/partial runtime rollout and human restart window remain
separate exact compatibility/effect previews; verified human3512 does not
authorize them. Source pin validity never establishes runtime admission.
