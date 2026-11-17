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
