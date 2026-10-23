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
    build_ci_report,
    build_terminal_seal_record,
    canonical_bytes,
    canonical_sha256,
    classify_verification_report,
    delivery_context_fields,
    discarded_attempt_digest,
    expected_ci_checks_sha256,
    make_report_invalid_attempt,
    parse_runtime_external_captures,
    parse_terminal_marker_line,
    terminal_marker_line,
    validate_ci_report,
    validate_cleanup_report,
    validate_ci_archive,
    validate_delivery_context,
    validate_expected_ci_checks,
    validate_pr_feedback_item,
    validate_runtime_source_envelope,
    validate_terminal_marker,
    validate_terminal_seal_request,
    validate_verification_report,
)


ZERO_SHA256 = "0" * 64
ZERO_SHA1 = "0" * 40


class RetainedScriptReaderTest(unittest.TestCase):
    def test_closed_native_script_reader_never_projects_agent_owner(self):
        context = {"task_id": "native-task", "node_id": "janitor", "transition_branch_key": "cleanup"}
        detail = {
            "summary": {"id": "native-task", "workflow_id": "workflow"},
            "current_scripts": [{
                "current_node": {"node_id": "janitor", "transition_branch_key": "cleanup"},
                "path": "/workspace/.kent/scripts/janitor",
            }],
        }
        graph = {"workflow_id": "workflow", "graph": {"nodes": [{
            "id": "janitor", "kind": "script", "script_path": ".kent/scripts/janitor",
        }]}}

        def validate(native=context, observed=detail, selected=graph):
            return runtime_module.validate_retained_script_reader(
                native, detail=observed, graph=selected, workspace="/workspace",
            )

        self.assertEqual(validate(), {**context, "invocation_kind": "script"})
        for native in (
            {}, None, {**context, "session_id": "borrowed"}, {**context, "step_id": "fabricated"},
            {**context, "cleanup_session_id": "borrowed"}, {**context, "task_id": "foreign"},
            {**context, "node_id": "foreign"}, {**context, "transition_branch_key": "foreign"},
        ):
            with self.subTest(native=native), self.assertRaises(RuntimeContractError):
                validate(native)
        for observed in (
            {}, {"summary": None}, {**detail, "current_scripts": None},
            {**detail, "current_scripts": detail["current_scripts"] * 2},
            {**detail, "current_scripts": [{
                **detail["current_scripts"][0], "current_node": {
                    **detail["current_scripts"][0]["current_node"], "session_id": "agent",
                },
            }]},
        ):
            with self.subTest(observed=observed), self.assertRaises(RuntimeContractError):
                validate(observed=observed)
        for selected in (
            {}, {**graph, "workflow_id": "foreign"},
            {"workflow_id": "workflow", "graph": {"nodes": []}},
            {"workflow_id": "workflow", "graph": {"nodes": [{
                "id": "janitor", "kind": "agent", "script_path": ".kent/scripts/janitor",
            }]}},
        ):
            with self.subTest(graph=selected), self.assertRaises(RuntimeContractError):
                validate(selected=selected)


