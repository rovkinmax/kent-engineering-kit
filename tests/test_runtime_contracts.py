from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import tracemalloc
from types import MappingProxyType
import unittest
from unittest import mock

import workflowkit.runtime as runtime_module
from workflowkit.runtime import (
    RuntimeContractError,
    append_ci_report_attempt,
    build_ci_report,
    build_terminal_seal_record,
    canonical_bytes,
    canonical_sha256,
    check_state_sha256,
    classify_ci_report,
    classify_expected_ci_checks,
    classify_expected_ci_checks_with_receipt,
    classify_pr_feedback,
    classify_terminal_state,
    classify_verification_report,
    discarded_attempt_digest,
    expected_ci_checks_sha256,
    make_pr_feedback_cursor,
    make_observation_hard_limit_attempt,
    make_observation_limit_attempt,
    make_report_invalid_attempt,
    parse_runtime_external_captures,
    parse_terminal_marker_line,
    prepare_ci_attempt,
    terminal_marker_line,
    validate_ci_report,
    validate_cleanup_report,
    validate_ci_report_history,
    validate_expected_ci_checks,
    validate_pr_feedback_cursor,
    validate_pr_feedback_item,
    validate_runtime_source_envelope,
    validate_terminal_marker,
    validate_terminal_seal_request,
    validate_verification_report,
)


ZERO_SHA256 = "0" * 64
ZERO_SHA1 = "0" * 40


def expected_checks() -> dict[str, object]:
    return {
        "schema": "github-ci-expected-checks-v1",
        "repository": "owner/repository",
        "project_commit": ZERO_SHA1,
        "runtime_source_envelope_digest": ZERO_SHA256,
        "checks": [
            {
                "workflow_name": "Pull Request",
                "check_name": "unit",
                "allow_skipped": False,
            }
        ],
    }


def observed_check(
    *,
    bucket: str = "pass",
    state: str = "SUCCESS",
) -> dict[str, object]:
    return {
        "workflow_name": "Pull Request",
        "check_name": "unit",
        "bucket": bucket,
        "state": state,
        "link": "https://github.com/owner/repository/actions/runs/1",
    }


def ci_attempt(
    *,
    sequence: int = 1,
    reason: str = "all_expected_checks_terminal_green",
) -> dict[str, object]:
    return {
        "sequence": sequence,
        "head_oid": ZERO_SHA1,
        "base_oid": ZERO_SHA1,
        "reason": reason,
        "watcher_exit_code": 0,
        "expected_checks": [observed_check()],
        "unexpected_check_count": 0,
        "unexpected_checks_sha256": ZERO_SHA256,
        "retry": None,
        "safe_error": None,
    }


def ci_report() -> dict[str, object]:
    return {
        "schema": "github-ci-report-v2",
        "mode": "expected-v1",
        "repository": "owner/repository",
        "pull_number": 1,
        "runtime_source_envelope_digest": ZERO_SHA256,
        "expected_ci_checks_sha256": expected_ci_checks_sha256(
            expected_checks()
        ),
        "discarded_attempt_count": 0,
        "discarded_attempts_sha256": ZERO_SHA256,
        "attempts": [ci_attempt()],
    }


