# Mock-Backed Scenario Evidence Policy

Use this policy for all six supported work kinds: `feature`, `bugfix`,
`refactor`, `migration`, `dependency`, and `test`. Classify the final behavior
delta, not the issue label. A behavior-changing change cannot avoid applicable
mock-backed scenario evidence by selecting a different work kind.

## Inventory and applicability

Plan records one stable behavior ID for each changed behavior. Before verifier
handoff, Implement refreshes the inventory against the final source delta;
Fix refreshes it after each relevant fix. Every changed source-delta path must
be assigned to at least one behavior. Delivery tooling and other changes that
cannot meaningfully be exercised by the Android mock-backed UI harness may be
marked `not_applicable` only per behavior, with a concrete rationale and
appropriate alternative verification.

An `applicable` behavior records:

- `behavior_id`: stable, unique identifier.
- `delta_paths`: changed files belonging to the behavior.
- `source_paths`: changed implementation files for the behavior; may be empty
  for a fixture-only update.
- `scenario_paths`: changed test or fixture files that implement the scenario.
- `mock_seam`: the mock, fake, or fixture seam used by the scenario.
- `assertions`: non-empty descriptions of assertions tied to the behavior.
- `test_case_ids`: selected JUnit identifiers in `classname#method` form.

A `not_applicable` behavior records `behavior_id`, `delta_paths`,
`source_paths` (which may be empty), a concrete `rationale`, and a non-empty
`alternative_verification` list. Blanket exemptions, empty claims, or
unavailable test infrastructure are not non-applicability.

## Execution and verification

Run the selected harness outside the evidence helper. The helper never runs
commands from a packet, envelope, or log. It only captures bounded source and
JUnit result identities and validates the resulting evidence.

1. Before the selected run, call `capture-before` with a JSON object containing
   exactly `workspace_path`, `task_short_id`, `work_kind`, `baseline`,
   `results_dir`, `selected_case_ids`, and `output_path`. Use the immutable
   task baseline from Plan. `results_dir` must be a contained build directory;
   `output_path` must be new under `build/kent-workflow/<task>/`; create the
   task output directory first and use unique names because the helper refuses
   to overwrite an existing snapshot or receipt.
2. Run only the planned project test command. Preserve its actual exit code and
   bounded log under `build/kent-workflow/<task>/`.
3. Call `capture-after` with exactly `workspace_path`, `start_path`,
   `output_path`, `runner_exit_code`, and `log_path`. It writes a
   `mock-scenario-execution-v1` receipt containing pre/post source identities,
   pre/post JUnit-file identities, selected case results, runner result, and
   the verification-log digest.
4. Write the evidence packet under the same ignored task directory. Serialize
   a `mock-scenario-review-context-v1` envelope as `review_context`, retaining
   the prior narrative inside its non-empty `narrative` field. Immediately
   before verifier handoff, call `validate` with exactly `workspace_path` and
   that serialized `review_context`.

The helper captures the source delta relative to the supplied immutable
baseline, including changed file bytes and executable modes. It excludes
generated `build/` output and `.kent/runtime/`. Packet `source_delta` must
exactly match the current path/status/mode/content inventory and its
`source_sha256`. Evidence and receipt references include a workspace-relative
path and SHA-256. Result and evidence paths must stay inside the workspace;
symlinks, traversal, stale digests, unsupported fields, duplicate JSON keys,
or oversized inputs block verification.

The packet schema is `mock-scenario-evidence-v1` and contains exactly:
`schema`, `execution_id`, `task_short_id`, `work_kind`, `baseline`,
`source_delta`, `source_sha256`, `behaviors`, `verification`, and
`execution_receipt`. The helper creates a fresh `execution_id` during
`capture-before`; the packet and receipt must carry the same ID so a new packet
cannot silently reuse a receipt from another captured run.
`source_delta` contains `files` and `sha256`; each file record contains
`path`, `status`, `mode`, and `sha256`, sorted by path. Status is one of
`added|modified|deleted|type_changed`; deleted-file mode and digest are
`null`, while other modes are `100644` or `100755`. The source digest is
SHA-256 of canonical compact JSON for `{baseline, files, schema}`, with
schema `mock-scenario-source-delta-v1` and sorted object keys.

Applicable behavior objects contain exactly `behavior_id`, `applicability`,
`delta_paths`, `source_paths`, `scenario_paths`, `mock_seam`, `assertions`,
and `test_case_ids`. Non-applicable objects contain exactly `behavior_id`,
`applicability`, `delta_paths`, `source_paths`, `rationale`, and
`alternative_verification`.

`verification` contains the closed result enum `passed|failed|blocked`, an
inert descriptive `command`, and the contained `log_path` and `log_sha256`.
`execution_receipt` contains its contained `path` and `sha256`.

The `mock-scenario-review-context-v1` envelope contains exactly `schema`,
`evidence_path`, `evidence_sha256`, and `narrative`. The
`mock-scenario-execution-v1` receipt contains exactly `schema`, `execution_id`,
`task_short_id`, `work_kind`, `baseline`, `source_sha256_pre`,
`source_sha256_post`, `source_delta_sha256`, `start_snapshot`, `results_dir`,
`result_files_before`, `result_files_after`, `selected_case_ids`,
`selected_cases`, `runner_exit_code`, `verification_log`, and `result`.
Result-file inventory records contain `path`, `sha256`, and `size`.
Selected-case records contain `id`, `outcome`, and `path`; outcome is one of
`passed|failed|skipped|missing|stale`. The execution receipt binds the same
task, work kind, baseline, selected cases, pre/post source digest, result-file
identities, runner exit code, and log identity.

Applicable behaviors require a changed scenario or demonstrably consumed
fixture delta and actual successful JUnit results for every selected case.
Pre-existing unchanged XML, absent or skipped cases, source/result drift,
missing infrastructure, and invalid evidence are `blocked`. An observed test
failure is `failed`. Only complete, fresh, matching evidence can be `passed`.
Non-applicable work may use an empty selected-case list, but still requires a
successful alternative verification with a contained, digest-bound log.

The helper is structural enforcement, not semantic attestation. Reviewers must
inspect whether each assertion meaningfully covers its mapped behavior,
whether fixture-only changes are consumed by the selected case, and whether
each non-applicability rationale and alternative check is sound.
