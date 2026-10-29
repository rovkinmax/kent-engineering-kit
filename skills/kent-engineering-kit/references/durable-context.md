# Durable Context and Recovery

This reference explains how a new or resumed Session reconstructs the current
Task contract without treating model memory, a copied summary, or a file path as
authority. The approved R2 store is independent of Git and worktree lifetime.
Use only the absolute `project_memory.root` selected for the approved
deployment. It must remain outside the checkout. This reference does not
choose a universal machine-specific path, and a profile entry alone does not
mean that the root or installed adapter is ready.

## Keep durable sources separate

- **Original human decision:** the native user message, Question answer, or
  authorized Task comment establishes the author's actual words and context.
  Its current applicability still depends on scope and supersession.
- **Accepted Task plan:** graph-owned acceptance records the reviewed
  execution contract and digest. References inside it do not authenticate
  human authorship, and acceptance does not prove completion.
- **Working plan:** the current revision is a proposal. A changed contract
  must pass the existing revalidation and review path.
- **Evidence ledger, reports, and checkpoints:** validated records establish
  observations, decisions, checks, and effect state. A label such as
  “decision” does not itself establish authority.
- **R2 project knowledge:** versioned project facts and provenance do not
  grant Task scope, user approval, or instructions that override current code.
- **R2 artifacts and invocation receipts:** exact stored objects and
  operation status do not establish a human decision, a successful external
  effect, or permission to repeat an unknown operation.

Keep these sources linked, not copied into one mutable “memory” file. A plan,
knowledge note, ledger summary, or agent audit may point to an original source;
only that original source can establish what its author decided.

## Discover before asking or continuing

Before continuing after a context change, inspect the task-fixed mode, accepted
and working plan, and the evidence, checkpoints and artifacts declared by the
selected route. Native nodes and edges select that route; a profile, command
map or installed storage interface does not select legacy Plan Contract,
ledger, packet, watcher or Janitor procedures.

### Selected legacy discovery tooling

A retained legacy checkout may predate the inspection and packet commands.
Check its own profile and helper capabilities before invoking them. If the
checkout has not opted into `delivery_reliability_v1`, use its existing plan,
ledger, checkpoint and original decision references directly. Do not require
`inspect` or `pack-delivery`, migrate storage, or request approval again merely
because those commands are unavailable. This preserves the legacy protocol;
it does not permit reconstructing hashes or treating missing evidence as consent.

For a qualified delivery-reliability checkout, use the
read-only `workflow-plan-contract` inspect mode by setting
`KENT_PLAN_CONTRACT_MODE=inspect`. Its request is a closed object:

```json
{
  "schema": "workflow-context-inspection-request-v1",
  "workspace_path": "/absolute/canonical/repository/root",
  "task_short_id": "TASK-123",
  "plan_path": "/absolute/plan/path/or/not-applicable",
  "evidence_refs": [
    {"path": "relative/path/inside/workspace", "sha256": "64-lowercase-hex-digits"}
  ]
}
```

Evidence-reference paths must resolve inside the selected workspace, and each
supplied digest is checked before inspection. A legacy plan path must also be
inside that workspace; an R2 plan path must identify a revision in the
admitted Task's external store.

The `workflow-context-inspection-v1` response provides
`accepted_plan_identity`, `working_plan_identity`,
`plan_comparison`, `evidence_status`, `evidence_locators`,
`checkpoint_locators`, `decision_candidates`, `unresolved`,
`discovery_complete`, and `authority_verification_required`. Candidate
references are marked `unverified_candidate`. An unresolved or incomplete
result means discovery could not establish the facts; it does not prove that
approval is absent or that the original source never existed.

Tagged `task_comment(...)`, `session_message(...)` and `question_answer(...)`
references aid source discovery; other narrative remains unresolved context.

Inspect decision references in every validated evidence record field used for
narrative, including `summary`, `decisions`, `checks`, and `artifacts`.
In particular, a reference kept only in `ledger.summary` must remain
discoverable. The R2 accepted snapshot's `authority_refs` are also candidates.
The inspector validates the ledger view, hashes supplied files, and reports
locators. It does not search every Session, fetch a referenced Question,
authenticate its author, determine whether it applies, or turn a pointer into
consent. Retrieve the original message, Question and answer, or Task comment
through the supported native source. Verify its complete content, authorship,
proposal/answer relationship, Task and scope, later supersession, and any
explicit limit or consumed budget before relying on it.

For a cross-Session decision, retain the owning Session and exact event or
Question/answer locator. If a relevant Question is not already referenced,
inspect the Task's retained Sessions and their answered-Question history
before asking it again:

```text
kent task sessions TASK --project PROJECT --offset 0 --limit 100 --json
kent questions list --session SESSION --max-handoffs 25 --json
kent task show TASK --project PROJECT --json
```

