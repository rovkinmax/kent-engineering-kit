# Runtime source capture

The eager byte collector and capture/binding APIs retain envelope-v1 semantics.
The managed runtime command protocol remains 2.2.0; that version does not
advertise streaming or v2 capability. Consumers explicitly qualify the exact Kit
and opt into the new API and integer `envelope_version=1` or `2`.

Use one `with source_read_budget(policy) as budget` across preflight and both
controller/target qualification. No-argument calls use finite legacy defaults;
nested calls reuse the same accounting/deadline. Only the identical active budget
may be passed explicitly inside that operation; another policy/budget fails.
Manifest, stdin and unverified environment values never enlarge capacity.

`verify_runtime_source_captures(root, selected_inputs, budget=budget)` resolves
every ordered descriptor at its exact commit, validates regular Git mode/path,
then streams a validated OID using raw `cat-file blob`, bounded chunks and exact
advertised size/EOF. No path becomes batch control syntax. A local OID cache
avoids duplicate payload reads but never descriptor/path/mode validation.
The operation budget caches only complete immutable blob facts, keyed by
canonical repository root and validated OID, including strict UTF-8 status.
Preflight/verification/H/T share these facts without caching native authority or
skipping independent descriptor path/type/mode checks. Git blob OID SHA-1 and
descriptor SHA-256 are checked incrementally before caching; a binary-only
observation cannot satisfy a later strict-text check.
The same operation caches root-qualified immutable commit/tree metadata.
Every descriptor still independently walks the exact tree components and checks
the selected leaf's type and mode. Tree metadata is OID-verified, charged against
metadata/record capacity and never used as native authority or a negative cache.
Only COMPLETE verification seals immutable, nonserializable observations.
Private tokens prevent API misuse, not hostile code already running in Python.

Pure runtime builders `capture_runtime_source_envelope_verified` and
`capture_runtime_authority_binding_verified` consume that proof, exact selected
inputs and explicit version. Runtime remains standalone and performs no I/O.
V2 retains v1's closed fields, canonical SHA-256 digest and ordered unique roots;
only the schema and semantic payload-size acceptance differ. Path/mode/OID
observations are private proof data, not extra envelope fields. Legacy readers
reject v2; there is no fallback. V1 streaming output matches eager output.

Defaults: 32 MiB physical stdout/stderr per operation, 60 s deadline, one child,
30 s command ceiling, 64 KiB chunks/stderr, 4 KiB size header, 4 MiB canonical
metadata, 4096 descriptor records and 8 MiB individually parsed documents.
Legacy bounded Git output remains 8 MiB. Raw streamed payload is not subject
to that materialized-output cap. Every repeated read/protocol byte is charged;
cache hits charge no payload I/O. Logical capture bytes count all aliases.
Metadata/count/deadline checks precede sealing. `SourceResourceBudgetExceeded`
is capacity exhaustion, not invalid provenance. Two sufficient policies produce
the same source identity.

The local Git CLI uses `--capture-runtime-envelope-from-git --envelope-version 2`
with an exact commit, no inline payload, one shared preflight/verification budget.
The old inline-base64 CLI and limits remain unchanged.

This capability does not enlarge source inspector, archive or report limits.
Existing cohorts and manual eager callers remain supported. No source package
authorizes installation, publication, aliases, receipts or live execution.

## Measured bounds

On macOS arm64/Python 3.14, four fixed descriptors with 1/4/16 MiB aggregate
payload used approximately 409 KB Python traced peak and 8.13 MB Git-child peak
RSS. Including full preflight and verification used approximately 531 KB Python
peak with approximately the same Git peak. Before operation caching, the 16 MiB
combined run read 33,582,239 physical bytes and required an explicitly trusted
64 MiB measurement policy. With verified object caching it reads 16,803,746 bytes.
The default 32 MiB capacity is unchanged; logical payload size is not capacity.

At 1/32/128 small descriptors, Python traced peak remains below 0.6 MB.
Canonical metadata, including bounded cached facts, grows with descriptors,
not with aggregate payload. These are fixture measurements, not universal RSS
guarantees; tests also enforce chunk, child concurrency and finite limits.

The independently qualified installed App receipt (SHA-256
8116c378d2d8c25b23faa66205284b46d6b41b38d0ba70611a230f2ebbce84f8)
contains 449 entries including local instructions, totaling 19,841,831 bytes:
63 entry slots and 13,712,601 aggregate bytes remain under the unchanged
inspector limits. Its largest file is 829,902 bytes (7,558,706 below the
8 MiB individual-file cap). This is a dated observation, not future headroom
or authority to expand inspector/archive limits.

The actual isolated Puber H/T qualification initially exhausted the unchanged
60 s operation budget after hundreds of Git proxy startups. With operation-local
blob/tree facts it completed in 23.38 s and 277 Git commands in an exclusive
probe. This is fixture evidence, not installed/runtime cohort qualification.