class EvidenceRecoveryViewTest(unittest.TestCase):
    def test_typed_script_ci_archive_row_is_closed_and_chain_bound(self):
        identity = {
            "task_id": "native-task",
            "task_short_id": "TASK-4",
            "project_id": "project-123",
            "workspace_path": "/workspace",
            "invocation_kind": "script",
            "node_id": "ci_prepare",
            "transition_branch_key": None,
        }
        row = runtime_module.build_script_ci_archive_record(
            task_short_id="TASK-4",
            workspace_path="/workspace",
            script_identity=identity,
            report_sha256="a" * 64,
            sequence=1,
            previous_hash="",
        )
        self.assertEqual(row["schema_version"], 3)
        self.assertEqual(
            row["artifacts"],
            [".kent/runtime/TASK-4/ci-report-" + "a" * 64 + ".json"],
        )
        self.assertEqual(
            runtime_module.validate_script_ci_archive_record(
                row,
                task_short_id="TASK-4",
                workspace_path="/workspace",
                sequence=1,
                previous_hash="",
            ),
            row,
        )
        self.assertFalse(runtime_module.is_ordinary_evidence_record(row))
        self.assertEqual(
            runtime_module.validate_evidence_view(
                runtime_module.canonical_bytes(row) + b"\n",
                task_short_id="TASK-4",
            )["records"],
            [row],
        )

        for changes in (
            {"workspace_path": "/other"},
            {"sequence": 2},
            {"previous_hash": "b" * 64},
            {"artifacts": [".kent/runtime/TASK-4/other.json"]},
            {"script_identity": {**identity, "invocation_kind": "agent"}},
            {"extra": "not-closed"},
        ):
            invalid = {**row, **changes}
            invalid["event_hash"] = runtime_module._event_hash(
                {key: item for key, item in invalid.items() if key != "event_hash"}
            )
            with self.subTest(changes=changes), self.assertRaises(RuntimeContractError):
                runtime_module.validate_script_ci_archive_record(
                    invalid,
                    task_short_id="TASK-4",
                    workspace_path="/workspace",
                    sequence=1,
                    previous_hash="",
                )

    def test_v2_identity_and_mixed_chain(self):
        legacy = self.record()
        owner = {
            "session_id": "11111111-1111-4111-8111-111111111111",
            "step_id": "22222222-2222-4222-8222-222222222222",
        }
        current = {
            "schema_version": 2, "sequence": 2, "task_short_id": "TASK-4",
            "previous_hash": legacy["event_hash"], **owner,
        }
        current["event_hash"] = runtime_module._event_hash(current)
        raw = self.line(legacy) + self.line(current)
        self.assertEqual(runtime_module.validate_evidence_view(
            raw, task_short_id="TASK-4")["records"], [legacy, current])
        request = runtime_module.build_evidence_recovery_request(
            raw + b"broken", task_short_id="TASK-4", owner=owner)
        self.assertEqual(request["owner"], owner)
        self.assertEqual(request["owner_provenance"]["schema"], "kent-owner-provenance-v2")
        self.assertFalse(request["owner_provenance"]["independently_verified_current_step"])
        for version, extra in ((3, {}), (2, {"run_id": "stale"}), (2, {"step_id": "unknown"})):
            invalid = {**current, "schema_version": version, **extra}
            invalid["event_hash"] = runtime_module._event_hash(
                {k: v for k, v in invalid.items() if k != "event_hash"})
            with self.assertRaises(RuntimeContractError):
                runtime_module.validate_evidence_view(
                    self.line(legacy) + self.line(invalid), task_short_id="TASK-4")

    def record(self, sequence=1, previous="", **extra):
        value = {
            "schema_version": 1, "sequence": sequence,
            "task_short_id": "TASK-4", "previous_hash": previous,
            "session_id": "session", "run_id": "run", "step_id": "step",
            **extra,
        }
        return {**value, "event_hash": runtime_module._event_hash(value)}

    def line(self, record):
        return runtime_module.canonical_bytes(record) + b"\n"

    def fixture(self):
        prefix = self.line(self.record())
        original = prefix + b'{"damaged":'
        owner = {"session_id": "new-session", "run_id": "new-run", "step_id": "new-step"}
        request = runtime_module.build_evidence_recovery_request(
            original, task_short_id="TASK-4", owner=owner,
        )
        digest = runtime_module.canonical_sha256(request)
        boundary = self.record(
            2, self.record()["event_hash"], **owner,
            record_kind="evidence_recovery_boundary_v1",
            recovery_request_sha256=digest,
        )
        manifest = {
            "schema": "evidence-recovery-v1", "request": request,
            "request_sha256": digest, "boundary_event_hash": boundary["event_hash"],
        }
        return original, manifest, boundary

    def test_healthy_view_and_legacy_unterminated_line(self):
        record = self.record()
        view = runtime_module.validate_evidence_view(
            self.line(record).rstrip(b"\n"), task_short_id="TASK-4",
        )
        self.assertEqual(view["records"], [record])
        self.assertEqual(view["status"], "healthy")

    def test_recovered_view_omits_opaque_suffix_and_keeps_gap(self):
        original, manifest, boundary = self.fixture()
        following = self.record(3, boundary["event_hash"], run_id="new-run")
        view = runtime_module.validate_evidence_view(
            original, task_short_id="TASK-4", manifest=manifest,
            continuation=self.line(boundary) + self.line(following),
        )
        self.assertEqual(view["status"], "recovered_with_gap")
        self.assertEqual(view["records"], [self.record(), boundary, following])
        self.assertEqual(view["recovery_request_sha256"], manifest["request_sha256"])

    def test_incomplete_or_changed_publication_and_second_corruption_rejected(self):
        original, manifest, boundary = self.fixture()
        for base, metadata, tail in (
            (original, None, self.line(boundary)),
            (original, manifest, None),
            (original + b"x", manifest, self.line(boundary)),
            (original, manifest, self.line(boundary) + b"bad"),
            (original, manifest, self.line(boundary) + self.line(boundary)),
        ):
            with self.subTest(metadata=metadata, tail=tail):
                with self.assertRaises(RuntimeContractError):
                    runtime_module.validate_evidence_view(
                        base, task_short_id="TASK-4", manifest=metadata,
                        continuation=tail,
                    )

    def test_unknown_beginning_healthy_chain_and_unknown_owner_rejected(self):
        for original, owner in (
            (b"broken", {"session_id": "s", "run_id": "r", "step_id": "t"}),
            (self.line(self.record()), {"session_id": "s", "run_id": "r", "step_id": "t"}),
            (self.line(self.record()) + b"bad", {
                "session_id": "s", "run_id": "unknown", "step_id": "t",
            }),
        ):
            with self.assertRaises(RuntimeContractError):
                runtime_module.build_evidence_recovery_request(
                    original, task_short_id="TASK-4", owner=owner,
                )

    def test_boundary_must_match_request_owner_and_cannot_be_final_ordinary(self):
        original, manifest, boundary = self.fixture()
        boundary["run_id"] = "forged"
        unsigned = {k: v for k, v in boundary.items() if k != "event_hash"}
        boundary["event_hash"] = runtime_module._event_hash(unsigned)
        manifest["boundary_event_hash"] = boundary["event_hash"]
        with self.assertRaises(RuntimeContractError):
            runtime_module.validate_evidence_view(
                original, task_short_id="TASK-4", manifest=manifest,
                continuation=self.line(boundary),
            )

    def test_boundary_is_not_final_ordinary_and_seals_cannot_be_recovered(self):
        original, manifest, boundary = self.fixture()
        request = {
            "schema": runtime_module.TERMINAL_SEAL_REQUEST_SCHEMA,
            "operation_report_digests": [],
            "redaction": {"status": "passed", "report_sha256": ZERO_SHA256},
            "retention_class": "cleanup_report_only",
        }
        seal = runtime_module.build_terminal_seal_record(
            request, sequence=3, task_short_id="TASK-4",
            previous_hash=boundary["event_hash"],
        )
        with self.assertRaises(RuntimeContractError):
            runtime_module.validate_evidence_view(
                original, task_short_id="TASK-4", manifest=manifest,
                continuation=self.line(boundary) + self.line(seal),
            )
        with self.assertRaises(RuntimeContractError):
            runtime_module.validate_terminal_chain(
                [self.record(), boundary, seal], task_short_id="TASK-4",
            )
        ordinary = self.record(3, boundary["event_hash"])
        seal = runtime_module.build_terminal_seal_record(
            request, sequence=4, task_short_id="TASK-4",
            previous_hash=ordinary["event_hash"],
        )
        view = runtime_module.validate_evidence_view(
            original, task_short_id="TASK-4", manifest=manifest,
            continuation=self.line(boundary) + self.line(ordinary) + self.line(seal),
        )
        runtime_module.validate_terminal_chain(view["records"], task_short_id="TASK-4")
        with self.assertRaises(RuntimeContractError):
            runtime_module.build_evidence_recovery_request(
                self.line(self.record()) + self.line(
                    runtime_module.build_terminal_seal_record(
                        request, sequence=2, task_short_id="TASK-4",
                        previous_hash=self.record()["event_hash"],
                    )
                ) + b"broken",
                task_short_id="TASK-4", owner=manifest["request"]["owner"],
            )

    def test_duplicate_json_keys_are_damaged_not_valid_prefix(self):
        record = self.record()
        raw = self.line(record).rstrip(b"\n")
        duplicate = raw[:-1] + b',"run_id":"run"}\n'
        with self.assertRaises(RuntimeContractError):
            runtime_module.validate_evidence_view(duplicate, task_short_id="TASK-4")
        with self.assertRaises(RuntimeContractError):
            runtime_module.build_evidence_recovery_request(
                duplicate + b"broken", task_short_id="TASK-4",
                owner={"session_id": "s", "run_id": "r", "step_id": "t"},
            )

    def test_closed_request_and_manifest_and_no_synthetic_boundary_claims(self):
        original, manifest, boundary = self.fixture()
        for mutate in (
            lambda m, b: m.update(extra=True),
            lambda m, b: m["request"].update(extra=True),
            lambda m, b: m["request"].update(prefix_count=True),
            lambda m, b: b.update(checks=["claimed PASS"]),
        ):
            original, manifest, boundary = self.fixture()
            mutate(manifest, boundary)
            with self.assertRaises(RuntimeContractError):
                runtime_module.validate_evidence_view(
                    original, task_short_id="TASK-4", manifest=manifest,
                    continuation=self.line(boundary),
                )

    def test_byte_and_record_limits_are_not_truncated(self):
        with self.assertRaises(RuntimeContractError):
            runtime_module.validate_evidence_view(
                b"x" * (runtime_module.MAX_EVIDENCE_BYTES + 1), task_short_id="TASK-4",
            )
        with self.assertRaises(RuntimeContractError):
            runtime_module.validate_evidence_view(
                b"\n" * (runtime_module.MAX_EVIDENCE_RECORDS + 1), task_short_id="TASK-4",
            )
        # Corrupt bytes are opaque, not logical records or authority.
        original = self.line(self.record()) + b"damaged\n" * 10000
        request = runtime_module.build_evidence_recovery_request(
            original, task_short_id="TASK-4",
            owner={"session_id": "s", "run_id": "r", "step_id": "t"},
        )
        self.assertEqual(request["prefix_count"], 1)
        self.assertEqual(request["suffix_byte_size"], len(b"damaged\n") * 10000)

    def test_owner_provenance_is_descriptive_closed_and_hash_bound(self):
        original, manifest, boundary = self.fixture()
        expected = dict(runtime_module.LEGACY_OWNER_PROVENANCE)
        self.assertEqual(manifest["request"]["owner_provenance"], expected)
        for changed in (
            {**expected, "independently_verified_current_run": True},
            {**expected, "independently_verified_current_run": 0},
            {**expected, "extra": "authority"},
            {**expected, "run_step_binding": "native-readback"},
        ):
            with self.assertRaises(RuntimeContractError):
                runtime_module.validate_owner_provenance(changed)
            original, manifest, boundary = self.fixture()
            manifest["request"]["owner_provenance"] = changed
            with self.assertRaises(RuntimeContractError):
                runtime_module.validate_evidence_view(
                    original, task_short_id="TASK-4", manifest=manifest,
                    continuation=self.line(boundary),
                )

    def test_recovery_reserves_room_for_boundary_final_ordinary_and_seal(self):
        records = []
        previous = ""
        for sequence in range(1, runtime_module.MAX_EVIDENCE_RECORDS - 1):
            record = self.record(sequence, previous)
            records.append(self.line(record))
            previous = record["event_hash"]
        with self.assertRaisesRegex(RuntimeContractError, "capacity"):
            runtime_module.build_evidence_recovery_request(
                b"".join(records) + b"broken", task_short_id="TASK-4",
                owner={"session_id": "s", "run_id": "r", "step_id": "t"},
            )

    def test_retention_receipt_closed_inventory_and_binding(self):
        unsigned = {
            "schema": "evidence-retention-v1", "task_short_id": "TASK-4",
            "source_workspace": "/source",
            "source_native_identity": {"project_id": "project", "workspace_id": "workspace", "task_id": "task"},
            "terminal_event_hash": ZERO_SHA256, "recovery_request_sha256": None,
            "owner_provenance": dict(runtime_module.LEGACY_OWNER_PROVENANCE),
            "files": [{
                "logical_path": ".kent/runtime/TASK-4/evidence-ledger.jsonl",
                "byte_size": 1, "sha256": ZERO_SHA256,
            }],
        }
        receipt = {**unsigned, "bundle_sha256": runtime_module.canonical_sha256(unsigned)}
        self.assertEqual(runtime_module.validate_evidence_retention_receipt(receipt), receipt)
        for change in (
            {"bundle_sha256": "a" * 64},
            {"files": unsigned["files"] * 2},
            {"files": [{**unsigned["files"][0], "logical_path": "../secret"}]},
            {"files": [{**unsigned["files"][0], "byte_size": True}]},
            {"files": [{**unsigned["files"][0], "byte_size": 256 * 1024 * 1024 + 1}]},
            {"extra": True},
            {"source_workspace": "relative"},
        ):
            with self.subTest(change=change), self.assertRaises(RuntimeContractError):
                runtime_module.validate_evidence_retention_receipt({**receipt, **change})


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