Page the Session list when `kent task sessions` reports more results, and
request enough Question history to include the relevant proposal and answer;
the default `--max-handoffs` window is not proof that no older answer exists.
Use `kent task show` for the original Task comments. Read the exact source in
its owning Session or Task record. A copied transcript or another agent's
summary is not a substitute.

An agent-written summary or a note labelled “decision” is not a replacement
for the original. If the source is available, inspect it before asking the
user to repeat the same decision. If it is truncated, ambiguous, missing, or
only agent-authored, preserve that gap and use only independently established
authority; do not infer permission.

Inspection is read-only. It returns paths and digests, not report contents or
a semantic reconstruction. Follow only the locators needed for this Task.
Never parse a damaged ledger suffix as evidence. A validated recovery view
exposes the validated prefix and continuation while keeping the damaged suffix
opaque; see
[Evidence recovery and retention](../../../contracts/evidence-recovery.md).
Private checkpoints and artifacts also require their owning reader and
retention rules.

For an explicitly authorized report-only Task, `plan_path` is the literal
`not-applicable`. Do not create a tracked plan to make inspection succeed.
The literal is a sentinel, not an accepted plan or approval; the evidence and
original authority still need inspection.

## Route each Task to its original storage mode

Task mode is fixed by native identity and the qualified project installation,
not inferred from a folder name, short ID, existing cache, missing plan, or
worktree location.

- Tasks inventoried as legacy at cutover—including Backlog and terminal
  Tasks—remain on their existing task paths and accepted snapshots. Do not
  migrate them automatically.
- A new native Task enters R2 only after qualified first-ingress storage
  authorization verifies project, Task, canonical execution root, primary
  workspace and genuine current Session, then binds
  `task.json` in the external project store. A graph-owned Script may operate
  on an already bound Task, but it does not create the first binding. Native
  references in `task.json` are checked against current native readback.
- A standalone user-driven Task uses an explicit project and task binding. It
  is not selected by matching a Jira key, feature name, or directory.
  Pre-native intake, when qualified, has its own explicit identity; after a
  native Task is created, bind it separately from real native readback and
  retain the intake source. Intake identity does not create or impersonate a
  native Task.
- An adopted-but-incomplete or conflicting R2 configuration, installation
  binding, cutover receipt, or Task binding blocks. Never fall back to a
  legacy `.todo` path to get past an R2 error.
- Use the legacy route only for a Task in the immutable legacy cutover
  inventory, or for a project that has not adopted R2. New R2 work does not
  use `.todo`; old Tasks keep their old contracts and locations.

### Ordinary storage authorization

Use the qualified installed `kent-project-memory` interface: `workflow` for
native documents and attachments, `mcp` for MCP binding and invocation
operations. Resolve that entry point from the approved installation, not a
caller-selected code path or product-local core. Do not replace a project's
relative `commands.project_memory` with an absolute installed path: the
profile's sibling-closure validation and legacy callers remain intact.

The safely read bound primary profile's `[project_memory]` table supplies
exact `root`, `project_key`, `project_uid`, and `mode = "r2"`. Ordinary storage
authorization also requires genuine native identity, safe private installation
and immutable cutover records, their integrity and closed schemas, the fixed
legacy inventory, and the exact Task adoption identity. The adapter loads
safe installed core bytes only from the private binding's trusted Kit root,
executes that verified read without reopening it, and rechecks relevant
binding/storage state before effects. Preserve namespace, immutable revision,
CAS, retry, privacy and artifact-framing guards.
Native-bound requests require genuine current Task/Session or Script
ownership. Standalone MCP uses native project/primary verification plus its
explicit standalone task binding, not invented native Task ownership.

Historical publication refs, whole-profile digests, file inventories and helper
declarations remain metadata, not admission of current code for ordinary
storage. These calls do not execute `inspect-delivery-source` or require
current publication ancestry, whole-Kit parity, unrelated helper/instruction
equality, graph-artifact parity or a receipt-pinned core digest. Safe compatible
installed-core changes are visible on subsequent calls. Updating a facade or
alias does not relocate the private binding's Kit/core root.

Explicit source inspection, publication and reconciliation retain their strict
source-admission contracts; ordinary storage authorization does not satisfy or
bypass them. `delivery_reliability = "v1"` remains a separate producer-evidence
capability, not storage authority or route selection. A profile switch or this
document does not qualify an installation. Missing, unsafe, malformed or
conflicting storage records block without a legacy fallback. The native Task
path still requires its bound primary source workspace; do not infer
secondary-workspace support, installation or native activation from source
contracts.

## R2 documents and accepted plans

### Standalone intake and attachments

The qualified Kit source command `scripts/project-memory` works without Kent.
Supply explicit `--root`, `--project` and `--project-uid` values; these select
the existing project identity, not a native Task. This CLI is distinct from the
installed `kent-project-memory` alias for the native workflow/MCP adapter.