class RuntimeContractTest(unittest.TestCase):
    def test_canonical_json_is_compact_sorted_and_digestable(self) -> None:
        ordinary = {"z": 1, "a": "é"}
        self.assertEqual(
            canonical_bytes(ordinary),
            json.dumps(
                ordinary,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8"),
        )
        self.assertEqual(
            canonical_sha256(ordinary),
            "fb64e573f7cde5b7efeda52ffc4bdd57572055b0b7e64a70172606c82c6c7eac",
        )
        with self.assertRaises(RuntimeContractError):
            canonical_bytes(MappingProxyType({"a": 1}))
        with self.assertRaises(RuntimeContractError):
            canonical_bytes(float("nan"))

    def test_canonical_json_unicode_control_and_surrogate_parity(self) -> None:
        values = [
            "",
            "é😀",
            "\x00\x01\x08\t\n\x0b\x0c\r\x1f",
            '"\\',
            "\u2028",
            {"é": ["😀", "\n", "\x1f"]},
        ]
        for value in values:
            with self.subTest(value=value):
                expected = json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                self.assertEqual(canonical_bytes(value), expected)
        for value in ("\ud800", "\ud83d\ude00", {"bad": "\ud800"}):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeContractError):
                    canonical_bytes(value)

    def test_bounded_canonical_encoder_streams_large_string_tokens(self) -> None:
        limit = runtime_module.MAX_OBSERVATION_CANONICAL_BYTES + 1
        opening = b'[{"payload":"'
        rows = [{"payload": "x" * (16 * 1024 * 1024)}]
        update_sizes: list[int] = []
        real_sha256 = runtime_module.hashlib.sha256

        class TrackingDigest:
            def __init__(self) -> None:
                self._digest = real_sha256()

            def update(self, value: bytes) -> None:
                update_sizes.append(len(value))
                self._digest.update(value)

            def hexdigest(self) -> str:
                return self._digest.hexdigest()

        tracemalloc.start()
        try:
            with mock.patch.object(
                runtime_module.hashlib,
                "sha256",
                side_effect=TrackingDigest,
            ):
                receipt = runtime_module._bounded_canonical_observation(
                    rows,
                    "projected_rows",
                )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        expected_prefix = opening + b"x" * (limit - len(opening))
        self.assertIsInstance(
            receipt,
            runtime_module.RejectedObservationHardLimit,
        )
        self.assertEqual(
            receipt.prefix_sha256,
            hashlib.sha256(expected_prefix).hexdigest(),
        )
        self.assertTrue(update_sizes)
        self.assertLessEqual(max(update_sizes), 4096)
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_canonical_json_nesting_limit_and_recursion_translation(self) -> None:
        def nested(depth: int, form: str) -> object:
            value: object = 0
            for index in range(depth):
                if form == "list":
                    value = [value]
                elif form == "tuple":
                    value = (value,)
                elif form == "object":
                    value = {"value": value}
                else:
                    value = [value] if index % 2 == 0 else {"value": value}
            return value

        for form in ("list", "tuple", "object", "mixed"):
            with self.subTest(form=form):
                value = nested(
                    runtime_module.MAX_CANONICAL_JSON_NESTING,
                    form,
                )
                expected = json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                self.assertEqual(canonical_bytes(value), expected)
                self.assertEqual(
                    canonical_sha256(value),
                    hashlib.sha256(expected).hexdigest(),
                )
                with self.assertRaises(RuntimeContractError):
                    canonical_bytes(nested(101, form))

        raw_limit = b"[" * 100 + b"0" + b"]" * 100
        self.assertEqual(
            runtime_module.parse_canonical_json(
                raw_limit,
                label="nested value",
                max_bytes=runtime_module.MAX_FEEDBACK_BYTES,
            ),
            nested(100, "list"),
        )
        for value in (nested(1000, "list"),):
            with self.subTest(raw=False):
                with self.assertRaises(RuntimeContractError) as failure:
                    canonical_bytes(value)
                self.assertNotIn("RecursionError", str(failure.exception))
        raw_over_depth = b"[" * 1000 + b"0" + b"]" * 1000
        with self.assertRaises(RuntimeContractError) as failure:
            runtime_module.parse_canonical_json(
                raw_over_depth,
                label="nested value",
                max_bytes=runtime_module.MAX_FEEDBACK_BYTES,
            )
        self.assertNotIn("RecursionError", str(failure.exception))
        with self.assertRaises(RuntimeContractError):
            runtime_module._bounded_canonical_observation(
                [{"nested": nested(101, "list")}],
                "projected_rows",
            )

        with mock.patch.object(
            runtime_module.json,
            "loads",
            side_effect=RecursionError("interpreter detail"),
        ):
            with self.assertRaises(RuntimeContractError) as failure:
                runtime_module.parse_canonical_json(b"0", label="fixture")
        self.assertEqual(
            str(failure.exception),
            "fixture exceeds the canonical JSON nesting limit",
        )

        with mock.patch.object(
            runtime_module,
            "_iter_canonical_fragments",
            side_effect=RecursionError("interpreter detail"),
        ):
            with self.assertRaises(RuntimeContractError) as failure:
                canonical_bytes(0)
        self.assertEqual(
            str(failure.exception),
            "canonical JSON nesting exceeds its limit",
        )

        with mock.patch.object(
            runtime_module,
            "_canonical_byte_chunks",
            side_effect=RecursionError("interpreter detail"),
        ):
            with self.assertRaises(RuntimeContractError) as failure:
                runtime_module._bounded_canonical_observation(
                    [{"value": 1}],
                    "projected_rows",
                )
        self.assertEqual(
            str(failure.exception),
            "canonical JSON nesting exceeds its limit",
        )

    def test_standalone_import_has_no_package_dependency(self) -> None:
        path = Path(__file__).resolve().parents[1] / "workflowkit" / "runtime.py"
        spec = importlib.util.spec_from_file_location("standalone_runtime", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.canonical_bytes({"b": 2, "a": 1}), b'{"a":1,"b":2}')
        self.assertNotIn("workflowkit", module.__dict__.get("__package__", ""))

    def test_capture_stdin_schema_is_strict_and_canonical_base64(self) -> None:
        encoded = base64.b64encode(b"role bytes").decode("ascii")
        raw = json.dumps(
            {
                "schema": "runtime-external-captures-v1",
                "roots": [
                    {
                        "kind": "effective-role",
                        "key": "release-manager",
                        "contents_base64": encoded,
                    }
                ],
            },
            separators=(",", ":"),
        )
        self.assertEqual(
            parse_runtime_external_captures(raw),
            (("effective-role", "release-manager", b"role bytes"),),
        )
        for bad in (
            raw + "{}",
            raw + "\n",
            raw.replace(
                '"schema":"runtime-external-captures-v1"',
                '"schema":"runtime-external-captures-v1","schema":"runtime-external-captures-v1"',
            ),
            raw.replace(encoded, encoded.rstrip("=")),
            raw.replace('"roots"', '"extra"'),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(RuntimeContractError):
                    parse_runtime_external_captures(bad)

    def test_runtime_envelope_total_root_limit_is_enforced(self) -> None:
        envelope = {
            "schema": "runtime-source-envelope-v1",
            "project_name": "project",
            "repository": "owner/repository",
            "topology_kind": "primary",
            "project_commit": ZERO_SHA1,
            "source_preview_sha256": ZERO_SHA256,
            "artifact_digests": {
                "spec_raw_blob_sha256": ZERO_SHA256,
                "source_manifest_raw_blob_sha256": ZERO_SHA256,
                "snapshot_raw_blob_sha256": ZERO_SHA256,
            },
            "external_roots": [
                {
                    "kind": f"root-{index}",
                    "key": "key",
                    "byte_count": 1024 * 1024,
                    "sha256": ZERO_SHA256,
                }
                for index in range(5)
            ],
        }
        with self.assertRaises(RuntimeContractError):
            validate_runtime_source_envelope(envelope)

    def test_terminal_classifier_models_checkpoint_phases_and_conflicts(self) -> None:
        self.assertEqual(
            classify_terminal_state(
                active=False,
                tombstone=True,
                sentinel=True,
                ledger_valid=False,
                marker_valid=True,
            ),
            "resume_tombstone_cleanup",
        )
        self.assertEqual(
            classify_terminal_state(
                active=False,
                tombstone=True,
                sentinel=True,
                ledger_valid=False,
                marker_valid=True,
                tombstone_entries=("fix-checkpoint.json",),
            ),
            "blocked_checkpoint_after_ledger_loss",
        )
        self.assertEqual(
            classify_terminal_state(
                active=False,
                tombstone=True,
                sentinel=False,
                ledger_valid=True,
                marker_valid=True,
                tombstone_entries=("fix-checkpoint.json", "smoke-checkpoint.json"),
            ),
            "pre_sentinel_recovery",
        )
        self.assertEqual(
            classify_terminal_state(
                active=False,
                tombstone=True,
                sentinel=True,
                ledger_valid=True,
                marker_valid=True,
                tombstone_entries=("unknown",),
            ),
            "blocked_unknown_tombstone_entry",
        )

    def test_terminal_request_marker_and_cleanup_line_are_closed(self) -> None:
        request = {
            "schema": "terminal-evidence-seal-request-v1",
            "operation_report_digests": [
                {"kind": "approval", "sha256": ZERO_SHA256}
            ],
            "redaction": {
                "status": "passed",
                "report_sha256": ZERO_SHA256,
            },
            "retention_class": "cleanup_report_only",
        }
        self.assertEqual(validate_terminal_seal_request(request), request)
        seal = {
            "schema": "terminal_evidence_v1",
            "task_short_id": "TASK-1",
            "event_count": 2,
            "final_hash": ZERO_SHA256,
            "operation_report_digests": request["operation_report_digests"],
            "redaction": request["redaction"],
            "retention_class": "cleanup_report_only",
        }
        self.assertEqual(validate_terminal_marker(seal), seal)
        line = terminal_marker_line(seal)
        self.assertEqual(parse_terminal_marker_line(line), seal)
        self.assertEqual(validate_cleanup_report("retry\n" + line), seal)
        with self.assertRaises(RuntimeContractError):
            validate_terminal_seal_request(
                {**request, "retention_class": "retain"}
            )
        with self.assertRaises(RuntimeContractError):
            validate_terminal_marker({**seal, "unknown": True})

    def test_terminal_chain_requires_an_ordinary_event_before_seal(self) -> None:
        ordinary = {
            "schema_version": 1,
            "sequence": 1,
            "task_short_id": "TASK-1",
            "node_key": "verify",
            "previous_hash": "",
        }
        ordinary["event_hash"] = canonical_sha256(ordinary)
        seal = build_terminal_seal_record(
            {
                "schema": "terminal-evidence-seal-request-v1",
                "operation_report_digests": [],
                "redaction": {
                    "status": "passed",
                    "report_sha256": ZERO_SHA256,
                },
                "retention_class": "cleanup_report_only",
            },
            sequence=2,
            task_short_id="TASK-1",
            previous_hash=ordinary["event_hash"],
        )
        from workflowkit.runtime import validate_terminal_chain

        marker = validate_terminal_chain([ordinary, seal], task_short_id="TASK-1")
        self.assertEqual(marker["event_count"], 2)
        with self.assertRaises(RuntimeContractError):
            validate_terminal_chain([seal], task_short_id="TASK-1")

    def test_verification_report_uses_only_safe_codes_and_mapping(self) -> None:
        report = {
            "schema": "workflow-verification-report-v2",
            "code": "passed",
            "log_path": f"build/kent-workflow/verification-report-{ZERO_SHA256}.log",
            "log_sha256": ZERO_SHA256,
            "exit_code": 0,
        }
        self.assertEqual(validate_verification_report(report), report)
        self.assertEqual(classify_verification_report(report), "passed")
        failed = {**report, "code": "child_exit_nonzero", "exit_code": 1}
        self.assertEqual(
            classify_verification_report(failed),
            "needs_changes",
        )
        with self.assertRaises(RuntimeContractError):
            validate_verification_report({**report, "child_output": "secret"})

    def test_closed_runtime_schemas_reject_unknown_fields(self) -> None:
        item = {
            "kind": "issue_comment",
            "id": "1",
            "author_login": None,
            "created_at": "2026-08-20T10:00:00Z",
            "updated_at": "2026-08-20T10:00:01Z",
            "body_bytes": 0,
            "body_sha256": ZERO_SHA256,
        }
        cursor = make_pr_feedback_cursor(
            repository="owner/repository",
            pull_number=1,
            head_oid=ZERO_SHA1,
            base_oid=ZERO_SHA1,
            pr_state="OPEN",
            review_decision="",
            merge_state_status="CLEAN",
            checks=[],
            items=[item],
        )
        expected = expected_checks()
        report = ci_report()
        verification = {
            "schema": "workflow-verification-report-v2",
            "code": "verification_failed",
            "log_path": None,
            "log_sha256": None,
            "exit_code": 1,
        }
        values = (
            (validate_pr_feedback_cursor, cursor),
            (validate_expected_ci_checks, expected),
            (validate_ci_report, report),
            (validate_verification_report, verification),
        )
        for validator, value in values:
            with self.subTest(validator=validator.__name__):
                with self.assertRaises(RuntimeContractError):
                    validator({**value, "unknown": True})

    def test_feedback_items_cursor_modes_and_change_classifier(self) -> None:
        item = {
            "kind": "issue_comment",
            "id": "1",
            "author_login": "reviewer",
            "created_at": "2026-08-20T10:00:00Z",
            "updated_at": "2026-08-20T10:00:01Z",
            "body_bytes": 4,
            "body_sha256": ZERO_SHA256,
        }
        self.assertEqual(validate_pr_feedback_item(item), item)
        cursor = make_pr_feedback_cursor(
            repository="owner/repository",
            pull_number=1,
            head_oid=ZERO_SHA1,
            base_oid=ZERO_SHA1,
            pr_state="OPEN",
            review_decision="",
            merge_state_status="CLEAN",
            checks=[],
            items=[item],
        )
        self.assertIsInstance(cursor, dict)
        self.assertEqual(
            classify_pr_feedback("uninitialized", cursor)["transition"],
            "state_changed",
        )
        self.assertEqual(
            classify_pr_feedback(cursor, cursor)["transition"],
            "still_waiting",
        )
        with self.assertRaises(RuntimeContractError):
            validate_pr_feedback_cursor({**cursor, "items": [item, item]})
        oversized_thread = {
            "kind": "review_thread",
            "id": "thread-1",
            "resolved": False,
            "outdated": False,
            "path": "src/main.py",
            "current_line": None,
            "current_start_line": None,
            "original_line": 1,
            "original_start_line": None,
            "subject_type": "LINE",
            "comment_ids": [
                f"{index:03d}-" + "x" * 250 for index in range(300)
            ],
        }
        digest_only = make_pr_feedback_cursor(
            repository="owner/repository",
            pull_number=1,
            head_oid=ZERO_SHA1,
            base_oid=ZERO_SHA1,
            pr_state="OPEN",
            review_decision="",
            merge_state_status="CLEAN",
            checks=[],
            items=[oversized_thread],
        )
        self.assertEqual(digest_only["mode"], "digest_only")
        self.assertLessEqual(digest_only["item_count"], 100)
        with self.assertRaises(RuntimeContractError):
            validate_pr_feedback_cursor({**cursor, "check_count": 1001})
        with self.assertRaises(RuntimeContractError):
            validate_pr_feedback_cursor({**cursor, "unknown": True})

    def test_feedback_item_variants_nullable_fields_and_boundaries(self) -> None:
        review = {
            "kind": "review",
            "id": "review-1",
            "author_login": None,
            "state": "COMMENTED",
            "submitted_at": None,
            "updated_at": "2026-08-20T10:00:00Z",
            "commit_oid": None,
            "body_bytes": 0,
            "body_sha256": ZERO_SHA256,
        }
        thread = {
            "kind": "review_thread",
            "id": "thread-1",
            "resolved": False,
            "outdated": True,
            "path": "src/main.py",
            "current_line": None,
            "current_start_line": None,
            "original_line": 1,
            "original_start_line": None,
            "subject_type": "FILE",
            "comment_ids": ["comment-1"],
        }
        review_comment = {
            "kind": "review_comment",
            "id": "comment-1",
            "thread_id": "thread-1",
            "author_login": None,
            "created_at": "2026-08-20T10:00:00Z",
            "updated_at": "2026-08-20T10:00:01Z",
            "current_commit_oid": None,
            "original_commit_oid": None,
            "body_bytes": 0,
            "body_sha256": ZERO_SHA256,
        }
        for item in (review, thread, review_comment):
            self.assertEqual(validate_pr_feedback_item(item), item)
        self.assertEqual(
            validate_pr_feedback_item(
                {
                    **thread,
                    "comment_ids": [
                        f"comment-{index:04d}" for index in range(1000)
                    ],
                }
            )["comment_ids"][-1],
            "comment-0999",
        )
        invalid = (
            ({**review, "id": "x" * 257},),
            ({**review, "author_login": "x" * 101},),
            ({**thread, "path": "x" * 1025},),
            ({**thread, "subject_type": "x" * 65},),
            ({**thread, "current_line": 0},),
            ({**thread, "comment_ids": ["x" * 257]},),
            ({**thread, "comment_ids": ["comment-1"] * 1001},),
            ({**review, "updated_at": "not-a-timestamp"},),
            ({**review, "state": "commented"},),
            ({**review_comment, "current_commit_oid": "x" * 40},),
            ({**review_comment, "body_bytes": -1},),
            ({**review, "unknown": True},),
        )
        for (value,) in invalid:
            with self.subTest(value=value):
                with self.assertRaises(RuntimeContractError):
                    validate_pr_feedback_item(value)
        with self.assertRaises(RuntimeContractError):
            validate_pr_feedback_item(
                {**thread, "comment_ids": ["comment-2", "comment-1"]}
            )
        with self.assertRaises(RuntimeContractError):
            validate_pr_feedback_item(
                {**thread, "comment_ids": ["comment-1", "comment-1"]}
            )

    def test_check_state_requires_unambiguous_identity_and_state(self) -> None:
        self.assertEqual(
            check_state_sha256(
                [
                    {"context": "build", "state": "SUCCESS"},
                    {"name": "unit", "status": "SUCCESS"},
                ]
            ),
            check_state_sha256(
                [
                    {"name": "unit", "status": "SUCCESS"},
                    {"context": "build", "state": "SUCCESS"},
                ]
            ),
        )
        for row in (
            {"name": "a", "context": "b", "status": "SUCCESS"},
            {"name": "a", "status": "SUCCESS", "state": "SUCCESS"},
            {"name": None, "context": None, "status": "SUCCESS"},
            {"name": "a", "status": None, "state": None},
        ):
            with self.subTest(row=row):
                with self.assertRaises(RuntimeContractError):
                    check_state_sha256([row])
        self.assertIsInstance(
            check_state_sha256(
                [
                    {"name": f"check-{index}", "status": "SUCCESS"}
                    for index in range(1000)
                ]
            ),
            str,
        )
        with self.assertRaises(RuntimeContractError):
            check_state_sha256(
                [
                    {"name": f"check-{index}", "status": "SUCCESS"}
                    for index in range(1001)
                ]
            )

    def test_expected_checks_require_identity_digest_and_current_head(self) -> None:
        expected = expected_checks()
        self.assertEqual(validate_expected_ci_checks(expected), expected)
        digest = expected_ci_checks_sha256(expected)
        result = classify_expected_ci_checks(
            expected,
            [observed_check()],
            current_repository="owner/repository",
            current_head_oid=ZERO_SHA1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_checks_digest=digest,
        )
        self.assertEqual(result["transition"], "all_expected_checks_terminal_green")
        self.assertEqual(result["expected_checks"], [observed_check()])
        self.assertEqual(result["unexpected_check_count"], 0)
        self.assertEqual(
            result["unexpected_checks_sha256"],
            canonical_sha256([]),
        )
        stale = classify_expected_ci_checks(
            expected,
            [observed_check()],
            current_repository="owner/repository",
            current_head_oid="1" * 40,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_checks_digest=digest,
        )
        self.assertEqual(stale["transition"], "source_changed")
        self.assertEqual(stale["unexpected_check_count"], 0)
        self.assertEqual(stale["expected_checks"], [])
        malformed_observation = [{"not": "a check"}]
        self.assertEqual(
            classify_expected_ci_checks(
                expected,
                malformed_observation,
                current_repository="owner/repository",
                current_head_oid="1" * 40,
                runtime_source_envelope_digest=ZERO_SHA256,
                expected_checks_digest=digest,
            )["transition"],
            "source_changed",
        )
        with self.assertRaises(RuntimeContractError):
            validate_expected_ci_checks(
                {**expected, "checks": [expected["checks"][0], expected["checks"][0]]}
            )
        repository_mismatch = classify_expected_ci_checks(
            expected,
            malformed_observation,
            current_repository="other/repository",
            current_head_oid=ZERO_SHA1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_checks_digest=digest,
        )
        self.assertEqual(repository_mismatch["transition"], "expected_contract_invalid")
        self.assertEqual(repository_mismatch["expected_checks"], [])
        for field in ("runtime_source_envelope_digest", "expected_checks_digest"):
            arguments = {
                "current_repository": "owner/repository",
                "current_head_oid": ZERO_SHA1,
                "runtime_source_envelope_digest": ZERO_SHA256,
                "expected_checks_digest": digest,
            }
            arguments[field] = "1" * 64
            mismatch = classify_expected_ci_checks(
                expected,
                malformed_observation,
                **arguments,
            )
            self.assertEqual(mismatch["transition"], "expected_contract_invalid")
            self.assertEqual(mismatch["expected_checks"], [])
            self.assertEqual(mismatch["unexpected_check_count"], 0)
        duplicate_over_limit = classify_expected_ci_checks(
            expected,
            [observed_check()] * 101,
            current_repository="owner/repository",
            current_head_oid=ZERO_SHA1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_checks_digest=digest,
        )
        self.assertEqual(
            duplicate_over_limit["transition"],
            "duplicate_observed_check",
        )
        unexpected = {**observed_check(), "check_name": "lint"}
        mixed = classify_expected_ci_checks(
            expected,
            [unexpected, observed_check()],
            current_repository="owner/repository",
            current_head_oid=ZERO_SHA1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_checks_digest=digest,
        )
        self.assertEqual(mixed["transition"], "all_expected_checks_terminal_green")
        self.assertEqual(mixed["unexpected_check_count"], 1)
        self.assertEqual(
            mixed["unexpected_checks_sha256"],
            canonical_sha256([unexpected]),
        )
        self.assertEqual(
            classify_expected_ci_checks(
                expected,
                [unexpected],
                current_repository="owner/repository",
                current_head_oid=ZERO_SHA1,
                runtime_source_envelope_digest=ZERO_SHA256,
                expected_checks_digest=digest,
            )["transition"],
            "expected_check_missing",
        )
        duplicate = classify_expected_ci_checks(
            expected,
            [observed_check(), observed_check()],
            current_repository="owner/repository",
            current_head_oid=ZERO_SHA1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_checks_digest=digest,
        )
        self.assertEqual(duplicate["transition"], "duplicate_observed_check")
        self.assertEqual(len(duplicate["expected_checks"]), 2)
        for bucket, transition in (
            ("fail", "expected_check_failed"),
            ("cancel", "expected_check_failed"),
            ("pending", "pending_limit"),
            ("skipping", "expected_check_skipped"),
        ):
            self.assertEqual(
                classify_expected_ci_checks(
                    expected,
                    [observed_check(bucket=bucket)],
                    current_repository="owner/repository",
                    current_head_oid=ZERO_SHA1,
                    runtime_source_envelope_digest=ZERO_SHA256,
                    expected_checks_digest=digest,
                )["transition"],
                transition,
            )
        allowed_skip = {
            **expected,
            "checks": [{**expected["checks"][0], "allow_skipped": True}],
        }
        self.assertEqual(
            classify_expected_ci_checks(
                allowed_skip,
                [observed_check(bucket="skipping")],
                current_repository="owner/repository",
                current_head_oid=ZERO_SHA1,
                runtime_source_envelope_digest=ZERO_SHA256,
                expected_checks_digest=expected_ci_checks_sha256(allowed_skip),
            )["transition"],
            "all_expected_checks_terminal_green",
        )
        empty = classify_expected_ci_checks(
            expected,
            [],
            current_repository="owner/repository",
            current_head_oid=ZERO_SHA1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_checks_digest=digest,
        )
        self.assertEqual(empty["transition"], "no_checks_reported")
        too_many_unexpected = [
            {**observed_check(), "check_name": f"other-{index:05d}"}
            for index in range(10001)
        ]
        limited = classify_expected_ci_checks(
            expected,
            too_many_unexpected,
            current_repository="owner/repository",
            current_head_oid=ZERO_SHA1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_checks_digest=digest,
        )
        self.assertEqual(limited["transition"], "report_invalid")
        self.assertEqual(limited["reason"], "observation_limit")
        self.assertEqual(limited["unexpected_check_count"], 10001)
        checks_100 = [
            {
                "workflow_name": "W",
                "check_name": f"check-{index:03d}",
                "allow_skipped": False,
            }
            for index in range(100)
        ]
        self.assertEqual(
            len(
                validate_expected_ci_checks(
                    {**expected, "checks": checks_100}
                )["checks"]
            ),
            100,
        )
        with self.assertRaises(RuntimeContractError):
            validate_expected_ci_checks(
                {
                    **expected,
                    "checks": [
                        *checks_100,
                        {
                            "workflow_name": "W",
                            "check_name": "check-100",
                            "allow_skipped": False,
                        },
                    ],
                }
            )

    def test_expected_ci_receipts_authority_grammar_order_and_permutation(self) -> None:
        expected = expected_checks()
        digest = expected_ci_checks_sha256(expected)
        malformed = [{"workflow_name": "bad"}]
        stale = classify_expected_ci_checks_with_receipt(
            expected,
            malformed,
            current_repository="owner/repository",
            current_head_oid="1" * 40,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_checks_digest=digest,
        )
        self.assertEqual(stale.state, "ordinary")
        self.assertIsNone(stale.projected_observations)
        self.assertIsNone(stale.unexpected_observations)
        self.assertEqual(stale.value["transition"], "source_changed")
        invalid = classify_expected_ci_checks_with_receipt(
            expected,
            malformed,
            current_repository="owner/repository",
            current_head_oid=ZERO_SHA1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_checks_digest=digest,
        )
        self.assertEqual(invalid.state, "grammar_invalid")
        self.assertIsNone(invalid.value)
        self.assertIsNotNone(invalid.grammar_error)
        self.assertEqual(invalid.projected_observations.source, "projected_rows")
        projected = invalid.projected_observations
        unexpected = runtime_module.RejectedObservationReceipt(
            "unexpected_rows",
            1,
            ZERO_SHA256,
        )
        with self.assertRaises(RuntimeContractError):
            runtime_module.ExpectedCiClassification(
                "grammar_invalid",
                None,
                runtime_module.RejectedObservationReceipt(
                    "unexpected_rows",
                    projected.count,
                    projected.sha256,
                ),
                None,
                invalid.grammar_error,
            )
        with self.assertRaises(RuntimeContractError):
            runtime_module.ExpectedCiClassification(
                "observation_limit",
                {},
                projected,
                projected,
            )
        with self.assertRaises(RuntimeContractError):
            runtime_module.ExpectedCiClassification(
                "hard_limit",
                None,
                projected,
                unexpected,
            )
        with self.assertRaises(RuntimeContractError):
            classify_expected_ci_checks(
                expected,
                malformed,
                current_repository="owner/repository",
                current_head_oid=ZERO_SHA1,
                runtime_source_envelope_digest=ZERO_SHA256,
                expected_checks_digest=digest,
            )
        unexpected_a = {**observed_check(), "check_name": "a"}
        unexpected_b = {**observed_check(), "check_name": "b"}
        first = classify_expected_ci_checks_with_receipt(
            expected,
            [unexpected_b, observed_check(), unexpected_a],
            current_repository="owner/repository",
            current_head_oid=ZERO_SHA1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_checks_digest=digest,
        )
        second = classify_expected_ci_checks_with_receipt(
            expected,
            [unexpected_a, unexpected_b, observed_check()],
            current_repository="owner/repository",
            current_head_oid=ZERO_SHA1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_checks_digest=digest,
        )
        self.assertEqual(
            first.value["unexpected_checks_sha256"],
            second.value["unexpected_checks_sha256"],
        )
        over_limit = [
            {**observed_check(), "check_name": f"unexpected-{index}"}
            for index in range(10000)
        ]
        over_limit.insert(5000, {**observed_check(), "check_name": "unexpected-0"})
        limited = classify_expected_ci_checks_with_receipt(
            expected,
            [observed_check(), *over_limit],
            current_repository="owner/repository",
            current_head_oid=ZERO_SHA1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_checks_digest=digest,
        )
        self.assertEqual(limited.state, "observation_limit")
        self.assertIsNotNone(limited.value)
        self.assertEqual(limited.projected_observations.source, "projected_rows")
        self.assertEqual(limited.unexpected_observations.count, 10001)

    def test_observation_encoder_and_typed_attempt_boundaries(self) -> None:
        expected = expected_checks()
        limit = runtime_module.MAX_OBSERVATION_CANONICAL_BYTES
        prefix = len(canonical_bytes([{"x": ""}]))
        exact_rows = [{"x": "a" * (limit - prefix)}]
        exact = runtime_module._bounded_canonical_observation(
            exact_rows,
            "projected_rows",
        )
        self.assertEqual(exact.count, 1)
        self.assertEqual(exact.sha256, canonical_sha256(exact_rows))
        over_rows = [{"x": "a" * (limit - prefix + 1)}]
        hard = runtime_module._bounded_canonical_observation(
            over_rows,
            "projected_rows",
        )
        self.assertEqual(hard.source, "projected_rows")
        self.assertEqual(
            hard.prefix_sha256,
            runtime_module.sha256_bytes(
                canonical_bytes(over_rows)[: limit + 1]
            ),
        )
        receipt = runtime_module.RejectedObservationReceipt(
            "projected_rows",
            10001,
            "b" * 64,
        )
        attempt = make_observation_limit_attempt(
            sequence=1,
            head_oid=ZERO_SHA1,
            base_oid=ZERO_SHA1,
            receipt=receipt,
        )
        self.assertEqual(attempt["unexpected_check_count"], 10001)
        self.assertEqual(validate_ci_report_history(
            build_ci_report(
                mode="expected-v1",
                repository="owner/repository",
                pull_number=1,
                runtime_source_envelope_digest=ZERO_SHA256,
                expected_ci_checks_sha256=expected_ci_checks_sha256(expected),
                attempts=[attempt],
            )
        ), build_ci_report(
            mode="expected-v1",
            repository="owner/repository",
            pull_number=1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_ci_checks_sha256=expected_ci_checks_sha256(expected),
            attempts=[attempt],
        ))
        hard_attempt = make_observation_hard_limit_attempt(
            sequence=1,
            head_oid=ZERO_SHA1,
            base_oid=ZERO_SHA1,
            hard_limit=hard,
        )
        self.assertEqual(hard_attempt["safe_error"]["code"], "hard_limit")
        self.assertEqual(hard_attempt["unexpected_check_count"], 0)
        hard_classification = runtime_module.ExpectedCiClassification(
            "hard_limit",
            None,
            hard,
            None,
        )
        self.assertIsNone(hard_classification.value)
        for malformed in (
            {
                **attempt,
                "expected_checks": [observed_check()],
            },
            {
                **attempt,
                "retry": {
                    "job_id": 1,
                    "failure_fingerprint_sha256": ZERO_SHA256,
                },
            },
            {
                **hard_attempt,
                "unexpected_check_count": 1,
            },
            {
                **attempt,
                "unexpected_check_count": 2147483648,
            },
        ):
            with self.assertRaises(RuntimeContractError):
                validate_ci_report(
                    {
                        **ci_report(),
                        "attempts": [malformed],
                    }
                )
        oversized = ci_attempt()
        oversized["expected_checks"] = [
            {
                "workflow_name": "W" * 256,
                "check_name": f"{index:03d}-" + "C" * 252,
                "bucket": "pass",
                "state": "SUCCESS",
                "link": "https://github.com/owner/repository/actions/runs/1",
            }
            for index in range(100)
        ]
        prepared = prepare_ci_attempt(oversized, receipt)
        self.assertEqual(prepared["safe_error"]["code"], "observation_limit")
        self.assertEqual(prepared["unexpected_check_count"], receipt.count)
        with self.assertRaises(RuntimeContractError):
            prepare_ci_attempt(
                {**oversized, "reason": "not-a-reason"},
                receipt,
            )
        with self.assertRaises(RuntimeContractError):
            prepare_ci_attempt(
                oversized,
                runtime_module.RejectedObservationReceipt(
                    "unexpected_rows",
                    10001,
                    "b" * 64,
                ),
            )

    def test_observation_limit_history_compatibility_and_hard_fields(self) -> None:
        legacy = {
            **ci_attempt(reason="report_invalid"),
            "expected_checks": [observed_check()],
            "unexpected_check_count": 1,
            "unexpected_checks_sha256": canonical_sha256([observed_check()]),
            "retry": {
                "job_id": 7,
                "failure_fingerprint_sha256": "a" * 64,
            },
            "safe_error": {
                "code": "observation_limit",
                "exit_code": 1,
                "stdout_sha256": "b" * 64,
                "stderr_sha256": "c" * 64,
            },
        }
        self.assertEqual(
            validate_ci_report({**ci_report(), "attempts": [legacy]})[
                "attempts"
            ][0],
            legacy,
        )

        def bounded(count: int) -> dict[str, object]:
            return {
                **ci_attempt(reason="report_invalid"),
                "expected_checks": [],
                "unexpected_check_count": count,
                "unexpected_checks_sha256": "d" * 64,
                "retry": None,
                "safe_error": {
                    "code": "observation_limit",
                    "exit_code": 1,
                    "stdout_sha256": "e" * 64,
                    "stderr_sha256": "f" * 64,
                },
            }

        for count in (10000, 10001, 2147483647):
            with self.subTest(count=count):
                validate_ci_report(
                    {**ci_report(), "attempts": [bounded(count)]}
                )
        with self.assertRaises(RuntimeContractError):
            validate_ci_report(
                {**ci_report(), "attempts": [bounded(2147483648)]}
            )

        hard = make_observation_hard_limit_attempt(
            sequence=1,
            head_oid=ZERO_SHA1,
            base_oid=ZERO_SHA1,
            hard_limit=runtime_module.RejectedObservationHardLimit(
                "projected_rows",
                "1" * 64,
            ),
            watcher_exit_code=1,
        )
        mutations = {
            "unexpected_digest": {
                **hard,
                "unexpected_checks_sha256": "2" * 64,
            },
            "stdout_digest": {
                **hard,
                "safe_error": {
                    **hard["safe_error"],
                    "stdout_sha256": "3" * 64,
                },
            },
            "stderr_digest": {
                **hard,
                "safe_error": {
                    **hard["safe_error"],
                    "stderr_sha256": "4" * 64,
                },
            },
            "wrong_safe_error": {
                **hard,
                "safe_error": {
                    **hard["safe_error"],
                    "code": "not-safe",
                },
            },
            "missing_safe_error_field": {
                **hard,
                "safe_error": {
                    "code": "hard_limit",
                    "exit_code": 1,
                    "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                },
            },
            "nonempty_expected": {
                **hard,
                "expected_checks": [observed_check()],
            },
            "retry": {
                **hard,
                "retry": {
                    "job_id": 1,
                    "failure_fingerprint_sha256": ZERO_SHA256,
                },
            },
            "nonzero_count": {
                **hard,
                "unexpected_check_count": 1,
            },
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label):
                with self.assertRaises(RuntimeContractError):
                    validate_ci_report(
                        {**ci_report(), "attempts": [mutation]}
                    )

    def test_report_invalid_safe_error_null_preserves_c1_boundaries(self) -> None:
        expected = observed_check()
        expected_digest = canonical_sha256([expected])
        retry = {
            "job_id": 7,
            "failure_fingerprint_sha256": "a" * 64,
        }
        for count in (0, 1, 10000):
            with self.subTest(count=count):
                attempt = {
                    **ci_attempt(reason="report_invalid"),
                    "expected_checks": [expected],
                    "unexpected_check_count": count,
                    "unexpected_checks_sha256": expected_digest,
                    "retry": retry,
                    "safe_error": None,
                }
                validated = validate_ci_report(
                    {**ci_report(), "attempts": [attempt]}
                )
                self.assertEqual(validated["attempts"][0], attempt)

        with self.assertRaises(RuntimeContractError):
            validate_ci_report(
                {
                    **ci_report(),
                    "attempts": [
                        {
                            **ci_attempt(reason="report_invalid"),
                            "expected_checks": [expected],
                            "unexpected_check_count": 10001,
                            "unexpected_checks_sha256": expected_digest,
                            "retry": retry,
                            "safe_error": None,
                        }
                    ],
                }
            )

        observation_limit = make_observation_limit_attempt(
            sequence=1,
            head_oid=ZERO_SHA1,
            base_oid=ZERO_SHA1,
            receipt=runtime_module.RejectedObservationReceipt(
                "unexpected_rows",
                10001,
                "b" * 64,
            ),
        )
        self.assertEqual(observation_limit["safe_error"]["code"], "observation_limit")
        hard_limit = make_observation_hard_limit_attempt(
            sequence=1,
            head_oid=ZERO_SHA1,
            base_oid=ZERO_SHA1,
            hard_limit=runtime_module.RejectedObservationHardLimit(
                "projected_rows",
                "c" * 64,
            ),
        )
        self.assertEqual(hard_limit["safe_error"]["code"], "hard_limit")
        self.assertEqual(hard_limit["unexpected_check_count"], 0)

    def test_expected_ci_classification_is_deeply_immutable_and_materializes(self) -> None:
        receipt = runtime_module.RejectedObservationReceipt(
            "projected_rows",
            1,
            "a" * 64,
        )
        source = {
            "transition": "all_expected_checks_terminal_green",
            "nested": {
                "items": [{"name": "unit"}],
                "tuple": ("stable", {"value": 1}),
            },
        }
        classification = runtime_module.ExpectedCiClassification(
            "ordinary",
            source,
            receipt,
            None,
        )
        source["nested"]["items"].append({"name": "mutated"})
        source["nested"]["tuple"][1]["value"] = 2

        frozen = classification.value
        self.assertIsNotNone(frozen)
        assert frozen is not None
        with self.assertRaises(TypeError):
            frozen["nested"] = {}
        with self.assertRaises(TypeError):
            frozen["nested"]["items"][0]["name"] = "mutated"
        with self.assertRaises(TypeError):
            frozen["nested"]["items"][0] = {}
        with self.assertRaises(AttributeError):
            frozen["nested"]["items"].append({"name": "mutated"})

        first = classification.materialize_value()
        second = classification.materialize_value()
        self.assertIsInstance(first["nested"]["items"], list)
        self.assertIsInstance(first["nested"]["tuple"], tuple)
        self.assertIsInstance(first["nested"]["tuple"][1], dict)
        first["nested"]["items"][0]["name"] = "changed"
        first["nested"]["items"].append({"name": "new"})
        first["nested"]["tuple"][1]["value"] = 9
        self.assertEqual(second["nested"]["items"], [{"name": "unit"}])
        self.assertEqual(second["nested"]["tuple"][1]["value"], 1)
        self.assertEqual(classification.materialize_value(), second)
        self.assertEqual(classification.projected_observations, receipt)

        expected = expected_checks()
        digest = expected_ci_checks_sha256(expected)
        legacy_first = classify_expected_ci_checks(
            expected,
            [observed_check()],
            current_repository="owner/repository",
            current_head_oid=ZERO_SHA1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_checks_digest=digest,
        )
        legacy_second = classify_expected_ci_checks(
            expected,
            [observed_check()],
            current_repository="owner/repository",
            current_head_oid=ZERO_SHA1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_checks_digest=digest,
        )
        legacy_first["expected_checks"][0]["check_name"] = "changed"
        self.assertEqual(
            legacy_second["expected_checks"][0]["check_name"],
            "unit",
        )

    def test_expected_ci_acceptance_matrix_boundaries_and_authority(self) -> None:
        expected = expected_checks()
        digest = expected_ci_checks_sha256(expected)
        expected_row = observed_check()
        unexpected_rows = [
            {
                **expected_row,
                "check_name": "unexpected-{:05d}".format(index),
            }
            for index in range(10000)
        ]
        ordinary = classify_expected_ci_checks_with_receipt(
            expected,
            [expected_row, *unexpected_rows[:10000]],
            current_repository="owner/repository",
            current_head_oid=ZERO_SHA1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_checks_digest=digest,
        )
        self.assertEqual(ordinary.state, "ordinary")
        self.assertEqual(ordinary.value["unexpected_check_count"], 10000)
        for field, value in (
            ("repository", "other/repository"),
            ("head", "1" * 40),
            ("runtime_source_envelope_digest", "1" * 64),
            ("expected_checks_digest", "1" * 64),
        ):
            arguments = {
                "current_repository": "owner/repository",
                "current_head_oid": ZERO_SHA1,
                "runtime_source_envelope_digest": ZERO_SHA256,
                "expected_checks_digest": digest,
            }
            if field == "repository":
                arguments["current_repository"] = value
            elif field == "head":
                arguments["current_head_oid"] = value
            elif field == "expected_checks_digest":
                arguments[field] = value
            else:
                arguments[field] = value
            for observations in (
                [{"workflow_name": "bad"}],
                [{"workflow_name": "x" * 5_000_000}],
            ):
                with mock.patch.object(
                    runtime_module,
                    "_bounded_canonical_observation",
                    side_effect=AssertionError("authority must short-circuit"),
                ):
                    authority = classify_expected_ci_checks_with_receipt(
                        expected,
                        observations,
                        **arguments,
                    )
                self.assertEqual(authority.state, "ordinary")
                self.assertIsNone(authority.projected_observations)
                self.assertIsNone(authority.unexpected_observations)

        non_special = {
            **ci_attempt(),
            "unexpected_check_count": 10001,
        }
        with self.assertRaises(RuntimeContractError):
            validate_ci_report({**ci_report(), "attempts": [non_special]})

        def sized_candidate(extra: int) -> dict[str, object]:
            rows = [
                {
                    **observed_check(),
                    "workflow_name": "W",
                    "check_name": "{:03d}".format(index),
                    "link": "https://github.com/" + "x" * 1522,
                }
                for index in range(30)
            ]
            rows[-1]["link"] += "x" * extra
            return {**ci_attempt(), "expected_checks": rows}

        exact = sized_candidate(10)
        self.assertEqual(
            len(canonical_bytes(exact)),
            runtime_module.MAX_CI_ATTEMPT_BYTES,
        )
        projected_receipt = runtime_module.RejectedObservationReceipt(
            "projected_rows",
            30,
            ZERO_SHA256,
        )
        self.assertEqual(
            prepare_ci_attempt(exact, projected_receipt),
            exact,
        )
        over = prepare_ci_attempt(
            sized_candidate(11),
            projected_receipt,
        )
        self.assertEqual(over["safe_error"]["code"], "observation_limit")
        with self.assertRaises(RuntimeContractError):
            prepare_ci_attempt(
                {**sized_candidate(11), "reason": "not-a-reason"},
                projected_receipt,
            )

        special_attempts = [
            make_observation_limit_attempt(
                sequence=index,
                head_oid=ZERO_SHA1,
                base_oid=ZERO_SHA1,
                receipt=runtime_module.RejectedObservationReceipt(
                    "unexpected_rows",
                    10001,
                    "a" * 64,
                ),
            )
            if index % 2
            else make_observation_hard_limit_attempt(
                sequence=index,
                head_oid=ZERO_SHA1,
                base_oid=ZERO_SHA1,
                hard_limit=runtime_module.RejectedObservationHardLimit(
                    "projected_rows",
                    "b" * 64,
                ),
            )
            for index in range(1, 10)
        ]
        special_report = build_ci_report(
            mode="expected-v1",
            repository="owner/repository",
            pull_number=1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_ci_checks_sha256=digest,
            attempts=special_attempts,
        )
        self.assertEqual(special_report["discarded_attempt_count"], 1)
        self.assertEqual(
            validate_ci_report_history(special_report, [special_attempts[0]]),
            special_report,
        )

    def test_ci_report_attempts_and_discarded_chain_are_bounded(self) -> None:
        report = ci_report()
        self.assertEqual(validate_ci_report(report), report)
        legacy = {
            **report,
            "mode": "legacy-schema3-observed-checks-v1",
            "runtime_source_envelope_digest": None,
            "expected_ci_checks_sha256": None,
        }
        self.assertEqual(validate_ci_report(legacy), legacy)
        with self.assertRaises(RuntimeContractError):
            validate_ci_report(
                {**report, "runtime_source_envelope_digest": None}
            )
        with self.assertRaises(RuntimeContractError):
            validate_ci_report(
                {
                    **legacy,
                    "expected_ci_checks_sha256": ZERO_SHA256,
                }
            )
        attempt = ci_attempt()
        first_digest = discarded_attempt_digest(ZERO_SHA256, attempt)
        self.assertEqual(
            first_digest,
            discarded_attempt_digest(ZERO_SHA256, attempt),
        )
        with self.assertRaises(RuntimeContractError):
            validate_ci_report(
                {**report, "attempts": [ci_attempt(sequence=2)]}
            )
        attempts = [ci_attempt(sequence=index) for index in range(1, 10)]
        bounded = build_ci_report(
            mode="expected-v1",
            repository="owner/repository",
            pull_number=1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_ci_checks_sha256=expected_ci_checks_sha256(
                expected_checks()
            ),
            attempts=attempts,
        )
        self.assertEqual(bounded["discarded_attempt_count"], 1)
        self.assertEqual(
            validate_ci_report_history(bounded, [attempts[0]]),
            bounded,
        )
        appended = append_ci_report_attempt(bounded, ci_attempt(sequence=10))
        self.assertEqual(appended["discarded_attempt_count"], 2)
        self.assertEqual(
            validate_ci_report_history(
                appended,
                [attempts[0], attempts[1]],
            ),
            appended,
        )
        with self.assertRaises(RuntimeContractError):
            validate_ci_report_history(
                {**appended, "discarded_attempts_sha256": "1" * 64},
                [attempts[0], attempts[1]],
            )
        with self.assertRaises(RuntimeContractError):
            validate_ci_report_history(
                {**appended, "discarded_attempt_count": 3},
                [attempts[0], attempts[1]],
            )
        with self.assertRaises(RuntimeContractError):
            append_ci_report_attempt(
                {**appended, "discarded_attempts_sha256": "1" * 64},
                ci_attempt(sequence=11),
                discarded_attempts=[attempts[0], attempts[1]],
            )
        malformed_previous = {
            **bounded,
            "attempts": [
                {**bounded["attempts"][0], "unknown": True},
                *bounded["attempts"][1:],
            ],
        }
        with self.assertRaises(RuntimeContractError):
            append_ci_report_attempt(
                malformed_previous,
                ci_attempt(sequence=10),
            )
        rich_checks = [
            {
                **observed_check(),
                "workflow_name": "W" * 256,
                "check_name": f"{index:03d}-" + "C" * 252,
            }
            for index in range(20)
        ]
        rich = build_ci_report(
            mode="expected-v1",
            repository="owner/repository",
            pull_number=1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_ci_checks_sha256=expected_ci_checks_sha256(
                expected_checks()
            ),
            attempts=[
                {
                    **ci_attempt(sequence=index),
                    "expected_checks": rich_checks,
                }
                for index in range(1, 9)
            ],
        )
        self.assertLessEqual(len(canonical_bytes(rich)), 64 * 1024)
        self.assertGreater(rich["discarded_attempt_count"], 0)
        self.assertEqual(rich["attempts"][-1]["sequence"], 8)

    def test_ci_attempt_and_report_limits_fail_closed(self) -> None:
        observations = [
            {
                **observed_check(),
                "workflow_name": "W" * 256,
                "check_name": f"{index:03d}-" + "C" * 252,
            }
            for index in range(100)
        ]
        oversized = {**ci_attempt(), "expected_checks": observations}
        with self.assertRaises(RuntimeContractError):
            validate_ci_report({**ci_report(), "attempts": [oversized]})
        invalid = build_ci_report(
            mode="expected-v1",
            repository="owner/repository",
            pull_number=1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_ci_checks_sha256=expected_ci_checks_sha256(
                expected_checks()
            ),
            attempts=[oversized],
        )
        self.assertEqual(invalid["attempts"][0]["reason"], "report_invalid")
        self.assertEqual(invalid["attempts"][0]["expected_checks"], [])
        self.assertEqual(classify_ci_report(invalid), "ci_watch_blocked")
        duplicate_attempt = {
            **ci_attempt(reason="duplicate_observed_check"),
            "expected_checks": [observed_check(), observed_check()],
        }
        duplicate_report = build_ci_report(
            mode="expected-v1",
            repository="owner/repository",
            pull_number=1,
            runtime_source_envelope_digest=ZERO_SHA256,
            expected_ci_checks_sha256=expected_ci_checks_sha256(
                expected_checks()
            ),
            attempts=[duplicate_attempt],
        )
        self.assertEqual(
            duplicate_report["attempts"][0]["reason"],
            "duplicate_observed_check",
        )
        self.assertEqual(
            classify_ci_report(duplicate_report),
            "ci_watch_blocked",
        )
        self.assertEqual(
            classify_ci_report(
                build_ci_report(
                    mode="expected-v1",
                    repository="owner/repository",
                    pull_number=1,
                    runtime_source_envelope_digest=ZERO_SHA256,
                    expected_ci_checks_sha256=expected_ci_checks_sha256(
                        expected_checks()
                    ),
                    attempts=[
                        {
                            **ci_attempt(
                                reason="duplicate_observed_check"
                            ),
                            "safe_error": {
                                "code": "report_invalid",
                                "exit_code": 1,
                                "stdout_sha256": ZERO_SHA256,
                                "stderr_sha256": ZERO_SHA256,
                            },
                        }
                    ],
                )
            ),
            "ci_watch_blocked",
        )
        minimal = make_report_invalid_attempt(
            sequence=1,
            head_oid=ZERO_SHA1,
            base_oid=ZERO_SHA1,
            raw_observations=observations,
            watcher_exit_code=1,
            stdout=b"stdout",
            stderr=b"stderr",
        )
        self.assertEqual(minimal["unexpected_check_count"], 100)
        self.assertEqual(minimal["safe_error"]["code"], "observation_limit")
        malformed = (
            {**ci_attempt(), "reason": "not-a-reason"},
            {**ci_attempt(), "head_oid": "not-a-commit"},
            {**ci_attempt(), "unexpected_checks_sha256": "not-a-digest"},
            {**ci_attempt(), "unexpected_check_count": 10001},
            {**ci_attempt(), "sequence": 0},
            {
                **ci_attempt(),
                "safe_error": {
                    "code": "not-safe",
                    "exit_code": 0,
                    "stdout_sha256": ZERO_SHA256,
                    "stderr_sha256": ZERO_SHA256,
                },
            },
        )
        for value in malformed:
            with self.subTest(value=value):
                with self.assertRaises(RuntimeContractError):
                    build_ci_report(
                        mode="expected-v1",
                        repository="owner/repository",
                        pull_number=1,
                        runtime_source_envelope_digest=ZERO_SHA256,
                        expected_ci_checks_sha256=expected_ci_checks_sha256(
                            expected_checks()
                        ),
                        attempts=[value],
                    )
