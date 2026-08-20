from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
import unittest

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
    classify_pr_feedback,
    classify_terminal_state,
    classify_verification_report,
    discarded_attempt_digest,
    expected_ci_checks_sha256,
    make_pr_feedback_cursor,
    make_report_invalid_attempt,
    parse_runtime_external_captures,
    parse_terminal_marker_line,
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
        self.assertEqual(canonical_bytes({"z": 1, "a": "é"}), b'{"a":"\xc3\xa9","z":1}')
        self.assertEqual(
            canonical_sha256({"z": 1, "a": "é"}),
            "fb64e573f7cde5b7efeda52ffc4bdd57572055b0b7e64a70172606c82c6c7eac",
        )
        with self.assertRaises(RuntimeContractError):
            canonical_bytes(float("nan"))

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