Create pre-native intake with an `r2-request-v1` JSON request containing
`operation = "initialize-intake"`, a fresh canonical UUIDv4 `invocation_id`,
and a bounded `intent` object. Preserve that ID before invocation. Retrying the
same ID and intent returns the same identity; changed intent conflicts. A new
intake uses a new ID even for the same Jira issue. The result identifies
`tasks/intake-<invocation_id>` and its immutable binding digest. Intent is
hashed for retry identity, not interpreted as authority.

After native Task creation, preserve the intake binding as provenance for the
separate native-bound Task. If post-create admission is unavailable, retain
intake evidence and defer binding to the real Plan ingress. Do not impersonate
the new Task's Session or repeat Task creation to repair storage bookkeeping.
At that ingress, `initialize-task` accepts optional
`source_refs: [{project_uid, task_uid, sha256}]` using the intake result's
`task_binding_sha256` as `sha256`. The adapter verifies the existing same-project
intake identity and bytes before creating the native binding. Exact retries
preserve the reference; an existing binding cannot acquire different sources.

Attachments use immutable content-addressed objects, not Markdown revisions.
For standalone `artifact-write`, send one JSON header line with
`schema = "r2-request-v1"`, `operation`, `task_key`, `sha256`, and integer
`byte_size`, followed by exactly that many raw bytes. `artifact-read` takes a
JSON request with `schema`, `operation`, `task_key` and `sha256`.
The artifact response is one `r2-artifact-response-v1` JSON line containing
`operation`, `object_sha256`, `byte_size` and the generated `object_path`;
a read response then contains exactly those raw bytes. Objects are bounded to
8 MiB and verified by digest. Do not supply an arbitrary destination path,
edit the returned object, or treat binary output as JSON.

For native Tasks, use the workflow adapter with `schema =
"r2-workflow-request-v1"`, `workspace_path`, and the current Session identity
(optionally `task_locator`). `artifact-write` uses `sha256` and `byte_size`
followed by LF and raw bytes; `artifact-read` uses `sha256`. Do not provide
`task_key`: the verified current Task owns the object. Both return the same
`r2-artifact-response-v1` header and binary read framing as the standalone CLI,
not the ordinary workflow JSON envelope.

### Revisions and acceptance

R2 stores immutable project and Task identities in `project.json` and
`task.json`. Markdown documents are addressed by explicit keys such as
`knowledge/<document>` and `tasks/<task-key>/plan`; the current workflow
adapter limits Task-document operations to the current admitted Task. No
operation resolves a Task from “the latest” file or a global current-Task
pointer.

Read revisions through the adapter and publish with the exact expected current
revision. Stale writers conflict; immutable content is never edited in place.
An exact published retry can settle by identity; partial/conflicting state is
not successful publication.

Accepting a Task plan is separate from publishing its working revision. The
immutable `r2-accepted-plan-v1` snapshot binds the project and Task UIDs,
`task.json` digest, accepted working revision and raw digest, normalized plan
and normalized digest, parent accepted head, `work_kind`, `authority_refs`,
coder selection and digest, and `plan_kind`. `accepted-head.json` identifies
the current accepted snapshot. Accept checks both the expected working
revision and expected accepted head under the plan-document lock. Thus two
concurrent acceptances cannot both replace the same baseline, and an old plan
path cannot hide a newer working head.

The snapshot's `authority_refs` are source pointers, not authenticated
approval. The storage core checks their shape, not authorship or meaning.
Workflow acceptance must preserve the references required by the existing
graph and project gates; readers must still retrieve and verify the original
sources.

Carry the exact reviewed immutable plan revision and digest, applicable
requirements/design revision references, and verified original decision
locators in the plan or the selected Workflow's declared acceptance inputs.
Do not accept the latest working head by convention, treat publication as
acceptance, invent request/metadata fields, or use a knowledge summary as the
reviewed contract.

Selected legacy acceptance can recover missing tagged pointers from validated
ledger narrative, including `summary`. This discovers locators, not authorship
or consent, and never parses damaged suffixes.

R2 normalization treats line-ending changes, trailing whitespace, final blank
lines, and checkbox progress as non-contract changes. A new immutable working
revision may therefore be published while the existing accepted snapshot
remains authoritative. A changed normalized plan or applicable coder
selection is drift and must follow the selected route's revalidation and
independent review before a new acceptance. Knowledge revisions do not silently
expand a Task plan. Sessions reread relevant knowledge before decisions; model
memory is not updated automatically.

