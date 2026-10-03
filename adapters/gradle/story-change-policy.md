# Gradle supported UI story-change evidence

## Ownership and applicability

This opt-in adapter defines a project-owned evidence contract for UI changes
in Gradle repositories. It does not add a shared workflow node, select a story
framework, or make a consuming project enforce the contract by itself.

Apply the requirement according to the delivered source diff, across work
kinds: every changed, catalog-supported UI state or interaction must map to
one or more added or meaningfully updated registered stories and fresh checks.
Executing an unchanged story or adding an ordinary unregistered preview is not
a story change. A runtime change elsewhere in the same file does not make an
unrelated preview a meaningful update. Several related states may map to one
story; repeated references are allowed, ambiguous duplicate story definitions
are not.

Split mixed changes into supported and unsupported states. Each supported
state keeps its story mapping; a genuinely unsupported state needs its own
concrete reason and alternative evidence. Missing catalog capability is not an
unsupported-state exemption. Use `non_ui` only when review of the actual diff
finds no UI change, and explain that determination.

The adapter checks bounded packet structure, path containment, file hashes,
and consistency with a separately supplied verifier context. It does **not**
authenticate compiler output, determine whether a state list is complete,
prove that a story changed meaningfully, or grant story PASS. The consuming
project's verifier and reviewer must inspect the delivered diff, real registry
output, commands, results, and retained artifacts.

## Expected verifier context

The project verifier supplies the expected context separately from the
evidence packet. Build it from project-owned policy and the actual verification
workspace: immutable task baseline, current source revision, and digest of the
actual task diff. Never derive these values or the capability exemption from
the packet being evaluated.

The closed `story-change-expected-context-v1` object is:

```json
{
  "schema": "story-change-expected-context-v1",
  "project_id": "appsome",
  "capability": "connected",
  "permit_deferred_not_connected": false,
  "baseline": "0123456789012345678901234567890123456789",
  "revision": "abcdef0123456789abcdef0123456789abcdef01",
  "diff_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

`capability` is `connected` or `not_connected`. A connected context cannot
permit not-connected deferral. Set `permit_deferred_not_connected` only when
the project's separately approved policy explicitly permits that deferral.
The CLI resolves context and packet paths relative to the current workspace;
the workspace root is the CLI's current directory, not a caller-selected
identity inside either JSON document.

## Evidence packet

The closed `story-change-evidence-v1` packet contains exactly:

- `schema`, `project_id`, `capability`, `applicability`, `baseline`,
  `revision`, `diff_sha256`;
- `reason` (null for connected UI evidence; required for non-UI or an
  explicitly deferred not-connected result);
- `states`, `stories`, `registry`, and `checks`.

`applicability` is `ui` or `non_ui`.

Each state has `id`, `support`, `reason`, `story_ids`, and
`alternative_evidence`. `support` is `supported`, `unsupported`, or `unknown`.
For a connected project, `unknown` fails: a missing capability must not be
recast as unsupported UI. A supported state has at least one `story_id` and no
unsupported reason or alternative-evidence exemption. An unsupported state
has a reason, no story mapping, and at least one alternative-evidence path.

Each story has `id`, `change`, `source`, `source_sha256`, `change_evidence`,
`change_evidence_sha256`, `identity`, and `check_ids`. `change` is `added`,
`updated`, or `unchanged`; `unchanged` never satisfies a changed-state
requirement. The source and change-evidence files must exist in the current
workspace and match their declared SHA-256 digests. `identity` is the exact
three-part object `{ "group", "component", "style" }`. Project review must
confirm that the mapped story itself changed meaningfully; a hash or an
`updated` label alone does not prove this.

`registry` is null when no connected story coverage is asserted. Otherwise it
has `artifact`, `check_id`, and `identities`, where each identity has the same
three fields as a story identity. Every mapped story identity must appear in
this list, and `check_id` must identify a fresh passing `registry` check for
the same artifact. The adapter only validates this relationship. Review must
verify the artifact came from the project's real registry rather than a
hand-entered name, source annotation, or ordinary preview.

Each check has `id`, `kind` (`registry` or `story`), `command`, `outcome`
(`passed`, `failed`, or `skipped`), `artifact`, `baseline`, `revision`,
`diff_sha256`, and `story_ids`. A story must reference at least one passing
story check, and the check must map back to that story. Every check identity
must match the separate expected context; its artifact must be retained in
the workspace. A nonzero, skipped, missing, stale, or mismatched check does
not satisfy the requirement. The adapter does not execute the named command.

For `non_ui`, include a reviewable `reason` and leave `states`, `stories`, and
`checks` empty with `registry` null. The project reviewer still confirms that
the actual diff is non-UI.

For connected UI, every supported state must map to an added or updated story
whose identity is present in the registry, and each such story must have
fresh passing checks. Unsupported states remain individually visible with
their reason and alternative evidence. If all UI states are genuinely
unsupported, the result is `unsupported`; this is not a story PASS.

## Puber deferred capability

The approved KEN-13 outcome treats Puber's capability as not connected for
this integration. Its project verifier may supply
`capability: "not_connected"` and `permit_deferred_not_connected: true`. For
UI changes the packet must retain the changed state IDs as `unknown`, include
the deferral reason, and contain no stories, registry claim, or story checks.
The result remains visibly `not_connected`, with no story PASS; unrelated
project checks may pass without relabelling this result. A connected expected
context rejects a packet that claims `not_connected`. Do not choose or
implement a catalog framework here. Moving Puber to connected capability
requires its own product decision and approved project adoption.

## Appsome registry and verification

Appsome's registry identity is the actual Showkase metadata tuple
`group`/`componentName`/`styleName`, represented in this packet as
`group`/`component`/`style`. Adoption must use the existing
`Showkase.getMetadata().componentList` registration path and centralized
storybook verification:

```text
./tools/agentw :feature:storybook:verifyPaparazziDebug
```

Do not invent a Showkase export Gradle task. If the existing build/test output
does not retain the metadata needed to establish identity, Appsome adoption
must separately approve and add a consumer-local metadata assertion. The
Paparazzi screenshot check proves only its visual coverage; it does not prove
keyboard, system-dialog, or runtime interaction behavior. Representable visual
states still need stories, and unsupported interactions need their own
project-owned evidence.

## CLI, limits, and result semantics

The standard-library Python API is
`ExpectedContext(...)` plus `evaluate_evidence(expected, packet)`. The CLI
accepts two workspace-relative JSON paths:

```text
python3 adapters/gradle/story_change_evidence.py \
  --context <expected-context.json> \
  --evidence <story-change-evidence.json>