def dynamic_check(
    *,
    workflow_name: str = "Pull Request",
    check_name: str = "unit",
    event: str = "pull_request",
    bucket: str = "pass",
    state: str = "SUCCESS",
    link: str | None = "https://github.com/owner/repository/actions/runs/1",
) -> dict[str, object]:
    return {
        "workflow_name": workflow_name,
        "check_name": check_name,
        "event": event,
        "bucket": bucket,
        "state": state,
        "link": link,
    }


def dynamic_contract(*, require_ci: bool = True) -> dict[str, object]:
    return {
        "schema": "github-ci-contract-v1",
        "repository": "owner/repository",
        "pull_number": 1,
        "head_oid": ZERO_SHA1,
        "base_oid": "1" * 40,
        "require_ci": require_ci,
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
    def test_delivery_context_is_closed_and_preserves_nested_packet_strings(self) -> None:
        pre_pr = {
            "schema": "workflow-delivery-context-v1",
            "phase": "pre_pr",
        }
        self.assertEqual(validate_delivery_context(pre_pr), pre_pr)

        packet = ' { "schema" : "not-yet-valid CI", "raw": true } '
        report_packet = " malformed but retained \n"
        post_pr = {
            "schema": "workflow-delivery-context-v1",
            "phase": "post_pr",
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "feature/TASK-17",
            "merge_strategy": "rebase",
            "pr_feedback_cursor": "uninitialized",
            "ci_contract": packet,
            "ci_report": report_packet,
        }
        self.assertEqual(validate_delivery_context(post_pr), post_pr)
        self.assertEqual(
            delivery_context_fields(
                {"delivery_context": json.dumps(post_pr, ensure_ascii=False)}
            ),
            {"delivery_context": json.dumps(post_pr, ensure_ascii=False)},
        )
        self.assertEqual(delivery_context_fields({}), {})

    def test_delivery_context_binds_feedback_cursor_to_pr_identity(self) -> None:
        context = {
            "schema": "workflow-delivery-context-v1",
            "phase": "post_pr",
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "feature/TASK-17",
            "merge_strategy": "rebase",
            "pr_feedback_cursor": canonical_bytes(
                runtime_module.make_dynamic_pr_feedback_cursor(
                    repository="owner/repository",
                    pull_number=17,
                    items=[],
                )
            ).decode("utf-8"),
        }
        self.assertEqual(validate_delivery_context(context), context)

        mismatched = {
            **context,
            "pr_feedback_cursor": canonical_bytes(
                runtime_module.make_dynamic_pr_feedback_cursor(
                    repository="another/repository",
                    pull_number=17,
                    items=[],
                )
            ).decode("utf-8"),
        }
        with self.assertRaises(RuntimeContractError):
            validate_delivery_context(mismatched)

    def test_delivery_context_v2_binds_the_observed_pr_target_branch(self) -> None:
        from workflowkit.runtime import DELIVERY_CONTEXT_V1_SCHEMA

        historical = {
            "schema": DELIVERY_CONTEXT_V1_SCHEMA,
            "phase": "post_pr",
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "feature/TASK-17",
            "merge_strategy": "rebase",
            "pr_feedback_cursor": "uninitialized",
            "ci_contract": ' { "raw": "preserved" } ',
        }
        self.assertEqual(validate_delivery_context(historical), historical)

        current = {
            **historical,
            "schema": runtime_module.DELIVERY_CONTEXT_SCHEMA,
            "pr_base_branch": "release/stable",
        }
        self.assertEqual(validate_delivery_context(current), current)
        self.assertEqual(
            delivery_context_fields(
                {"delivery_context": json.dumps(current, ensure_ascii=False)}
            )["delivery_context"],
            json.dumps(current, ensure_ascii=False),
        )
        for invalid_target in ("", "-main", " main ", "main\n"):
            with self.subTest(target=invalid_target), self.assertRaises(
                RuntimeContractError
            ):
                validate_delivery_context(
                    {**current, "pr_base_branch": invalid_target}
                )
        with self.assertRaises(RuntimeContractError):
            validate_delivery_context(
                {key: value for key, value in current.items()
                 if key != "pr_base_branch"}
            )

    def test_delivery_context_rejects_invalid_shapes_and_duplicate_json_keys(self) -> None:
        post_pr = {
            "schema": "workflow-delivery-context-v1",
            "phase": "post_pr",
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "feature/TASK-17",
            "merge_strategy": "rebase",
            "pr_feedback_cursor": "uninitialized",
        }
        invalid = (
            {**post_pr, "unexpected": "field"},
            {**post_pr, "phase": "pre_pr", "pr_url": ""},
            {**post_pr, "pr_url": "https://github.com/owner/repository/issues/17"},
            {**post_pr, "branch_name": "-unsafe"},
            {**post_pr, "merge_strategy": "unknown"},
            {**post_pr, "pr_feedback_cursor": "not-json"},
            {**post_pr, "ci_contract": ""},
            {key: value for key, value in post_pr.items() if key != "branch_name"},
            {
                **post_pr,
                "ci_report": "x" * runtime_module.MAX_DYNAMIC_CI_REPORT_BYTES + "x",
            },
            {"schema": "workflow-delivery-context-v1", "phase": "pre_pr", "branch_name": "invented"},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(RuntimeContractError):
                validate_delivery_context(value)

        for raw in (
            '{"schema":"workflow-delivery-context-v1","phase":"pre_pr",'
            '"phase":"post_pr"}',
            " " * 1_000_001,
            json.dumps({"schema": "workflow-delivery-context-v1", "phase": "pre_pr"})
            + " trailing",
        ):
            with self.subTest(raw_length=len(raw)), self.assertRaises(RuntimeContractError):
                delivery_context_fields({"delivery_context": raw})
        with self.assertRaises(RuntimeContractError):
            delivery_context_fields({"delivery_context": 17})

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
        for name in module.__all__:
            with self.subTest(export=name):
                self.assertTrue(hasattr(module, name))

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
        cursor = runtime_module.make_dynamic_pr_feedback_cursor(
            repository="owner/repository",
            pull_number=1,
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
            (runtime_module.validate_dynamic_pr_feedback_cursor, cursor),
            (validate_expected_ci_checks, expected),
            (validate_ci_report, report),
            (validate_verification_report, verification),
        )
        for validator, value in values:
            with self.subTest(validator=validator.__name__):
                with self.assertRaises(RuntimeContractError):
                    validator({**value, "unknown": True})

    def test_retained_expected_ci_apis_bind_identity_and_policy(self) -> None:
        expected = expected_checks()
        self.assertEqual(validate_expected_ci_checks(expected), expected)
        self.assertEqual(
            expected_ci_checks_sha256(expected),
            hashlib.sha256(canonical_bytes(expected)).hexdigest(),
        )
        projection = {"checks": expected["checks"]}
        digest = runtime_module.ci_policy_projection_sha256(expected)
        self.assertEqual(digest, canonical_sha256(projection))
        self.assertEqual(digest, runtime_module.ci_policy_projection_sha256({
            **expected, "project_commit": "1" * 40,
        }))
        self.assertNotEqual(digest, runtime_module.ci_policy_projection_sha256(
            expected, {"required": ["build"]},
        ))
        self.assertNotEqual(digest, runtime_module.ci_policy_projection_sha256({
            **expected, "checks": [{**expected["checks"][0], "allow_skipped": True}],
        }))
        snapshot = {
            "schema": "github-ci-policy-snapshot-v1",
            "target_commit": "2" * 40, "policy_sha256": digest,
            "source_policy_sha256": "3" * 64,
        }
        self.assertEqual(runtime_module.validate_ci_policy_snapshot(snapshot), snapshot)
        for changed in (
            {**expected, "project_commit": "bad"},
            {**expected, "runtime_source_envelope_digest": "bad"},
            {**expected, "checks": expected["checks"] * 2},
            {**expected, "checks": [{**expected["checks"][0], "allow_skipped": "true"}]},
            {**expected, "checks": [{**expected["checks"][0], "check_name": "bad\nname"}]},
        ):
            with self.subTest(changed=changed), self.assertRaises(RuntimeContractError):
                validate_expected_ci_checks(changed)
        for changed in (
            {**snapshot, "target_commit": "bad"},
            {**snapshot, "source_policy_sha256": "bad"},
            {**snapshot, "unknown": True},
        ):
            with self.subTest(changed=changed), self.assertRaises(RuntimeContractError):
                runtime_module.validate_ci_policy_snapshot(changed)

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

    def test_dynamic_ci_is_open_world_and_fail_closed(self) -> None:
        external = dynamic_check(
            workflow_name="",
            event="",
            bucket="skipping",
            state="NEUTRAL",
            link="https://app.aikido.dev/featurebranch/scan/200235380?groupId=1019",
        )
        green = runtime_module.classify_github_checks(
            [external, dynamic_check(check_name="security")],
            require_ci=True,
        )
        self.assertEqual(green["state"], "green")
        self.assertEqual(green["transition"], "green")
        self.assertEqual(green["check_count"], 2)
        self.assertEqual(
            green["checks"],
            [external, dynamic_check(check_name="security")],
        )
        same_name_different_event = runtime_module.classify_github_checks(
            [
                dynamic_check(event="pull_request"),
                dynamic_check(event="merge_group"),
            ],
            require_ci=True,
        )
        self.assertEqual(same_name_different_event["state"], "green")
        self.assertEqual(same_name_different_event["check_count"], 2)

        failed_extra = runtime_module.classify_github_checks(
            [
                dynamic_check(),
                dynamic_check(
                    check_name="external-security",
                    bucket="fail",
                    state="FAILURE",
                ),
            ],
            require_ci=True,
        )
        self.assertEqual(failed_extra["state"], "failed")
        self.assertEqual(failed_extra["transition"], "failed")
        self.assertEqual(failed_extra["check_count"], 2)

        pending = runtime_module.classify_github_checks(
            [dynamic_check(bucket="pending", state="WAITING")],
            require_ci=True,
        )
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(pending["transition"], "pending")

        self.assertEqual(
            runtime_module.classify_github_checks([], require_ci=False)["state"],
            "not_required",
        )
        self.assertEqual(
            runtime_module.classify_github_checks([], require_ci=True)["state"],
            "no_checks",
        )

        invalid = runtime_module.classify_github_checks(
            [dynamic_check(bucket="pass", state="FAILURE")],
            require_ci=True,
        )
        self.assertEqual(invalid["state"], "invalid_observation")
        self.assertEqual(
            invalid["diagnostic"]["code"],
            "state_bucket_conflict",
        )

    def test_dynamic_invalid_observation_attempt_is_valid_report_evidence(self) -> None:
        contract = runtime_module.validate_ci_contract(dynamic_contract())
        classification = runtime_module.classify_github_checks(
            [dynamic_check(bucket="pass", state="FAILURE")],
            require_ci=True,
        )
        attempt = {
            "sequence": 1,
            "head_oid": contract["head_oid"],
            "base_oid": contract["base_oid"],
            "retry": None,
            "checks": classification["checks"],
            "check_count": classification["check_count"],
            "checks_sha256": classification["checks_sha256"],
            "reason": "invalid_observation",
            "watcher_exit_code": None,
            "error": {
                "code": "invalid_observation",
                "message": "a check row is malformed or contradictory",
                "stdout_sha256": ZERO_SHA256,
                "stderr_sha256": ZERO_SHA256,
            },
        }
        report = runtime_module.build_dynamic_ci_report(
            contract=contract,
            attempts=[attempt],
        )
        self.assertEqual(
            runtime_module.validate_dynamic_ci_report(report),
            report,
        )

    def test_ci_archive_dispatcher_requires_explicit_v2_or_v3_schema(self) -> None:
        legacy = ci_report()
        self.assertEqual(validate_ci_archive(legacy), legacy)

        contract = runtime_module.validate_ci_contract(dynamic_contract())
        rows = [dynamic_check()]
        classification = runtime_module.classify_github_checks(
            rows,
            require_ci=True,
        )
        attempt = {
            "sequence": 1,
            "head_oid": contract["head_oid"],
            "base_oid": contract["base_oid"],
            "retry": None,
            "checks": classification["checks"],
            "check_count": classification["check_count"],
            "checks_sha256": classification["checks_sha256"],
            "reason": "green",
            "watcher_exit_code": 0,
            "error": None,
        }
        current = runtime_module.build_dynamic_ci_report(
            contract=contract,
            attempts=[attempt],
        )
        self.assertEqual(validate_ci_archive(current), current)

        for bucket, state, reason in (
            ("fail", "FAILURE", "failed"),
            ("pending", "WAITING", "pending"),
        ):
            with self.subTest(reason=reason):
                classified = runtime_module.classify_github_checks(
                    [dynamic_check(bucket=bucket, state=state)],
                    require_ci=True,
                )
                archive = runtime_module.build_dynamic_ci_report(
                    contract=contract,
                    attempts=[{
                        **attempt,
                        "checks": classified["checks"],
                        "check_count": classified["check_count"],
                        "checks_sha256": classified["checks_sha256"],
                        "reason": reason,
                    }],
                )
                self.assertEqual(validate_ci_archive(archive), archive)

        for unknown in (
            {**current, "schema": "github-ci-report-v4"},
            {"schema": "github-ci-report-unknown"},
        ):
            with self.subTest(schema=unknown["schema"]):
                with self.assertRaises(RuntimeContractError):
                    validate_ci_archive(unknown)

    def test_dynamic_ci_contract_and_report_preserve_retry_history(self) -> None:
        contract = runtime_module.validate_ci_contract(dynamic_contract())
        self.assertEqual(contract, dynamic_contract())
        rows = [dynamic_check(workflow_name="", event="")]
        attempt = {
            "sequence": 1,
            "head_oid": ZERO_SHA1,
            "base_oid": "1" * 40,
            "retry": None,
            "checks": rows,
            "check_count": 1,
            "checks_sha256": canonical_sha256(rows),
            "reason": "green",
            "watcher_exit_code": 0,
            "error": None,
        }
        report = runtime_module.build_dynamic_ci_report(
            contract=contract,
            attempts=[attempt],
        )
        self.assertEqual(report["schema"], "github-ci-report-v3")
        self.assertEqual(report["contract"], contract)
        self.assertEqual(
            runtime_module.validate_dynamic_ci_report(report),
            report,
        )
        retry = {
            **attempt,
            "sequence": 2,
            "checks": [
                dynamic_check(workflow_name="", event="", bucket="pending", state="WAITING")
            ],
            "checks_sha256": canonical_sha256(
                [
                    dynamic_check(
                        workflow_name="",
                        event="",
                        bucket="pending",
                        state="WAITING",
                    )
                ]
            ),
            "retry": {
                "attempt_id": "attempt-1",
                "failure_fingerprint_sha256": "a" * 64,
            },
            "reason": "pending",
        }
        appended = runtime_module.append_dynamic_ci_report(report, retry)
        self.assertEqual(
            appended["attempts"][-1]["retry"],
            retry["retry"],
        )
        self.assertEqual(appended["attempts"][-1]["sequence"], 2)
        with self.assertRaises(RuntimeContractError):
            runtime_module.validate_ci_contract(
                {**contract, "unknown": True}
            )
        with self.assertRaises(RuntimeContractError):
            runtime_module.validate_dynamic_ci_report(
                {**report, "runtime_source_envelope_digest": ZERO_SHA256}
            )
        with self.assertRaises(RuntimeContractError):
            runtime_module.validate_dynamic_ci_report(
                {**report, "discarded_attempts_sha256": "1" * 64}
            )

    def test_dynamic_feedback_cursor_ignores_timestamp_noise_and_never_auto_acks(self) -> None:
        item = {
            "kind": "issue_comment",
            "id": "1",
            "author_login": "reviewer",
            "created_at": "2026-08-20T10:00:00Z",
            "updated_at": "2026-08-20T10:00:01Z",
            "body_bytes": 4,
            "body_sha256": ZERO_SHA256,
        }
        cursor = runtime_module.make_dynamic_pr_feedback_cursor(
            repository="owner/repository",
            pull_number=1,
            items=[item],
            review_decision="CHANGES_REQUESTED",
            checks=[
                dynamic_check(
                    workflow_name="",
                    event="",
                    link="https://app.aikido.dev/featurebranch/scan/200235380?groupId=1019",
                )
            ],
        )
        self.assertEqual(
            runtime_module.validate_dynamic_pr_feedback_cursor(cursor),
            cursor,
        )
        self.assertEqual(
            runtime_module.validate_dynamic_pr_feedback_cursor(
                canonical_bytes(cursor).decode("utf-8")
            ),
            cursor,
        )
        timestamp_only = runtime_module.make_dynamic_pr_feedback_cursor(
            repository="owner/repository",
            pull_number=1,
            items=[{**item, "updated_at": "2026-08-21T10:00:01Z"}],
            review_decision="CHANGES_REQUESTED",
        )
        self.assertEqual(
            runtime_module.diff_dynamic_pr_feedback_cursor(
                cursor,
                timestamp_only,
            )["transition"],
            "still_waiting",
        )
        edited = runtime_module.make_dynamic_pr_feedback_cursor(
            repository="owner/repository",
            pull_number=1,
            items=[{**item, "body_bytes": 5, "body_sha256": "1" * 64}],
            review_decision="CHANGES_REQUESTED",
        )
        diff = runtime_module.diff_dynamic_pr_feedback_cursor(cursor, edited)
        self.assertEqual(diff["transition"], "state_changed")
        self.assertEqual(diff["observed_cursor"], edited)
        self.assertNotIn("acknowledged", diff)
        review_changed = runtime_module.make_dynamic_pr_feedback_cursor(
            repository="owner/repository",
            pull_number=1,
            items=[item],
            review_decision="APPROVED",
        )
        self.assertEqual(
            runtime_module.diff_dynamic_pr_feedback_cursor(
                cursor,
                review_changed,
            )["transition"],
            "state_changed",
        )
        with self.assertRaises(RuntimeContractError):
            runtime_module.diff_dynamic_pr_feedback_cursor(
                cursor,
                {**edited, "repository": "other/repository"},
            )
        draft_review = {
            "kind": "review",
            "id": "draft-1",
            "author_login": None,
            "state": "COMMENTED",
            "submitted_at": None,
            "commit_oid": None,
            "body_bytes": 0,
            "body_sha256": ZERO_SHA256,
        }
        draft_cursor = runtime_module.make_dynamic_pr_feedback_cursor(
            repository="owner/repository",
            pull_number=1,
            items=[draft_review],
        )
        self.assertEqual(draft_cursor["item_count"], 1)

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

        # Retained v2 archives can contain either bounded rejection form.
        for code, count, digest in (
            ("observation_limit", 10001, "b" * 64),
            ("hard_limit", 0, canonical_sha256([])),
        ):
            attempt = {
                **ci_attempt(reason="report_invalid"),
                "expected_checks": [],
                "unexpected_check_count": count,
                "unexpected_checks_sha256": digest,
                "safe_error": {
                    "code": code, "exit_code": None,
                    "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                    "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                },
            }
            with self.subTest(code=code):
                archive = {**ci_report(), "attempts": [attempt]}
                self.assertEqual(validate_ci_archive(archive), archive)

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
            hashlib.sha256(
                b"github-ci-discarded-v1\0" + bytes(32) + canonical_bytes(attempt)
            ).hexdigest(),
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
            bounded["discarded_attempts_sha256"], first_digest,
        )
        self.assertEqual(bounded["attempts"], attempts[1:])
        self.assertEqual(validate_ci_archive(bounded), bounded)
        malformed_attempts = [*attempts[:-1], {**attempts[-1], "unknown": True}]
        with self.assertRaises(RuntimeContractError):
            build_ci_report(
                mode="expected-v1", repository="owner/repository", pull_number=1,
                runtime_source_envelope_digest=ZERO_SHA256,
                expected_ci_checks_sha256=expected_ci_checks_sha256(expected_checks()),
                attempts=malformed_attempts,
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
        self.assertEqual(validate_ci_archive(invalid), invalid)
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
        self.assertEqual(validate_ci_archive(duplicate_report), duplicate_report)
        self.assertEqual(
            validate_ci_report(
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
            )["attempts"][0]["safe_error"]["code"],
            "report_invalid",
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