The workflow adapter's closed `r2-workflow-request-v1` interface accepts
`resolve-task`, `initialize-task`, `read`, `publish`, `accepted-head`,
`resolve-plan`, `check-plan`, `prepare-report-only`, and `accept-plan`.
It also supports the attachment operations documented above and the read-only
Script-owned `resolve-janitor` source descriptor query.
Operation-specific fields are exactly those implemented by
`templates/project/workflow-project-memory`: `document_key` for `read`;
`document_key`, `expected_revision`, `content`, `operation_id`, and
`provenance` for `publish`; `plan_path` for `resolve-plan`;
`coder_selection` for `check-plan`; `provenance` for `prepare-report-only`;
and `expected_revision`, `expected_accepted`, and `metadata` for
`accept-plan`. The optional `native_context` request field carries the
injected `_kent` object with `task_id`, `node_id`, and optional
`transition_branch_key`; it is not a caller-selected identity. The current
adapter permits `initialize-task` only from a verified current Session, not a
Script. Do not invent extra request fields or treat implementation plumbing as
a new authority interface.

`resolve-plan` retains `supplied_revision`, `working_revision`, `working_path`
and `working_sha256`; `supplied_sha256` and `supplied_comparison` are additive
response fields for comparing the exact supplied revision with the working
revision. None of these locators or comparisons establishes acceptance.

Session callers may supply `task_locator` with the exact native Task ID to
avoid scanning the project inventory. It is only a lookup hint: the adapter
still verifies the inherited current Session, project, workspace and Task.
It cannot be combined with Script `native_context` or standalone bindings,
and cannot replace a missing inherited Session identity.
MCP call/list wrappers expose the same hint as
`--task-locator <native-task-id>` and preserve it across invocation operations.

## Graph-owned acceptance and report-only plans

The workflow adapter permits `accept-plan` and `prepare-report-only` only for
an actual current native Script whose graph `script_path` equals
`commands.plan_contract_accept` from the safely read, private-bound PRIMARY
workspace profile. It verifies primary/project/native identity and rechecks
the binding and selector before the effect. Matching an unrelated genuine
Script to a caller-local command mapping cannot grant acceptance. A missing or
unsafe acceptance mapping blocks those operations, not unrelated ordinary
document or MCP operations. There is no Agent acceptance bypass.

The selected Workflow must declare its acceptance owner and applicable review
and project approval gates. Using storage does not implicitly select the
legacy Plan Contract Accept procedure. Mechanical Script ownership does
**not** add a universal human-approval gate or authenticate a referenced
decision. Do not ask for new approval merely to repair a pointer or agent-owned
bookkeeping; do ask when the applicable contract requires a genuinely new
decision.

For report-only work, the working plan content is exactly
`not-applicable\n`; the accepted snapshot has `plan_kind = "report_only"` and
no coder selection. Preparation is an idempotent graph-owned publication, not
acceptance. The selected acceptance node still performs the acceptance step
after the review and checks declared by that route. No tracked plan is created
and the sentinel grants no permission or positive result.

## Local locator cache and terminal closure

This cache/terminal protocol belongs only to the selected legacy route; it is
not an automatic consequence of using ordinary R2 storage.

For an R2 Task, `.kent/runtime/<task>/plan-contract.json` is only a
`r2-plan-contract-cache-v1` locator produced by the qualified adapter, not a
manually maintained plan/authority record. On
read, compare it with the validated external accepted head and snapshot. If
the cache is absent, check the external accepted state directly; do not
recreate the cache as a read-side repair. A mismatched cache is an error, not
permission to pick either version.

Acceptance may publish this locator only while terminal acceptance remains
open. During existing terminal preparation, before preparation completes and
before Janitor retirement, the terminal owner retains exact cache bytes when
present and the external snapshot locator and digest under the project's
retention protocol, then retires only the local cache. Cleanup preparation, a
terminal seal, or an existing terminal sentinel closes acceptance. After that point, a
retry may verify existing retained state but must not recreate the cache,
reaccept a plan, or resurrect a report-only sentinel.

The cache/seal/Janitor lifecycle above applies only when the selected legacy
route declares it. A separately qualified Agent cleanup route must use its
own declared retention and recovery procedure; storage access alone does not
qualify that route or select it automatically.

The external store outlives its worktree, but its local revision history is not
an off-machine backup. Do not delete or garbage-collect old revisions as part
of ordinary Task cleanup. MCP raw objects, schema caches, call receipts,
runtime effect checkpoints, and evidence ledgers remain in their own
task-bound protocols; none is project knowledge or authority.

## Compact plans and request framing

Keep the current working plan concise and reference immutable prior revisions
for history. Preserve every applicable restriction, acceptance obligation,
remaining action and original decision locator; shortening is not waiver.
Ordinary plans do not inherit operation/portfolio canonical-JSON requirements.

Use each helper's declared request schema and framing. Preflight the complete
serialized UTF-8 request, including escaping, envelope and inline content,
against its actual bound before publication. Binary attachments use their
header/raw-byte protocol above. Do not invent fields, raise global limits or
turn missing agent paperwork into another human permission request.
