# Release Source-Binding Check

## Purpose and limit

`scripts/check-release-source-bindings` is a bounded, read-only check of
release-manifest SHA-256 bindings in one explicitly selected Git commit or
tree. It runs before project release builders and reports whether the declared
source bytes match. It does not update or repair pins.

This result is only source-binding validity. It is not release readiness,
runtime attestation, workflow-graph validation, source-closure verification,
publication authorization, or a replacement for the existing release builder
and runtime checks.

The helper is standalone Python 3.11+ standard-library code. A consumer
materializes an identical copy at
`.kent/scripts/workflow-check-release-source-bindings` from an accepted,
published Kit source commit and records that commit and the copy's SHA-256.
Consumers do not import an installed Kit checkout or receive automatic updates.

## Invocation

```text
python3 scripts/check-release-source-bindings \
  --project <exact-repository-root> \
  --ref <full-lowercase-commit-or-tree-object-id> \
  [--kind-map <repository-relative-json-path>]
```

`--project` must be the repository root, not a subdirectory or symlink.
`--ref` is required; abbreviated and symbolic refs are rejected so the selected
object is unambiguous. A commit selects its tree; a tree selects itself. The
helper reports the supplied object ID, resolved tree ID, and commit ID when the
selector is a commit. It never falls back to `HEAD`, the index, or working-tree
bytes.

CI supplies the explicit checked-out commit ID, for example
`--ref "$GITHUB_SHA"`. On pull requests the existing checkout behavior is
retained, including GitHub's selected merge commit. Do not substitute a PR
head SHA or add a new event/job for this check.

The optional kind map is itself read as a regular blob from the same selected
tree. Its closed JSON form is:

```json
{
  "schema": "release-source-binding-kind-map-v1",
  "paths": {
    "principal-policy-sha256": ".kent/workflows/specs/osome-sdk-publication-principal-policy.json"
  }
}
```

Custom mappings only associate a validated kind name with one normalized
repository path. They cannot override built-in kinds, execute code, or define
commands. Every mapping must be used by a descriptor; unused, duplicate, or
unknown mappings fail.

## Selected source and descriptors

The helper reads `.kent/workflow-profile.toml`, the profile-selected release
spec, and that spec's `source_manifest.path` from the selected tree. It requires
ProjectProfile schema 4 and release spec schema 2 or 3. It checks matching
project name, topology and adoption identities across the profile, spec and
manifest before examining `external_roots`.

Each external-root descriptor must have exactly `kind`, `key`, and
`runtime_digest_required: true`. Duplicate JSON keys, duplicate kind/key
pairs, unsupported fields or kinds, malformed values, and unsupported schema
versions fail closed.

Built-in descriptor semantics:

- `builder-sha256`: `key` is the expected digest and the path is taken only
  from `profile.release.builder_path`.
- `source-sha256`: `key` is exactly a normalized `path=digest` value.
- A mapped custom kind uses its mapped path and a digest-only `key`.

Digests are either lowercase 64-character hexadecimal SHA-256 or exactly
`sha256:` followed by eight lowercase hexadecimal groups of eight characters,
separated by colons. Both encodings are decoded before comparison. Different
descriptors may bind the same path or Git object when their decoded expected
digests agree; conflicting expectations fail. `checked_count` counts
descriptors, including agreeing aliases.

Every selected path must be canonical, repository-relative, and resolve to a
regular Git blob with mode `100644` or `100755`. Absolute, escaping,
noncanonical, missing, symlink, gitlink, tree, and unsupported object paths
fail. Every path and mode is validated before cached blob bytes are reused.

## Read-only and resource boundary

Only fixed Git plumbing is used: `rev-parse`, `cat-file`, and `ls-tree`.
Commands use an argument vector without a shell, a replacement environment
without inherited `GIT_*` routing, disabled replacement objects/lazy fetch and
optional locks, and disabled hooks/automatic maintenance. The helper does not
read or refresh the index, inspect working bytes, access the network, or invoke
builders, Gradle, signing, Kent, or project commands. It never prints blob
contents.

Limits are 4,096 descriptors and kind-map entries, 1,024 UTF-8 bytes per path,
32 MiB of selected blob contents, 8 MiB of output per Git command, 30 seconds
per Git command, and 60 seconds total. Any exceeded or uncertain bound fails;
there is no fallback to working-tree state.

## Result

Success writes one JSON object containing `schema`,
`source_bindings_valid: true`, `requested_ref`, `tree_oid`, optional
`commit_oid`, and descriptor `checked_count`. Errors exit nonzero and identify
the invalid schema/path/kind or, for a digest mismatch, the kind, path,
expected digest and actual digest.

The report deliberately contains no `ready`, runtime-attested,
activation-authorized, or full-admission claim. Existing release builders,
source-closure validators, graph checks, runtime checks, and effect gates
remain required.
