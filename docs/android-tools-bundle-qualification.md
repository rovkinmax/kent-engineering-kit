# Android tools bundle qualification

`android-tools bundle qualify` is a bounded, read-only description of the
installed launcher and the coherent sibling bundle it loaded. It requires no
configuration, device, lease, Task, or provider. It creates no invocation
record and writes no local evidence.

The invocation accepts no options or operands:

```sh
android-tools bundle qualify
```

Success writes exactly one JSON object:

```json
{
  "schema": "android-tools-bundle-qualification-v1",
  "installation_id": "<sha256>",
  "contracts": {
    "custody_api": 1,
    "custody_marker": 1,
    "device_launch": 1,
    "native_result": 1,
    "run_manifest": "android-tools-run-v1"
  },
  "custody": {
    "path": "/absolute/path/android_tools_custody.py",
    "size_bytes": 23503,
    "sha256": "<sha256>"
  }
}
```

`size_bytes` is the actual qualified file size, not a fixed compatibility
value. The object has no arbitrary capability registry, provider inventory,
configuration, or invocation-specific data. Unsupported options and operations
fail with a bounded categorical error and a nonzero exit status; they never
emit a success descriptor.

The existing loader reads each bounded sibling as an owned, regular,
non-symlink file, rejects group/world-writable bytes, and checks descriptor and
path identity plus size, mode, modification time, and change time around the
read. It verifies the existing exact bundle hashes and protocol/API contracts,
then executes those same verified in-memory bytes. Qualification returns
custody metadata from that loader read and additionally requires custody API
1 and marker protocol 1.

`installation_id` is SHA-256 over UTF-8 canonical JSON with sorted keys and
compact separators:

```json
{
  "launcher_sha256": "<actual launcher sha256>",
  "bundle_sha256s": {
    "android_tools_custody.py": "<verified sha256>",
    "android_tools_evidence.py": "<verified sha256>",
    "android_tools_native.py": "<verified sha256>"
  }
}
```

This identifier describes the bytes read for this invocation. It is not a
signature, self-authentication, trust root, semantic version, or accepted-set
check. Consumers must validate the canonical trusted entrypoint, fixed
contracts, and actual dependencies they use. The facade's existing bundle
integrity checks remain in force for every operation.

The fixed contracts mean:

- `custody_api` 1 and `custody_marker` 1 preserve the documented public custody
  operations and uncertainty marker protocol.
- `device_launch` 1 preserves launch transport/effect separation and
  owner-intent assurance; it does not independently attest the launched
  package.
- `native_result` 1 preserves transport/effect and unknown-outcome semantics.
- `run_manifest` identifies the existing `android-tools-run-v1` manifest.

Changes to these meanings require a new reviewed contract version. Compatible
changes to unrelated facade, native, evidence, or provider implementation
bytes do not change these fixed contracts. The owner remains responsible for
maintaining the published meanings and regression tests.

## Workflow evidence export

The workflow evidence interface accepts the ordinary-R2 binding fields and a
`native_task_id` supplied in the existing private `--binding-file`. The
launcher passes that object unchanged to the evidence module. Binding
production remains the caller's responsibility: use the native Task ID from
the current Kent-assigned Workflow metadata; do not infer it from an R2 task
key or UID, or from an injected environment variable. The installed ordinary
R2 helper validates the inherited Session against this locator on every
resolve, attachment read, and attachment write. An absent or mismatched
locator blocks export.

Workflow exports use only the exact private helper
`~/.kent/ordinary-r2/bin/kent-project-memory`; arbitrary aliases, symlinks,
and same-named helpers are rejected. The native Task ID is included in the
private local binding digest, but not in the exported evidence envelope or R2
Task identity.

An `exported` result requires a valid write receipt and byte-exact readback for
each newly written object. On recovery from a pending or unknown attempt, or
when consulting a cached exported result, the exporter reads each object
first. Exact bytes at the expected Task path establish durability and avoid a
duplicate write. A failed or mismatched read is not proof that an object is
absent; a same-content immutable write retry is allowed only with the same
Task locator validated for that helper call, and still requires exact
readback. Export and recovery never repeat an Android/device action.

Updating the launcher checksum and evidence module changes the bundle's
`installation_id` and qualification output. Appsome consumers that retain
immutable `qualification_raw` therefore need a separately authorized,
owner-led installation or rollback procedure: first settle every active or
prepared context that may depend on the outgoing qualification; switch the
launcher and evidence module as a pair; and, for rollback, restore both
captured old files and verify the exact old qualification bytes. Historical
qualification records remain immutable. Source changes and source approval
alone do not authorize installation, activation, or a Kent restart.