```

It emits one closed `story-change-evaluation-v1` JSON object with `status`,
`reasons`, and retained `artifacts`. Status values are `evidence_ready`,
`not_applicable`, `unsupported`, `not_connected`, and `failed`; none is named
or equivalent to story PASS.

- Exit `0`: the packet is coherent, including explicit `not_applicable`,
  `unsupported`, or deferred `not_connected` results. `evidence_ready` means
  structural evidence is ready for project review, not that delivery is
  verified.
- Exit `1`: required evidence is missing, failed, stale, mismatched, or
  internally inconsistent.
- Exit `2`: malformed/oversized JSON, unknown fields or statuses, unsafe
  paths, or an operational input error.

The adapter bounds each JSON input at 1 MiB; state and story arrays at 128
items each; checks at 256; path/reference arrays at 256; strings at 2,048
characters; and source/change-evidence hashing at 16 MiB per file. Git object
identities must be 40- or 64-digit lowercase hex and diff digests must be
64-digit lowercase SHA-256. Artifact paths must be workspace-relative POSIX
paths; absolute paths, traversal, and symlink resolution outside the workspace
are rejected. Keep full results and artifact references in the project's
existing verification report/log and `review_context`, not a new workflow
parameter or signature.

## Separately gated consumer adoption

This Kit adapter does not modify either consumer. After the higher-priority
KEN-11 mock-scenario change is integrated, prepare separate reviewed
project-owned adoption previews and PRs. Each preview should close its file
set to:

- `.kent/commands/story-change-policy.md`;
- every feature, bugfix, refactor, migration, dependency, and test procedure
  selected by that project's `.kent/workflow-profile.toml`, including the
  applicable task-start/design and writer handoffs;
- `.kent/project-contract.md` policy linkage;
- `.kent/scripts/workflow-compile-verify` invocation and retention of the full
  result in existing report/log and review carriers;
- the project-local copy of this adapter and its deterministic fixture tests.

For Appsome, include its current Storybook recipe and actual Showkase metadata
assertion only if needed to retain registry identities; preserve the existing
`verifyPaparazziDebug` route. For Puber, add only the expected-context
`not_connected`/visible-deferral hook and fixtures unless a later human-approved
capability decision changes that boundary.

Before each adoption preview, resolve exact procedure paths from the selected
project profile and inspect the then-current consumer heads and KEN-11
integration. Verification inputs must include the immutable task baseline,
current source revision, actual diff digest, project-owned expected
capability/deferral, changed state list, actual registry identities where
connected, and retained command/check artifacts. Required checks are the
consumer's affected unit tests and focused verifier wrapper/helper fixtures;
Appsome also runs its existing managed-worktree Storybook/Paparazzi command.
Consumer edits, registry extraction, and rollout remain subject to each
project's own preview, reviews, and approval.
