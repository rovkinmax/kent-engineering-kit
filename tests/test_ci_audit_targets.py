from __future__ import annotations

import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from workflowkit import runtime
from workflowkit import ci_contract


ROOT = Path(__file__).resolve().parents[1]


def load_template(path: Path, name: str):
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class TargetBranchContinuityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        (self.workspace / ".kent").mkdir()
        (self.workspace / ".kent/workflow-profile.toml").write_text(
            "[capabilities]\nci_monitoring = false\n"
        )

    def _pr(self, *, state: str, base_name: str) -> dict[str, object]:
        return {
            "url": "https://github.com/owner/repository/pull/17",
            "state": state,
            "headRefName": "TASK-17",
            "headRefOid": "a" * 40,
            "baseRefName": base_name,
            "baseRefOid": "b" * 40,
            "mergedAt": (
                "2026-10-22T12:00:00Z" if state == "MERGED" else None
            ),
            "mergeCommit": {"oid": "c" * 40} if state == "MERGED" else None,
            "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
        }

    def _run(
        self,
        source: str,
        payload: dict[str, object],
        pr: dict[str, object],
        *,
        observe_pr=None,
        max_errors: int = 1,
        interval_seconds: int = 30,
        sleep_calls=None,
        runtime_module=runtime,
        native_protocol=None,
    ):
        from workflowkit.github_observation import provider_retry_constraint

        module = load_template(ROOT / "templates/project" / source, source.replace("-", "_"))
        captured = io.BytesIO()
        watcher = types.SimpleNamespace(
            observe_pr=observe_pr or (lambda _url: {
                "pr": pr,
                "checks": [],
                "feedback_items": [],
            })
        )
        sleep_calls = sleep_calls if sleep_calls is not None else mock.Mock()

        class ObservationError(RuntimeError):
            code = "invalid_observation"

        observation = types.SimpleNamespace(
            GitHubObserver=lambda *_args, **_kwargs: watcher,
            ObservationError=ObservationError,
            parse_pr_url=lambda _url: ("owner/repository", 17),
            provider_retry_constraint=provider_retry_constraint,
        )
        checkpoint = types.SimpleNamespace(
            current_script_delivery_protocol=lambda *_args, **_kwargs: native_protocol,
            require_github_provider_read=lambda *_args: None,
            is_github_provider_read_blocked=lambda _error: False,
        )
        with (
            mock.patch.object(module, "runtime_contracts", return_value=runtime_module),
            mock.patch.object(module, "github_observation", return_value=observation),
            mock.patch.object(module, "_sibling", return_value=checkpoint),
            mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(
                module.sys,
                "stdout",
                types.SimpleNamespace(buffer=captured),
            ),
            mock.patch.object(module.Path, "cwd", return_value=self.workspace),
            mock.patch.object(module.time, "sleep", sleep_calls),
            mock.patch.dict(os.environ, {
                "KENT_CI_WATCH_TEST_MODE": "1",
                "KENT_CI_WATCH_MAX_POLLS": "1",
                "KENT_CI_WATCH_MAX_ERRORS": str(max_errors),
                "KENT_CI_WATCH_INTERVAL_SECONDS": str(interval_seconds),
                "KENT_PR_WATCH_TEST_MODE": "1",
                "KENT_PR_WATCH_MAX_POLLS": "1",
                "KENT_PR_WATCH_MAX_ERRORS": str(max_errors),
                "KENT_PR_WATCH_INTERVAL_SECONDS": str(interval_seconds),
            }),
        ):
            self.assertEqual(module.main(), 0)
        return json.loads(captured.getvalue())

    def _payload(self, *, with_ci: bool) -> dict[str, object]:
        value: dict[str, object] = {
            "workspace_path": str(self.workspace),
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "TASK-17",
            "merge_strategy": "squash",
            "pr_base_branch": "main",
            "pr_head_oid": "a" * 40,
            "pr_base_oid": "b" * 40,
        }
        if with_ci:
            contract = {
                "schema": "github-ci-contract-v1",
                "repository": "owner/repository",
                "pull_number": 17,
                "head_oid": "a" * 40,
                "base_oid": "b" * 40,
                "require_ci": True,
            }
            value["ci_contract"] = runtime.canonical_bytes(contract).decode()
        return value

    def test_ci_and_ci_off_pr_watchers_reject_merged_wrong_target(self) -> None:
        cases = (
            ("workflow-wait-github-ci", True, "ci_watch_state_changed"),
            ("workflow-wait-github-pr", False, "merge_watch_state_changed"),
        )
        for script, with_ci, transition in cases:
            with self.subTest(script=script):
                result = self._run(
                    script,
                    self._payload(with_ci=with_ci),
                    self._pr(state="MERGED", base_name="release"),
                )
                self.assertEqual(result["transition"], transition)
                self.assertEqual(result["pr_base_branch"], "main")
                self.assertNotIn("merge_report", result)
                diagnostic = json.loads(result["pr_report"])
                self.assertEqual(
                    diagnostic["expected_pr_base_branch"],
                    "main",
                )
                self.assertEqual(
                    diagnostic["observed_base_ref_name"],
                    "release",
                )

    def test_first_observation_recovery_has_context_without_fake_report(self) -> None:
        (self.workspace / ".kent/workflow-profile.toml").write_text(
            'schema_version=4\nkit_managed_commands=["runtime_contracts","prepare_ci"]\n'
            '[command_versions]\nruntime_contracts="2.0.0"\nprepare_ci="2.0.0"\n'
            '[capabilities]\nci_monitoring=true\n'
        )
        for reason in ("retarget", "head", "base"):
            with self.subTest(reason=reason):
                pr = self._pr(state="OPEN", base_name="release" if reason == "retarget" else "main")
                if reason in {"head", "base"}:
                    pr["headRefOid" if reason == "head" else "baseRefOid"] = "d" * 40
                result = self._run("workflow-wait-github-ci", self._payload(with_ci=True), pr)
                context = runtime.validate_delivery_context(json.loads(result["delivery_context"]))
                self.assertEqual(context["schema"], "workflow-delivery-context-v1")
                self.assertNotIn("pr_base_branch", context)
                self.assertEqual(result["pr_base_branch"], "main")
                self.assertEqual(context["pr_feedback_cursor"], "uninitialized")
                self.assertNotIn("ci_report", context)
                self.assertNotIn("ci_report", result)

    def test_native_source_ci_schema_is_not_inferred_from_flat_target(self) -> None:
        (self.workspace / ".kent/workflow-profile.toml").write_text(
            '[command_versions]\nruntime_contracts="2.2.0"\nprepare_ci="3.1.0"\n'
            '[capabilities]\nci_monitoring=true\n'
        )
        for schema in ("workflow-delivery-context-v1", "workflow-delivery-context-v2"):
            with self.subTest(schema=schema):
                result = self._run(
                    "workflow-wait-github-ci", self._payload(with_ci=True),
                    self._pr(state="OPEN", base_name="release"),
                    native_protocol={
                        "protocol": "typed-v1", "node_key": "ci_watch",
                        "identity": {"task_id": "native-task"}, "target_required": True,
                        "context_schema": schema,
                    },
                )
                context = runtime.validate_delivery_context(json.loads(result["delivery_context"]))
                self.assertEqual(context["schema"], schema)
                self.assertEqual("pr_base_branch" in context, schema.endswith("-v2"))
                self.assertEqual(result["pr_base_branch"], "main")

    def test_non_source_ci_family_keeps_flat_recovery_stdout(self) -> None:
        result = self._run(
            "workflow-wait-github-ci", self._payload(with_ci=True),
            self._pr(state="OPEN", base_name="release"),
        )
        self.assertNotIn("delivery_context", result)
        self.assertNotIn("ci_report", result)

    def test_legacy_source_ci_with_actual_v1_only_runtime(self) -> None:
        path = os.environ.get("KENT_LEGACY_CI_RUNTIME")
        if not path:
            self.skipTest("explicit legacy source-CI runtime binding is unavailable")
        legacy = load_template(Path(path), "actual_legacy_ci_runtime")
        (self.workspace / ".kent/workflow-profile.toml").write_text(
            'schema_version=4\nkit_managed_commands=["runtime_contracts","prepare_ci"]\n'
            '[command_versions]\nruntime_contracts="2.0.0"\nprepare_ci="2.0.0"\n'
            '[capabilities]\nci_monitoring=true\n'
        )
        result = self._run(
            "workflow-wait-github-ci", self._payload(with_ci=True),
            self._pr(state="OPEN", base_name="release"), runtime_module=legacy,
        )
        context = legacy.validate_delivery_context(json.loads(result["delivery_context"]))
        self.assertEqual(context["schema"], "workflow-delivery-context-v1")
        self.assertNotIn("pr_base_branch", context)
        self.assertEqual(result["pr_base_branch"], "main")
        self.assertNotIn("ci_report", context)
        # This is a genuinely older consumer, not the modern dual-schema parser.
        with self.assertRaises(ValueError):
            legacy.validate_delivery_context({
                **context, "schema": "workflow-delivery-context-v2", "pr_base_branch": "main",
            })

    def test_matching_target_is_preserved_on_merged_output(self) -> None:
        for script, with_ci, transition in (
            ("workflow-wait-github-ci", True, "ci_watch_pr_merged"),
            ("workflow-wait-github-pr", False, "merge_watch_pr_merged"),
        ):
            with self.subTest(script=script):
                result = self._run(
                    script,
                    self._payload(with_ci=with_ci),
                    self._pr(state="MERGED", base_name="main"),
                )
                self.assertEqual(result["transition"], transition)
                self.assertEqual(result["pr_base_branch"], "main")
                self.assertIn("merge_report", result)
                self.assertNotIn("pr_report", result)

    def test_both_waiters_honor_provider_delay_floor_and_retry_budget(self) -> None:
        from workflowkit.github_observation import ObservationError

        merged = {
            "pr": self._pr(state="MERGED", base_name="main"),
            "checks": [],
            "feedback_items": [],
        }
        cases = (
            ("workflow-wait-github-ci", True, "ci_watch_pr_merged"),
            ("workflow-wait-github-pr", False, "merge_watch_pr_merged"),
        )
        for script, with_ci, transition in cases:
            for interval, expected_delay in ((30, 120), (180, 180)):
                with self.subTest(script=script, interval=interval):
                    observer = mock.Mock(side_effect=[
                        ObservationError(
                            "query_failed",
                            rate_limited=True,
                            retry_after_seconds=120,
                        ),
                        merged,
                    ])
                    sleep = mock.Mock()
                    result = self._run(
                        script,
                        self._payload(with_ci=with_ci),
                        self._pr(state="OPEN", base_name="main"),
                        observe_pr=observer,
                        max_errors=2,
                        interval_seconds=interval,
                        sleep_calls=sleep,
                    )
                    self.assertEqual(result["transition"], transition)
                    self.assertEqual(observer.call_count, 2)
                    sleep.assert_called_once_with(expected_delay)

        for script, with_ci, transition in cases:
            with self.subTest(script=script, case="unqualified"):
                observer = mock.Mock(side_effect=[
                    ObservationError(
                        "observation_limit",
                        provider_retry_unknown=True,
                    ),
                ])
                sleep = mock.Mock()
                result = self._run(
                    script,
                    self._payload(with_ci=with_ci),
                    self._pr(state="OPEN", base_name="main"),
                    observe_pr=observer,
                    max_errors=2,
                    interval_seconds=30,
                    sleep_calls=sleep,
                )
                self.assertEqual(
                    result["transition"],
                    "ci_watch_failed" if with_ci else "merge_watch_state_changed",
                )
                self.assertEqual(observer.call_count, 1)
                sleep.assert_not_called()

        for script, with_ci, transition in cases:
            with self.subTest(script=script, case="ordinary-error-interval"):
                observer = mock.Mock(side_effect=[
                    ObservationError("query_failed"),
                    merged,
                ])
                sleep = mock.Mock()
                result = self._run(
                    script,
                    self._payload(with_ci=with_ci),
                    self._pr(state="OPEN", base_name="main"),
                    observe_pr=observer,
                    max_errors=2,
                    interval_seconds=30,
                    sleep_calls=sleep,
                )
                self.assertEqual(result["transition"], transition)
                self.assertEqual(observer.call_count, 2)
                sleep.assert_called_once_with(30)

    def test_both_waiters_stop_on_actual_unknown_provider_transport_metadata(self) -> None:
        from workflowkit.github_observation import ObservationError, read_json

        def transport_error(stderr: bytes) -> ObservationError:
            command = (
                "import sys; "
                "sys.stderr.buffer.write(bytes.fromhex(sys.argv[1])); "
                "sys.exit(1)"
            )
            with self.assertRaises(ObservationError) as raised:
                read_json(
                    sys.executable,
                    self.workspace,
                    ["-c", command, stderr.hex()],
                    capture_provider_retry=True,
                )
            return raised.exception

        primary_invalid_reset = b"\n".join(
            (
                b"* Request at 2026-10-22 00:00:00 +0000 UTC",
                b"* Request to https://api.github.com/graphql",
                b"< HTTP/2.0 403 response",
                b"< X-RateLimit-Remaining: 0",
                b"< X-RateLimit-Reset: not-an-epoch",
                b"< Date: invalid date",
                b"",
                b'{"message":"API rate limit exceeded"}',
                b"* Request took 0.001s",
                b"",
            )
        )
        cases = (
            ("absent-response-frame", b"provider transport failed\n"),
            ("primary-invalid-reset", primary_invalid_reset),
        )
        watchers = (
            ("workflow-wait-github-ci", True, "ci_watch_failed"),
            ("workflow-wait-github-pr", False, "merge_watch_state_changed"),
        )
        for case, stderr in cases:
            error = transport_error(stderr)
            self.assertEqual(error.code, "query_failed")
            self.assertTrue(error.provider_retry_unknown)
            self.assertFalse(error.rate_limited)
            for script, with_ci, transition in watchers:
                with self.subTest(case=case, script=script):
                    observer = mock.Mock(side_effect=error)
                    sleep = mock.Mock()
                    result = self._run(
                        script,
                        self._payload(with_ci=with_ci),
                        self._pr(state="OPEN", base_name="main"),
                        observe_pr=observer,
                        max_errors=2,
                        interval_seconds=30,
                        sleep_calls=sleep,
                    )
                    self.assertEqual(result["transition"], transition)
                    self.assertEqual(observer.call_count, 1)
                    sleep.assert_not_called()

    def test_exhausted_retry_budget_makes_no_extra_observation_or_sleep(self) -> None:
        from workflowkit.github_observation import ObservationError

        for script, with_ci in (
            ("workflow-wait-github-ci", True),
            ("workflow-wait-github-pr", False),
        ):
            with self.subTest(script=script):
                observer = mock.Mock(side_effect=ObservationError(
                    "query_failed",
                    stdout=b"SECRET stdout",
                    stderr=b"SECRET debug response",
                    rate_limited=True,
                    retry_after_seconds=120,
                ))
                sleep = mock.Mock()
                result = self._run(
                    script,
                    self._payload(with_ci=with_ci),
                    self._pr(state="OPEN", base_name="main"),
                    observe_pr=observer,
                    max_errors=1,
                    interval_seconds=30,
                    sleep_calls=sleep,
                )
                self.assertEqual(observer.call_count, 1)
                sleep.assert_not_called()
                encoded = json.dumps(result)
                self.assertNotIn("SECRET stdout", encoded)
                self.assertNotIn("SECRET debug response", encoded)
                self.assertNotIn("Retry-After", encoded)

    def test_prepare_checks_expected_target_before_both_merged_shortcuts(self) -> None:
        payload = {
            "workspace_path": str(self.workspace),
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "TASK-17",
            "merge_strategy": "squash",
            "task_short_id": "TASK-17",
            "pr_base_branch": "main",
        }
        merged_wrong = self._pr(state="MERGED", base_name="release")
        for observations, read_count in (
            ([merged_wrong], 1),
            ([self._pr(state="OPEN", base_name="main"), merged_wrong], 2),
        ):
            with (
                self.subTest(read_count=read_count),
                mock.patch.object(
                    ci_contract,
                    "read_pull_request",
                    side_effect=observations,
                ) as read,
                self.assertRaisesRegex(
                    ci_contract.CiContractError,
                    "target_branch_changed",
                ),
            ):
                ci_contract.prepare_ci_payload(payload, gh_bin="/bin/true")
            self.assertEqual(read.call_count, read_count)
        opened = self._pr(state="OPEN", base_name="main")
        with mock.patch.object(
            ci_contract,
            "read_pull_request",
            side_effect=[opened, opened],
        ):
            ready = ci_contract.prepare_ci_payload(payload, gh_bin="/bin/true")
        self.assertEqual(ready["transition"], "ci_prepare_ready_initial")
        self.assertEqual(ready["pr_base_branch"], "main")

    def test_legacy_preparation_failure_preserves_separate_target_carrier(self) -> None:
        context = {
            "schema": "workflow-delivery-context-v2",
            "phase": "post_pr",
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "TASK-17",
            "pr_base_branch": "main",
            "merge_strategy": "squash",
            "pr_feedback_cursor": "uninitialized",
        }
        failure = ci_contract.preparation_failure({
            "workspace_path": str(self.workspace),
            "task_short_id": "TASK-17",
            "delivery_context": runtime.canonical_bytes(context).decode(),
        })
        self.assertEqual(failure["transition"], "ci_prepare_failed")
        self.assertEqual(failure["pr_base_branch"], "main")
        self.assertEqual(failure["ci_contract"], "")
        self.assertEqual(failure["ci_report"], "")
        self.assertNotIn("pr_base_branch", failure["ci_contract"])

    def test_legacy_watcher_archive_forwards_native_context_for_graph_selection(self) -> None:
        module = load_template(
            ROOT / "templates/project/workflow-wait-github-ci",
            "legacy_ci_archive_context",
        )
        native_context = {
            "task_id": "native-task-id",
            "node_id": "native-ci-watch",
            "transition_branch_key": "wait-ci",
        }
        observation = types.SimpleNamespace(
            read_json=mock.Mock(
                return_value={"ci_report_artifact": ".kent/runtime/TASK-17/report.json"}
            )
        )
        with mock.patch.object(module, "runtime_contracts", return_value=runtime):
            module._archive(
                observation,
                {
                    "workspace_path": str(self.workspace),
                    "pr_url": "https://github.com/owner/repository/pull/17",
                    "branch_name": "TASK-17",
                    "merge_strategy": "squash",
                    "task_short_id": "TASK-17",
                },
                {"report": "legacy protocol report"},
                native_context=native_context,
            )
        request = json.loads(
            observation.read_json.call_args.kwargs["input_bytes"]
        )
        self.assertEqual(request["operation"], "archive_ci_report")
        self.assertEqual(request["_kent"], native_context)

    def test_preparation_retry_uses_explicit_archiver_without_legacy_fallback(self) -> None:
        contract = {
            "schema": "github-ci-contract-v1",
            "repository": "owner/repository",
            "pull_number": 17,
            "head_oid": "a" * 40,
            "base_oid": "b" * 40,
            "require_ci": True,
        }
        report = runtime.build_dynamic_ci_report(
            contract=contract,
            attempts=[{
                "sequence": 1,
                "head_oid": contract["head_oid"],
                "base_oid": contract["base_oid"],
                "retry": None,
                "checks": [],
                "check_count": 0,
                "checks_sha256": runtime.canonical_sha256([]),
                "reason": "no_checks",
                "watcher_exit_code": 0,
                "error": None,
            }],
        )
        payload = {
            "workspace_path": str(self.workspace),
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "TASK-17",
            "merge_strategy": "squash",
            "task_short_id": "TASK-17",
            "pr_base_branch": "main",
            "ci_contract": runtime.canonical_bytes(contract).decode(),
            "ci_report": runtime.canonical_bytes(report).decode(),
            "pr_feedback_cursor": "uninitialized",
        }
        pr = self._pr(state="OPEN", base_name="main")
        archiver = mock.Mock(return_value="typed-artifact")
        with (
            mock.patch.object(
                ci_contract,
                "read_pull_request",
                side_effect=[pr, pr],
            ),
            mock.patch.object(ci_contract, "archive_ci_report") as legacy,
        ):
            result = ci_contract.prepare_ci_payload(
                payload,
                gh_bin="/bin/true",
                report_archiver=archiver,
            )
        self.assertEqual(result["transition"], "ci_prepare_ready_retry")
        archiver.assert_called_once_with(self.workspace, "TASK-17", report)
        legacy.assert_not_called()

    def test_delivery_context_v2_preserves_and_cross_checks_base_branch(self) -> None:
        context = {
            "schema": runtime.DELIVERY_CONTEXT_SCHEMA,
            "phase": "post_pr",
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "TASK-17",
            "pr_base_branch": "main",
            "merge_strategy": "squash",
            "pr_feedback_cursor": "uninitialized",
        }
        payload = {
            "workspace_path": str(self.workspace),
            "task_short_id": "TASK-17",
            "delivery_context": json.dumps(context),
            "pr_base_branch": "main",
        }
        unwrapped = ci_contract.unwrap_delivery_context_payload(payload)
        self.assertEqual(unwrapped["pr_base_branch"], "main")
        with self.assertRaisesRegex(
            ci_contract.CiContractError,
            "delivery_context_conflict",
        ):
            ci_contract.unwrap_delivery_context_payload({
                **payload,
                "pr_base_branch": "release",
            })

    def test_typed_archive_qualifies_then_calls_ledger_in_process(self) -> None:
        contract = {
            "schema": "github-ci-contract-v1",
            "repository": "owner/repository",
            "pull_number": 17,
            "head_oid": "a" * 40,
            "base_oid": "b" * 40,
            "require_ci": True,
        }
        check = {
            "workflow_name": "Build",
            "check_name": "unit",
            "event": "pull_request",
            "bucket": "pass",
            "state": "SUCCESS",
            "link": None,
        }
        report = runtime.build_dynamic_ci_report(
            contract=contract,
            attempts=[{
                "sequence": 1,
                "head_oid": contract["head_oid"],
                "base_oid": contract["base_oid"],
                "retry": None,
                "checks": [check],
                "check_count": 1,
                "checks_sha256": runtime.canonical_sha256([check]),
                "reason": "green",
                "watcher_exit_code": 0,
                "error": None,
            }],
        )
        native_context = {
            "task_id": "native-task-id",
            "node_id": "ci_watch",
            "transition_branch_key": "wait-ci",
        }
        payload = {
            "operation": "archive_script_ci_report",
            "workspace_path": str(self.workspace),
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "TASK-17",
            "merge_strategy": "squash",
            "task_short_id": "TASK-17",
            "ci_report": runtime.canonical_bytes(report).decode(),
            "_kent": native_context,
        }
        artifact = (
            f".kent/runtime/TASK-17/ci-report-"
            f"{hashlib.sha256(runtime.canonical_bytes(report)).hexdigest()}.json"
        )
        proof = {
            "schema": "script-ci-archive-reader-proof-v1",
            "reader_proof_sha256": "d" * 64,
            "native_script_identity": {
                "task_id": "native-task-id",
                "task_short_id": "TASK-17",
                "project_id": "native-project-id",
                "workspace_path": str(self.workspace),
                "invocation_kind": "script",
                "node_id": "ci_watch",
                "transition_branch_key": "wait-ci",
            },
        }
        ledger_source = (
            "def archive_script_ci_report(root, *, task, _payload=None, "
            "_reader_resolver=None, _initial_reader_proof=None):\n"
            "    assert task == 'TASK-17'\n"
            "    assert _payload['schema'] == 'script-ci-archive-request-v1'\n"
            "    assert _payload['native_context']['task_id'] == 'native-task-id'\n"
            "    assert _initial_reader_proof['reader_proof_sha256'] == '" + "d" * 64 + "'\n"
            "    assert _reader_resolver(root, task, _payload['native_context']) == _initial_reader_proof\n"
            "    return {'ci_report_artifact': '" + artifact + "'}\n"
        ).encode()
        native_read = mock.Mock()
        with (
            mock.patch.object(ci_contract, "_native_read_adapter", return_value=native_read),
            mock.patch.object(
                ci_contract,
                "_archive_reader_snapshot",
                return_value=(proof, {
                    "project:.kent/scripts/workflow-evidence-ledger": ledger_source,
                    "project:.kent/scripts/workflow_runtime_contracts.py": b"# runtime\n",
                }),
            ),
            mock.patch.object(
                ci_contract,
                "resolve_script_ci_archive_readers",
                return_value=proof,
            ) as resolve,
            mock.patch.object(ci_contract.Path, "cwd", return_value=self.workspace),
        ):
            self.assertEqual(
                ci_contract.archive_script_ci_report(
                    payload,
                    evidence_command=".kent/scripts/workflow-evidence-ledger",
                ),
                artifact,
            )
        resolve.assert_called_once_with(
            self.workspace,
            "TASK-17",
            native_context,
            native_read=native_read,
        )

    def test_typed_archive_failure_is_blocked_without_legacy_fallback(self) -> None:
        contract = {
            "schema": "github-ci-contract-v1",
            "repository": "owner/repository",
            "pull_number": 17,
            "head_oid": "a" * 40,
            "base_oid": "b" * 40,
            "require_ci": True,
        }
        report = runtime.build_dynamic_ci_report(
            contract=contract,
            attempts=[{
                "sequence": 1,
                "head_oid": contract["head_oid"],
                "base_oid": contract["base_oid"],
                "retry": None,
                "checks": [],
                "check_count": 0,
                "checks_sha256": runtime.canonical_sha256([]),
                "reason": "no_checks",
                "watcher_exit_code": 0,
                "error": None,
            }],
        )
        payload = {
            "workspace_path": str(self.workspace),
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "TASK-17",
            "merge_strategy": "squash",
            "task_short_id": "TASK-17",
            "ci_report": runtime.canonical_bytes(report).decode(),
            "_kent": {"task_id": "native-task-id", "node_id": "ci_watch"},
        }
        native_read = mock.Mock()
        with (
            mock.patch.object(ci_contract, "_native_read_adapter", return_value=native_read),
            mock.patch.object(
                ci_contract,
                "_archive_reader_snapshot",
                side_effect=ci_contract.CiContractError("archive_reader_source_mismatch"),
            ),
            mock.patch.object(ci_contract, "archive_ci_report") as legacy,
            self.assertRaisesRegex(ci_contract.CiContractError, "archive_blocked"),
        ):
            ci_contract.archive_script_ci_report(
                payload,
                evidence_command=".kent/scripts/workflow-evidence-ledger",
            )
        legacy.assert_not_called()

    def test_typed_preparation_failure_binds_original_string_inputs(self) -> None:
        context = {
            "schema": "workflow-delivery-context-v2",
            "phase": "post_pr",
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "TASK-17",
            "pr_base_branch": "main",
            "merge_strategy": "squash",
            "pr_feedback_cursor": "uninitialized",
        }
        raw_context = " " + json.dumps(context) + " "
        original = {
            "workspace_path": str(self.workspace),
            "task_short_id": "TASK-17",
            "delivery_context": raw_context,
            "_kent": {"task_id": "native-task-id", "node_id": "ci_prepare"},
        }
        with mock.patch.object(ci_contract.Path, "cwd", return_value=self.workspace):
            output = ci_contract.typed_preparation_failure(
                original,
                ci_contract.CiContractError("target_branch_changed"),
            )
        self.assertEqual(
            set(output),
            {
                "transition",
                "workspace_path",
                "task_short_id",
                "ci_preparation_diagnostic",
            },
        )
        self.assertNotIn("ci_report", output)
        diagnostic = json.loads(output["ci_preparation_diagnostic"])
        self.assertEqual(diagnostic["schema"], "workflow-ci-preparation-diagnostic-v1")
        self.assertEqual(diagnostic["classification"], "identity_conflict")
        self.assertEqual(diagnostic["code"], "target_branch_changed")
        self.assertEqual(
            diagnostic["input_strings_sha256"],
            hashlib.sha256(runtime.canonical_bytes({
                key: value for key, value in original.items()
                if isinstance(key, str) and isinstance(value, str)
            })).hexdigest(),
        )
        self.assertEqual(diagnostic["resume_context"], raw_context)

    def test_typed_preparation_failure_omits_invalid_resume_packets(self) -> None:
        context = {
            "schema": "workflow-delivery-context-v1",
            "phase": "post_pr",
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "TASK-17",
            "merge_strategy": "squash",
            "pr_feedback_cursor": "uninitialized",
            "ci_report": '{"schema":"wrong"}',
        }
        original = {
            "workspace_path": str(self.workspace),
            "task_short_id": "TASK-17",
            "delivery_context": json.dumps(context),
        }
        with mock.patch.object(ci_contract.Path, "cwd", return_value=self.workspace):
            output = ci_contract.typed_preparation_failure(
                original,
                ci_contract.CiContractError("previous CI report is invalid"),
            )
        diagnostic = json.loads(output["ci_preparation_diagnostic"])
        self.assertEqual(diagnostic["classification"], "invalid_input")
        self.assertEqual(diagnostic["code"], "previous_ci_packet_invalid")
        self.assertNotIn("resume_context", diagnostic)
        self.assertNotIn("ci_report", output)

    def test_invalid_target_is_not_misclassified_as_identity_conflict(self) -> None:
        original = {
            "workspace_path": str(self.workspace),
            "task_short_id": "TASK-17",
            "pr_base_branch": "",
        }
        with mock.patch.object(ci_contract.Path, "cwd", return_value=self.workspace):
            output = ci_contract.typed_preparation_failure(
                original,
                ci_contract.CiContractError("target_branch_invalid"),
            )
        diagnostic = json.loads(output["ci_preparation_diagnostic"])
        self.assertEqual(diagnostic["code"], "delivery_context_invalid")
        self.assertEqual(diagnostic["classification"], "invalid_input")


class ProviderRetryCiPreparationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()

    def test_read_pull_request_guard_runs_before_child_and_sets_scoped_debug(self):
        child = self.workspace / "fake-gh"
        child.write_text(
            "#!/bin/sh\n"
            'test "$GH_DEBUG" = "api" || exit 41\n'
            "printf '%s\\n' '< HTTP/2.0 429' >&2\n"
            "printf '%s\\n' '< Retry-After: 120' >&2\n"
            "printf '\\n' >&2\n"
            "printf '%s\\n' '< {\"message\":\"API rate limit exceeded\"}' >&2\n"
            "printf '%s\\n' '* Request took 41ms' >&2\n"
            "printf '%s' 'secret stdout'\n"
            "exit 1\n",
            encoding="utf-8",
        )
        child.chmod(0o755)
        blocked = RuntimeError("inherited provider read blocked")
        with mock.patch.object(ci_contract.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(
                RuntimeError,
                "inherited provider read blocked",
            ):
                ci_contract.read_pull_request(
                    self.workspace,
                    "https://github.com/owner/repository/pull/17",
                    gh_bin=str(child),
                    provider_guard=lambda: (_ for _ in ()).throw(blocked),
                )
        popen.assert_not_called()

        from workflowkit.github_observation import (
            ObservationError,
            provider_retry_constraint,
        )

        with (
            mock.patch.object(
                ci_contract.time,
                "time_ns",
                return_value=1_000_000_000_000,
            ),
            self.assertRaises(ObservationError) as raised,
        ):
            ci_contract.read_pull_request(
                self.workspace,
                "https://github.com/owner/repository/pull/17",
                gh_bin=str(child),
                provider_guard=mock.Mock(),
            )
        error = raised.exception
        self.assertEqual(
            provider_retry_constraint(
                error,
                interval=30,
                failure_count=1,
            ),
            '{"kind":"not_before","not_before_unix_ms":1120000,'
            '"provider_host":"github.com","schema":"github-provider-retry-v1"}',
        )
        self.assertNotIn("secret stdout", repr(error.as_dict()))
        self.assertNotIn("Retry-After", repr(error.as_dict()))

    def test_prepare_ci_threads_same_guard_to_each_provider_read(self):
        payload = {
            "workspace_path": str(self.workspace),
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "TASK-17",
            "merge_strategy": "squash",
            "task_short_id": "TASK-17",
            "pr_base_branch": "main",
        }
        pr = TargetBranchContinuityTest()._pr(state="OPEN", base_name="main")
        guard = mock.Mock()
        with mock.patch.object(
            ci_contract,
            "read_pull_request",
            side_effect=[pr, pr],
        ) as read:
            ci_contract.prepare_ci_payload(
                payload,
                gh_bin="/bin/true",
                provider_guard=guard,
            )
        self.assertEqual(read.call_count, 2)
        self.assertTrue(
            all(call.kwargs["provider_guard"] is guard
                for call in read.call_args_list)
        )

    def test_typed_prepare_guard_denial_uses_existing_safe_failure_carrier(self):
        module = load_template(
            ROOT / "scripts/prepare-github-ci",
            "prepare_ci_guard_denial",
        )
        scripts = self.workspace / ".kent/scripts"
        scripts.mkdir(parents=True)
        (self.workspace / ".kent/workflow-profile.toml").write_text(
            '[policies]\ndelivery_reliability = "v1"\n',
            encoding="utf-8",
        )
        checkpoint = scripts / "workflow-checkpoint"
        checkpoint.write_text(
            "def emit_producer_result(root, payload, result, producer_kind):\n"
            "    return result\n",
            encoding="utf-8",
        )
        identity = {
            "task_id": "native-task-id",
            "task_short_id": "TASK-17",
            "project_id": "native-project-id",
            "workspace_path": str(self.workspace),
            "invocation_kind": "script",
            "node_id": "native-ci-prepare",
            "transition_branch_key": None,
        }
        observed = types.SimpleNamespace(
            identity=identity,
            graph={"graph": {"nodes": [{
                "id": "native-ci-prepare",
                "key": "ci_prepare",
            }]}},
        )
        payload = {
            "workspace_path": str(self.workspace),
            "task_short_id": "TASK-17",
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "TASK-17",
            "merge_strategy": "squash",
            "pr_base_branch": "main",
            "_kent": {
                "task_id": "native-task-id",
                "node_id": "native-ci-prepare",
            },
            "commentary": "inherited producer commentary",
        }
        blocked = RuntimeError("github_provider_retry_blocked")
        guard = mock.Mock(side_effect=blocked)
        output = io.BytesIO()

        def prepare(_payload, *, provider_guard, **_kwargs):
            provider_guard()
            self.fail("preparation continued after guard denial")

        with (
            mock.patch.object(
                module,
                "read_preparation_input",
                return_value=payload,
            ),
            mock.patch.object(
                module,
                "observe_script_delivery_protocol",
                return_value=(
                    observed,
                    "typed-v1",
                    True,
                    "delivery-context-v2",
                    mock.Mock(),
                ),
            ),
            mock.patch.object(
                module,
                "github_provider_read_guard",
                return_value=guard,
            ),
            mock.patch.object(module, "prepare_ci_payload", side_effect=prepare),
            mock.patch.object(module.Path, "cwd", return_value=self.workspace),
            mock.patch.object(
                module.sys,
                "stdout",
                types.SimpleNamespace(buffer=output),
            ),
        ):
            self.assertEqual(module.main(), 0)
        result = json.loads(output.getvalue())
        diagnostic = json.loads(result["ci_preparation_diagnostic"])
        self.assertEqual(result["transition"], "ci_prepare_failed")
        self.assertEqual(diagnostic["code"], "github_query_failed")
        self.assertEqual(diagnostic["classification"], "transient")
        self.assertNotIn("github_retry_constraint", result)
        guard.assert_called_once_with()


class PrepareCiGraphProtocolDispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        scripts = self.workspace / ".kent/scripts"
        scripts.mkdir(parents=True)
        (self.workspace / ".kent/workflow-profile.toml").write_text(
            '[policies]\ndelivery_reliability = "v1"\n'
        )
        self.checkpoint = scripts / "workflow-checkpoint"

    def _run_failure(
        self,
        protocol: str,
        observed: object,
        *,
        target_required: bool,
        context_schema: str | None,
    ) -> dict[str, object]:
        module = load_template(
            ROOT / "scripts/prepare-github-ci",
            "prepare_ci_dispatch_" + protocol,
        )
        native_context = {
            "task_id": "native-task-id",
            "node_id": observed.identity["node_id"],
        }
        payload: dict[str, object] = {
            "workspace_path": str(self.workspace),
            "task_short_id": "TASK-17",
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "TASK-17",
            "merge_strategy": "squash",
            "pr_base_branch": "main",
            "_kent": native_context,
        }
        if protocol == "typed-v1":
            expected = {
                "transition",
                "workspace_path",
                "task_short_id",
                "ci_preparation_diagnostic",
            }
        else:
            expected = {
                "transition",
                "workspace_path",
                "pr_url",
                "branch_name",
                "merge_strategy",
                "task_short_id",
                "ci_contract",
                "ci_report",
                "pr_feedback_cursor",
                "pr_base_branch",
            }
        self.checkpoint.write_text(
            "import json\n"
            "def emit_producer_result(root, payload, result, producer_kind):\n"
            "    assert producer_kind == 'prepare-ci'\n"
            "    output = json.loads(result)\n"
            f"    assert set(output) == set({sorted(expected)!r})\n"
            "    output['commentary'] = 'producer-captured'\n"
            "    return json.dumps(output).encode()\n"
        )
        output_stream = io.BytesIO()
        with (
            mock.patch.object(module, "read_preparation_input", return_value=payload),
            mock.patch.object(
                module,
                "observe_script_delivery_protocol",
                return_value=(
                    observed,
                    protocol,
                    target_required,
                    context_schema,
                    mock.Mock(),
                ),
            ),
            mock.patch.object(
                module,
                "prepare_ci_payload",
                side_effect=ci_contract.CiContractError("target_branch_changed"),
            ),
            mock.patch.object(module.Path, "cwd", return_value=self.workspace),
            mock.patch.object(
                module.sys,
                "stdout",
                types.SimpleNamespace(buffer=output_stream),
            ),
        ):
            self.assertEqual(module.main(), 0)
        return json.loads(output_stream.getvalue())

    def test_typed_v1_and_legacy_graphs_emit_their_exact_failure_carriers(self) -> None:
        from dataclasses import replace

        from tests.test_delivery_continuity import runtime_v2_profile
        from workflowkit import delivery
        from workflowkit.delivery import build_delivery_workflow

        def actual_observation(profile):
            profile = replace(profile, project_root=self.workspace)
            spec = build_delivery_workflow(profile, 1)
            node_ids = {
                node.key: "native-" + node.key
                for node in spec.nodes
            }
            nodes = []
            for node in spec.nodes:
                row = {
                    "id": node_ids[node.key],
                    "key": node.key,
                    "kind": node.kind,
                }
                if node.script_path is not None:
                    row["script_path"] = node.script_path
                nodes.append(row)
            groups = {}
            edges = []
            for index, edge in enumerate(spec.edges):
                source = (edge.source, edge.transition)
                group_id = groups.setdefault(
                    source,
                    "group-{}-{}".format(*source),
                )
                edges.append({
                    "id": f"edge-{index}",
                    "transition_group_id": group_id,
                    "target_node_id": node_ids[edge.target],
                    "parameters": [
                        {"key": parameter.key}
                        for parameter in edge.parameters
                    ],
                })
            graph = {
                "workflow_id": "44444444-4444-4444-8444-444444444444",
                "expected_version": 1,
                "graph": {
                    "nodes": nodes,
                    "transition_groups": [
                        {
                            "id": identifier,
                            "source_node_id": node_ids[source],
                            "transition_id": transition,
                        }
                        for (source, transition), identifier in groups.items()
                    ],
                    "edges": edges,
                },
            }
            identity = {"node_id": node_ids["ci_prepare"]}
            context = {
                "task_id": "native-task-id",
                "node_id": identity["node_id"],
            }
            detail = {"summary": {"workflow_id": graph["workflow_id"]}}
            with (
                mock.patch.object(
                    delivery.ProjectProfile,
                    "load",
                    return_value=profile,
                ),
                mock.patch.object(
                    delivery,
                    "observe_script_identity",
                    return_value=(identity, detail, graph),
                ),
            ):
                observed = delivery.observe_script_delivery_graph(
                    self.workspace,
                    "TASK-17",
                    context,
                    native_read=mock.Mock(),
                )
            return (
                observed,
                delivery.prepare_ci_protocol(observed),
                delivery.pr_base_branch_required(observed),
                delivery.delivery_context_schema(observed),
            )

        typed_profile = runtime_v2_profile(
            ci=True,
            source_ci=True,
            current_family=True,
        )
        typed_observed, typed_protocol, typed_target, typed_schema = (
            actual_observation(typed_profile)
        )
        self.assertEqual(typed_protocol, "typed-v1")
        self.assertTrue(typed_target)
        typed = self._run_failure(
            typed_protocol,
            typed_observed,
            target_required=typed_target,
            context_schema=typed_schema,
        )
        self.assertEqual(
            set(typed) - {"commentary"},
            {
                "transition",
                "workspace_path",
                "task_short_id",
                "ci_preparation_diagnostic",
            },
        )
        diagnostic = json.loads(typed["ci_preparation_diagnostic"])
        self.assertEqual(diagnostic["code"], "target_branch_changed")
        self.assertEqual(diagnostic["classification"], "identity_conflict")
        self.assertNotIn("pr_base_branch", typed)

        legacy_profile = replace(
            typed_profile,
            command_versions={
                **typed_profile.command_versions,
                "prepare_ci": "2.0.0",
            },
        )
        legacy_observed, legacy_protocol, legacy_target, legacy_schema = (
            actual_observation(legacy_profile)
        )
        self.assertEqual(legacy_protocol, "legacy")
        self.assertTrue(legacy_target)
        legacy = self._run_failure(
            legacy_protocol,
            legacy_observed,
            target_required=legacy_target,
            context_schema=legacy_schema,
        )
        self.assertEqual(
            set(legacy) - {"commentary"},
            {
                "transition",
                "workspace_path",
                "pr_url",
                "branch_name",
                "merge_strategy",
                "task_short_id",
                "ci_contract",
                "ci_report",
                "pr_feedback_cursor",
                "pr_base_branch",
            },
        )
        self.assertEqual(legacy["pr_base_branch"], "main")
        self.assertNotIn("ci_preparation_diagnostic", legacy)

    def test_archive_operation_follows_native_ci_graph_protocol(self) -> None:
        report = {
            "contract": {
                "repository": "owner/repository",
                "pull_number": 17,
            },
        }
        native_context = {
            "task_id": "native-task-id",
            "node_id": "native-ci-watch",
        }
        observed = types.SimpleNamespace(
            identity={"node_id": "native-ci-watch"},
            graph={"graph": {"nodes": [{
                "id": "native-ci-watch",
                "key": "ci_watch",
            }]}},
            profile=types.SimpleNamespace(
                command=lambda key: ".kent/scripts/workflow-evidence-ledger"
            ),
        )
        cases = (
            (
                "typed-v1",
                "archive_script_ci_report",
                "typed-artifact",
                "typed",
            ),
            (
                "legacy",
                "archive_ci_report",
                "legacy-artifact",
                "legacy",
            ),
        )
        for protocol, operation, artifact, expected_archive in cases:
            with self.subTest(protocol=protocol):
                module = load_template(
                    ROOT / "scripts/prepare-github-ci",
                    f"prepare_ci_archive_{protocol}",
                )
                payload = {
                    "operation": operation,
                    "workspace_path": str(self.workspace),
                    "pr_url": "https://github.com/owner/repository/pull/17",
                    "branch_name": "TASK-17",
                    "merge_strategy": "squash",
                    "task_short_id": "TASK-17",
                    "ci_report": "{}",
                    "_kent": native_context,
                }
                stdout = types.SimpleNamespace(buffer=io.BytesIO())
                stdout.write = lambda value: stdout.buffer.write(
                    value.encode("utf-8")
                )
                stdout.flush = lambda: None
                with (
                    mock.patch.object(
                        module,
                        "read_preparation_input",
                        return_value=payload,
                    ),
                    mock.patch.object(
                        module,
                        "observe_script_delivery_protocol",
                        return_value=(
                            observed,
                            protocol,
                            True,
                            "workflow-delivery-context-v2",
                            mock.Mock(),
                        ),
                    ),
                    mock.patch.object(
                        runtime,
                        "validate_dynamic_ci_report",
                        return_value=report,
                    ),
                    mock.patch.object(
                        module,
                        "archive_script_ci_report",
                        return_value="typed-artifact",
                    ) as typed_archive,
                    mock.patch.object(
                        module,
                        "archive_ci_report",
                        return_value="legacy-artifact",
                    ) as legacy_archive,
                    mock.patch.object(module.Path, "cwd", return_value=self.workspace),
                    mock.patch.object(
                        module.sys,
                        "stdout",
                        stdout,
                    ),
                ):
                    self.assertEqual(module.main(), 0)
                self.assertEqual(
                    json.loads(stdout.buffer.getvalue()),
                    {"ci_report_artifact": artifact},
                )
                if expected_archive == "typed":
                    typed_archive.assert_called_once_with(
                        payload,
                        evidence_command=".kent/scripts/workflow-evidence-ledger",
                    )
                    legacy_archive.assert_not_called()
                else:
                    typed_archive.assert_not_called()
                    legacy_archive.assert_called_once_with(
                        self.workspace,
                        "TASK-17",
                        report,
                    )


class ArchiveReaderSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "project"
        self.kit = self.root / "kit"
        self.project.mkdir()
        self.kit.mkdir()
        self.profile_bytes = b"[profile]\nsource = \"snapshot-test\"\n"
        self.bindings = {
            ".kent/workflow-profile.toml": ".kent/workflow-profile.toml",
            ".kent/scripts/workflow-evidence-ledger":
                "templates/project/workflow-evidence-ledger",
        }
        kit_paths = (
            set(ci_contract._ARCHIVE_KIT_MODULE_PATHS)
            | set(ci_contract._ARCHIVE_KIT_TEMPLATE_PATHS)
            | set(self.bindings.values())
        )
        self._write_source_tree(self.kit, kit_paths)
        self._write_file(
            self.kit,
            ".kent/workflow-profile.toml",
            self.profile_bytes,
            executable=False,
        )
        self._write_file(
            self.project,
            ".kent/workflow-profile.toml",
            self.profile_bytes,
            executable=False,
        )
        self._write_file(
            self.project,
            ".kent/scripts/workflow-evidence-ledger",
            (ROOT / self.bindings[".kent/scripts/workflow-evidence-ledger"]).read_bytes(),
            executable=True,
        )
        self._commit(self.project, "project")
        self._commit(self.kit, "kit")
        self.project_commit = self._git(self.project, "rev-parse", "HEAD")
        self.kit_commit = self._git(self.kit, "rev-parse", "HEAD")
        self.profile = types.SimpleNamespace(
            schema_version=4,
            kit_managed_commands=("evidence",),
            command_versions={},
            ci_archive_reader_sources=(),
            commands={},
            procedures={},
            capabilities={},
            policies={},
        )
        self.observation = types.SimpleNamespace(
            profile=self.profile,
            identity={"node_id": "native-ci-watch"},
            task_detail={
                "summary": {"workflow_id": "44444444-4444-4444-8444-444444444444"},
                "execution_target": {"commit_oid": self.project_commit},
            },
            graph={
                "workflow_id": "44444444-4444-4444-8444-444444444444",
                "graph": {
                    "nodes": [{
                        "id": "native-ci-watch",
                        "key": "ci_watch",
                        "kind": "script",
                        "script_path": ".kent/scripts/workflow-wait-github-ci",
                    }],
                    "transition_groups": [],
                    "edges": [],
                },
            },
        )

    @staticmethod
    def _write_file(root: Path, relative: str, raw: bytes, *, executable: bool) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o755 if executable else 0o644)

    def _write_source_tree(self, root: Path, paths: set[str]) -> None:
        for relative in paths:
            source = ROOT / relative
            self._write_file(
                root,
                relative,
                source.read_bytes(),
                executable=bool(source.stat().st_mode & 0o111),
            )

    def _git(self, root: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()

    def _commit(self, root: Path, label: str) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.name", "Kent Test"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "kent@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-qm", label],
            cwd=root,
            check=True,
        )

    def _snapshot(self):
        from workflowkit import delivery
        from workflowkit.profile import ProjectProfile

        native_context = {"task_id": "native-task-id", "node_id": "native-ci-watch"}
        with (
            mock.patch.object(ci_contract, "__file__", str(
                self.kit / "workflowkit/ci_contract.py"
            )),
            mock.patch.object(
                ci_contract,
                "_archive_project_reader_bindings",
                return_value=self.bindings,
            ),
            mock.patch.object(
                ProjectProfile,
                "from_toml",
                return_value=self.profile,
            ),
            mock.patch.object(
                delivery,
                "observe_script_delivery_graph",
                return_value=self.observation,
            ),
            mock.patch.object(
                delivery,
                "prepare_ci_protocol",
                return_value="typed-v1",
            ),
            mock.patch.object(
                delivery,
                "pr_base_branch_required",
                return_value=True,
            ),
            mock.patch.object(
                delivery,
                "delivery_context_schema",
                return_value="workflow-delivery-context-v2",
            ),
        ):
            return ci_contract._archive_reader_snapshot(
                self.project,
                "TASK-17",
                native_context,
                native_read=mock.Mock(),
            )

    def test_snapshot_binds_selected_project_and_serving_kit_source_bytes(self) -> None:
        proof, snapshots = self._snapshot()
        self.assertEqual(
            proof["schema"],
            "script-ci-archive-reader-proof-v1",
        )
        self.assertRegex(proof["reader_proof_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(proof["native_script_identity"], self.observation.identity)
        self.assertEqual(
            snapshots["project:.kent/scripts/workflow-evidence-ledger"],
            snapshots["kit:templates/project/workflow-evidence-ledger"],
        )

        project_reader = self.project / ".kent/scripts/workflow-evidence-ledger"
        project_reader.write_bytes(b"# task-different ledger bytes\n")
        with self.assertRaisesRegex(
            ci_contract.CiContractError,
            "archive_reader_source_mismatch",
        ):
            self._snapshot()

        project_reader.write_bytes(
            (ROOT / self.bindings[".kent/scripts/workflow-evidence-ledger"]).read_bytes()
        )
        kit_reader = self.kit / "templates/project/workflow-checkpoint"
        kit_reader.write_bytes(b"# serving-kit-different checkpoint bytes\n")
        with self.assertRaisesRegex(
            ci_contract.CiContractError,
            "archive_reader_source_mismatch",
        ):
            self._snapshot()


class ProjectOwnedArchiveReaderSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        from workflowkit.profile import ProjectProfile

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "project"
        self.kit = self.root / "kit"
        self.project.mkdir()
        self.kit.mkdir()

        profile = (ROOT / "contracts/project-profile.example.toml").read_text()
        profile = profile.replace("schema_version = 3", "schema_version = 4", 1)
        profile = profile.replace('release_topology = "none"\n', "", 1)
        profile = profile.replace(
            "kit_managed_adapters = []\n",
            'kit_managed_adapters = []\n'
            'kit_managed_commands = ["runtime_contracts", "verify", '
            '"evidence", "janitor", "wait_ci", "wait_pr", '
            '"github_observation", "prepare_ci"]\n'
            'ci_archive_reader_sources = '
            '[".kent/scripts/project-reader-wrapper"]\n',
            1,
        )
        profile = profile.replace(
            "[execution]\n",
            "[release]\n"
            'topology_kind = "puber-release"\n'
            'adoption_mode = "managed-in-place"\n'
            'spec_path = ".kent/workflows/specs/test.toml"\n'
            'builder_path = ".kent/workflows/builders/test.py"\n'
            'snapshot_path = ".kent/workflows/test.json"\n'
            "\n"
            "[command_versions]\n"
            'runtime_contracts = "2.1.0"\n'
            'verify = "2.0.0"\n'
            'evidence = "2.1.0"\n'
            'janitor = "3.0.0"\n'
            'wait_ci = "4.0.0"\n'
            'wait_pr = "4.0.0"\n'
            'github_observation = "1.0.0"\n'
            'prepare_ci = "3.0.0"\n'
            "\n"
            "[execution]\n",
            1,
        )
        profile = profile.replace(
            "[commands]\n",
            "[commands]\n"
            'runtime_contracts = ".kent/scripts/workflow_runtime_contracts.py"\n'
            'prepare_ci = ".kent/scripts/workflow-prepare-github-ci"\n',
            1,
        )
        profile = profile.replace(
            'checkpoint = ".kent/scripts/workflow-checkpoint"',
            'checkpoint = ".kent/scripts/project-checkpoint-wrapper"',
            1,
        )
        profile = profile.replace(
            'cleanup = ""',
            'cleanup = ".kent/commands/project-cleanup.md"',
            1,
        )
        self.profile_bytes = profile.encode("utf-8")
        self.profile = ProjectProfile.from_toml(
            self.project,
            profile,
            source="project-owned archive fixture",
            check_files=False,
        )

        self.native_script_path = ".kent/scripts/project-reader-wrapper"
        self.bindings = ci_contract._archive_project_reader_bindings(
            self.profile,
            script_key="wait_ci",
            script_path=self.native_script_path,
        )
        kit_paths = (
            set(ci_contract._ARCHIVE_KIT_MODULE_PATHS)
            | set(ci_contract._ARCHIVE_KIT_TEMPLATE_PATHS)
            | {canonical for canonical in self.bindings.values() if canonical}
        )
        self._write_source_tree(self.kit, kit_paths)
        self._write_file(
            self.kit,
            ".kent/workflow-profile.toml",
            b'[profile]\nproject_name = "kit-source"\n',
            executable=False,
        )
        cleanup_path = self.profile.procedure("cleanup")
        self.cleanup_bytes = b"# distinct project cleanup procedure\n"
        for project_path, canonical_path in self.bindings.items():
            if project_path == ".kent/workflow-profile.toml":
                raw, executable = self.profile_bytes, False
            elif project_path == cleanup_path:
                raw, executable = self.cleanup_bytes, False
            elif canonical_path:
                source = self.kit / canonical_path
                raw = source.read_bytes()
                executable = bool(source.stat().st_mode & 0o111)
                if project_path.startswith(".kent/scripts/"):
                    executable = True
            else:
                raw = f"# project-owned reader: {project_path}\n".encode()
                executable = project_path.startswith(".kent/scripts/")
            self._write_file(
                self.project,
                project_path,
                raw,
                executable=executable,
            )

        self._commit(self.kit, "serving Kit")
        self._commit(self.project, "project source")
        self.project_commit = self._git(self.project, "rev-parse", "HEAD")
        self.kit_commit = self._git(self.kit, "rev-parse", "HEAD")
        self.workflow_id = "44444444-4444-4444-8444-444444444444"
        self.observation = types.SimpleNamespace(
            profile=self.profile,
            identity={"node_id": "native-ci-watch"},
            task_detail={
                "summary": {"workflow_id": self.workflow_id},
                "execution_target": {"commit_oid": self.project_commit},
            },
            graph={
                "workflow_id": self.workflow_id,
                "graph": {
                    "nodes": [{
                        "id": "native-ci-watch",
                        "key": "ci_watch",
                        "kind": "script",
                        "script_path": self.native_script_path,
                    }],
                    "transition_groups": [],
                    "edges": [],
                },
            },
        )

    @staticmethod
    def _write_file(
        root: Path,
        relative: str,
        raw: bytes,
        *,
        executable: bool,
    ) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o755 if executable else 0o644)

    def _write_source_tree(self, root: Path, paths: set[str]) -> None:
        for relative in paths:
            source = ROOT / relative
            self._write_file(
                root,
                relative,
                source.read_bytes(),
                executable=bool(source.stat().st_mode & 0o111),
            )

    def _git(self, root: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()

    def _commit(self, root: Path, label: str) -> None:
        if not (root / ".git").exists():
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.name", "Kent Test"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "kent@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-qm", label],
            cwd=root,
            check=True,
        )

    def _snapshot(self):
        from workflowkit import delivery

        with (
            mock.patch.object(
                ci_contract,
                "__file__",
                str(self.kit / "workflowkit/ci_contract.py"),
            ),
            mock.patch.object(
                delivery,
                "observe_script_delivery_graph",
                return_value=self.observation,
            ),
            mock.patch.object(
                delivery,
                "prepare_ci_protocol",
                return_value="typed-v1",
            ),
            mock.patch.object(
                delivery,
                "pr_base_branch_required",
                return_value=True,
            ),
            mock.patch.object(
                delivery,
                "delivery_context_schema",
                return_value="workflow-delivery-context-v2",
            ),
        ):
            return ci_contract._archive_reader_snapshot(
                self.project,
                "TASK-17",
                {"task_id": "native-task-id", "node_id": "native-ci-watch"},
                native_read=mock.Mock(),
            )

    def test_distinct_project_profile_cleanup_and_wrapper_snapshot(self) -> None:
        from workflowkit import delivery

        bindings = self.bindings
        self.assertEqual(bindings[".kent/workflow-profile.toml"], "")
        self.assertEqual(bindings[self.profile.procedure("cleanup")], "")
        self.assertEqual(bindings[self.profile.command("checkpoint")], "")
        self.assertEqual(bindings[".kent/scripts/project-reader-wrapper"], "")
        self.assertEqual(
            bindings[self.profile.command("evidence")],
            "templates/project/workflow-evidence-ledger",
        )
        self.assertNotEqual(
            self.profile_bytes,
            (self.kit / ".kent/workflow-profile.toml").read_bytes(),
        )
        self.assertNotEqual(
            self.cleanup_bytes,
            (self.kit / ".kent/commands/cleanup-task.md").read_bytes(),
        )
        proof, snapshots = self._snapshot()
        self.assertEqual(
            proof["schema"],
            "script-ci-archive-reader-proof-v1",
        )
        self.assertEqual(
            snapshots["project:.kent/workflow-profile.toml"],
            self.profile_bytes,
        )
        self.assertEqual(
            snapshots[f"project:{self.profile.procedure('cleanup')}"],
            self.cleanup_bytes,
        )
        self.assertEqual(
            snapshots[f"project:{self.profile.command('checkpoint')}"],
            b"# project-owned reader: .kent/scripts/project-checkpoint-wrapper\n",
        )
        self.assertEqual(
            snapshots["project:.kent/scripts/project-reader-wrapper"],
            b"# project-owned reader: .kent/scripts/project-reader-wrapper\n",
        )
        self.assertEqual(
            snapshots[f"project:{self.profile.command('evidence')}"],
            snapshots["kit:templates/project/workflow-evidence-ledger"],
        )

    def test_native_ci_wrapper_must_be_in_static_reader_closure(self) -> None:
        self.observation.graph["graph"]["nodes"][0]["script_path"] = (
            ".kent/scripts/undeclared-ci-wrapper"
        )
        with self.assertRaisesRegex(
            ci_contract.CiContractError,
            "native_script_path_mismatch",
        ):
            self._snapshot()

    def test_changed_managed_helper_fails_after_matching_project_commit(self) -> None:
        managed_path = self.project / self.profile.command("evidence")
        managed_path.write_bytes(b"# altered managed evidence helper\n")
        self._commit(self.project, "altered managed evidence helper")
        self.project_commit = self._git(self.project, "rev-parse", "HEAD")
        self.observation.task_detail["execution_target"]["commit_oid"] = (
            self.project_commit
        )
        with self.assertRaisesRegex(
            ci_contract.CiContractError,
            "archive_reader_family_mismatch",
        ):
            self._snapshot()


class ScriptDeliveryProtocolObservationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        (self.workspace / ".kent").mkdir()
        self.profile_path = self.workspace / ".kent/workflow-profile.toml"
        self.profile_path.write_text(
            '[policies]\ndelivery_reliability = "v1"\n'
        )
        self.native_context = {
            "task_id": "native-task-id",
            "node_id": "ci_prepare",
        }
        self.payload = {
            "workspace_path": str(self.workspace),
            "task_short_id": "TASK-17",
            "_kent": self.native_context,
        }

    def test_current_profile_requires_native_script_identity(self) -> None:
        profile = types.SimpleNamespace()
        with (
            mock.patch(
                "workflowkit.profile.ProjectProfile.from_toml",
                return_value=profile,
            ),
            mock.patch.object(ci_contract, "_native_read_adapter") as native_read,
            mock.patch.object(ci_contract.Path, "cwd", return_value=self.workspace),
        ):
            payload = dict(self.payload)
            payload.pop("_kent")
            with self.assertRaisesRegex(
                ci_contract.CiContractError,
                "native_script_identity_required",
            ):
                ci_contract.observe_script_delivery_protocol(payload)
        native_read.assert_not_called()

    def test_profile_symlink_is_not_a_legacy_protocol_fallback(self) -> None:
        target = self.workspace / "profile-target.toml"
        target.write_text('[policies]\ndelivery_reliability = "legacy"\n')
        self.profile_path.unlink()
        self.profile_path.symlink_to(target)
        with self.assertRaisesRegex(
            ci_contract.CiContractError,
            "profile_unsafe",
        ):
            ci_contract.observe_script_delivery_protocol(self.payload)

    def test_current_profile_uses_native_graph_protocol_selectors(self) -> None:
        from workflowkit import delivery

        profile = types.SimpleNamespace()
        observation = types.SimpleNamespace(profile=profile)
        native_read = mock.Mock()
        with (
            mock.patch(
                "workflowkit.profile.ProjectProfile.from_toml",
                return_value=profile,
            ),
            mock.patch.object(
                ci_contract,
                "_native_read_adapter",
                return_value=native_read,
            ),
            mock.patch.object(
                delivery,
                "observe_script_delivery_graph",
                return_value=observation,
            ) as observe,
            mock.patch.object(
                delivery,
                "prepare_ci_protocol",
                return_value="typed-v1",
            ),
            mock.patch.object(
                delivery,
                "pr_base_branch_required",
                return_value=True,
            ),
            mock.patch.object(
                delivery,
                "delivery_context_schema",
                return_value="workflow-delivery-context-v2",
            ),
            mock.patch.object(ci_contract.Path, "cwd", return_value=self.workspace),
        ):
            observed, protocol, target_required, schema, reader = (
                ci_contract.observe_script_delivery_protocol(self.payload)
            )
        self.assertIs(observed, observation)
        self.assertEqual(protocol, "typed-v1")
        self.assertTrue(target_required)
        self.assertEqual(schema, "workflow-delivery-context-v2")
        self.assertIs(reader, native_read)
        observe.assert_called_once_with(
            self.workspace,
            "TASK-17",
            self.native_context,
            native_read=native_read,
        )


class LegacyArchiveCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        (self.workspace / ".kent").mkdir()
        self.profile_path = self.workspace / ".kent/workflow-profile.toml"
        self.payload = {
            "operation": "archive_ci_report",
            "workspace_path": str(self.workspace),
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "TASK-17",
            "merge_strategy": "squash",
            "task_short_id": "TASK-17",
            "ci_report": "",
        }
        contract = {
            "schema": "github-ci-contract-v1",
            "repository": "owner/repository",
            "pull_number": 17,
            "head_oid": "a" * 40,
            "base_oid": "b" * 40,
            "require_ci": True,
        }
        self.report = runtime.build_dynamic_ci_report(
            contract=contract,
            attempts=[{
                "sequence": 1,
                "head_oid": contract["head_oid"],
                "base_oid": contract["base_oid"],
                "retry": None,
                "checks": [],
                "check_count": 0,
                "checks_sha256": runtime.canonical_sha256([]),
                "reason": "no_checks",
                "watcher_exit_code": 0,
                "error": None,
            }],
        )
        self.payload["ci_report"] = json.dumps(self.report)
        self.module = load_template(
            ROOT / "scripts/prepare-github-ci",
            "prepare_ci_legacy_archive_compatibility",
        )

    def _run_cli(self) -> tuple[int, str]:
        stdout = io.StringIO()
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, json.dumps(self.payload).encode())
        finally:
            os.close(write_fd)
        with os.fdopen(read_fd, "rb") as input_stream:
            with (
                mock.patch.object(
                    self.module.sys,
                    "stdin",
                    types.SimpleNamespace(buffer=input_stream),
                ),
                mock.patch.object(self.module.sys, "stdout", stdout),
                mock.patch.object(
                    self.module.Path,
                    "cwd",
                    return_value=self.workspace,
                ),
            ):
                code = self.module.main()
        return code, stdout.getvalue()

    def test_old_schema4_profile_keeps_legacy_archive_without_kent(self) -> None:
        self.profile_path.write_text(
            'schema_version = 4\n'
            'kit_managed_commands = ["runtime_contracts", "prepare_ci"]\n'
            '[policies]\n'
            'delivery_reliability = "v1"\n'
            '[command_versions]\n'
            'runtime_contracts = "2.0.0"\n'
            'prepare_ci = "2.0.0"\n'
        )
        with mock.patch.object(
            self.module,
            "archive_ci_report",
            return_value=".kent/runtime/TASK-17/ci-report-test.json",
        ) as archive:
            code, output = self._run_cli()
        self.assertEqual(code, 0, output)
        self.assertEqual(
            json.loads(output),
            {"ci_report_artifact": ".kent/runtime/TASK-17/ci-report-test.json"},
        )
        archive.assert_called_once_with(
            self.workspace,
            "TASK-17",
            self.report,
        )

    def test_typed_profile_without_kent_cannot_fall_back_to_legacy(self) -> None:
        self.profile_path.write_text(
            'schema_version = 4\n'
            'kit_managed_commands = ["runtime_contracts", "prepare_ci"]\n'
            '[policies]\n'
            'delivery_reliability = "v1"\n'
            '[command_versions]\n'
            'runtime_contracts = "2.1.0"\n'
            'prepare_ci = "3.0.0"\n'
        )
        profile = types.SimpleNamespace()
        with (
            mock.patch(
                "workflowkit.profile.ProjectProfile.from_toml",
                return_value=profile,
            ),
            mock.patch.object(
                self.module,
                "archive_ci_report",
            ) as archive,
        ):
            code, output = self._run_cli()
        self.assertEqual(code, 2)
        self.assertEqual(
            json.loads(output),
            {"error": "ci_report_archive_failed"},
        )
        archive.assert_not_called()


class CiRerunEffectClosureTest(unittest.TestCase):
    @staticmethod
    def workflow(jobs: list[dict[str, object]]) -> dict[str, object]:
        return {
            "schema": "normalized_github_workflow_source_v1",
            "jobs": jobs,
        }

    def test_safe_test_dependency_dag_returns_complete_effect_set(self) -> None:
        workflow = self.workflow([
            {"job_key": "unit", "needs": []},
            {"job_key": "integration", "needs": ["unit"]},
            {"job_key": "lint", "needs": []},
        ])
        result = ci_contract.qualify_ci_rerun_effect_closure(
            workflow,
            "unit",
            effects_by_job={
                "unit": ["test"],
                "integration": ["infrastructure", "test"],
            },
        )
        self.assertEqual(
            result,
            {
                "jobs": ["integration", "unit"],
                "effects": ["infrastructure", "test"],
            },
        )

    def test_publish_dependent_blocks_test_job_rerun(self) -> None:
        workflow = self.workflow([
            {"job_key": "unit", "needs": []},
            {"job_key": "publish", "needs": ["unit"]},
        ])
        with self.assertRaisesRegex(
            ci_contract.CiContractError,
            "rerun_effect_forbidden",
        ):
            ci_contract.qualify_ci_rerun_effect_closure(
                workflow,
                "unit",
                effects_by_job={
                    "unit": ["test"],
                    "publish": ["publication"],
                },
            )

    def test_missing_or_unknown_effect_declaration_blocks(self) -> None:
        workflow = self.workflow([
            {"job_key": "unit", "needs": []},
            {"job_key": "report", "needs": ["unit"]},
        ])
        cases = (
            (
                {"unit": ["test"]},
                "rerun_effect_unknown",
            ),
            (
                {"unit": ["test"], "report": ["unknown"]},
                "rerun_effect_forbidden",
            ),
        )
        for effects, error in cases:
            with self.subTest(effects=effects), self.assertRaisesRegex(
                ci_contract.CiContractError,
                error,
            ):
                ci_contract.qualify_ci_rerun_effect_closure(
                    workflow,
                    "unit",
                    effects_by_job=effects,
                )

    def test_invalid_dependency_graph_fails_closed(self) -> None:
        cases = (
            self.workflow([
                {"job_key": "unit", "needs": ["missing"]},
            ]),
            self.workflow([
                {"job_key": "unit", "needs": ["report"]},
                {"job_key": "report", "needs": ["unit"]},
            ]),
            self.workflow([
                {"job_key": "unit", "needs": []},
                {"job_key": "unit", "needs": []},
            ]),
            self.workflow([
                {"job_key": "unit", "needs": "report"},
            ]),
        )
        for workflow in cases:
            with self.subTest(workflow=workflow), self.assertRaisesRegex(
                ci_contract.CiContractError,
                "rerun_dependency_invalid",
            ):
                ci_contract.qualify_ci_rerun_effect_closure(
                    workflow,
                    "unit",
                    effects_by_job={"unit": ["test"]},
                )


class ProviderRetryWaiterTest(unittest.TestCase):
    def test_both_waiters_honor_typed_provider_delay_and_keep_normal_interval(self):
        from workflowkit.github_observation import ObservationError

        throttled = ObservationError(
            "query_failed",
            rate_limited=True,
            retry_after_seconds=120,
        )
        ordinary = ObservationError("query_failed")
        for name in ("workflow-wait-github-ci", "workflow-wait-github-pr"):
            module = load_template(
                ROOT / "templates/project" / name,
                name.replace("-", "_") + "_retry_delay",
            )
            with self.subTest(watcher=name):
                self.assertEqual(
                    module._retry_delay_seconds(
                        throttled,
                        interval=30,
                        failure_count=1,
                    ),
                    120,
                )
                self.assertEqual(
                    module._retry_delay_seconds(
                        ordinary,
                        interval=30,
                        failure_count=1,
                    ),
                    30,
                )

    def _run_guarded_waiter(self, watcher_name: str, scenario: str):
        from workflowkit import github_observation

        module = load_template(
            ROOT / "templates/project" / watcher_name,
            watcher_name.replace("-", "_") + "_guarded_retry",
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        (root / ".kent").mkdir()
        (root / ".kent/workflow-profile.toml").write_text(
            '[policies]\ndelivery_reliability = "v1"\n',
            encoding="utf-8",
        )
        contract = {
            "schema": "github-ci-contract-v1",
            "repository": "owner/repository",
            "pull_number": 17,
            "head_oid": "a" * 40,
            "base_oid": "b" * 40,
            "require_ci": True,
        }
        report = runtime.build_dynamic_ci_report(
            contract=contract,
            attempts=[{
                "sequence": 1,
                "head_oid": contract["head_oid"],
                "base_oid": contract["base_oid"],
                "retry": None,
                "checks": [],
                "check_count": 0,
                "checks_sha256": runtime.canonical_sha256([]),
                "reason": "query_failed",
                "watcher_exit_code": 1,
                "error": {
                    "code": "query_failed",
                    "message": "query_failed",
                    "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                    "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                },
            }],
        )
        report_text = runtime.canonical_bytes(report).decode("utf-8")
        payload = {
            "workspace_path": str(root),
            "task_short_id": "TASK-17",
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "TASK-17",
            "merge_strategy": "squash",
            "pr_base_branch": "main",
            "pr_head_oid": contract["head_oid"],
            "pr_base_oid": contract["base_oid"],
            "ci_contract": runtime.canonical_bytes(contract).decode("utf-8"),
            "ci_report": report_text,
            "pr_feedback_cursor": "uninitialized",
            "commentary": "retained incoming commentary",
            "_kent": {
                "task_id": "native-task-id",
                "node_id": "native-waiter-node",
            },
        }
        identity = {
            "task_id": "native-task-id",
            "task_short_id": "TASK-17",
            "project_id": "native-project-id",
            "workspace_path": str(root),
            "invocation_kind": "script",
            "node_id": "native-waiter-node",
            "transition_branch_key": None,
        }
        inherited_state = "unknown" if scenario == "inherited_block" else None
        require_calls = []
        child_calls = []
        sleep = mock.Mock()
        archives = mock.Mock()
        emitted = []

        class ProviderBlocked(RuntimeError):
            def __init__(self, state: str):
                self.state = state

        blocked_error = ProviderBlocked(inherited_state or "unknown")

        class Checkpoint:
            @staticmethod
            def require_github_provider_read(_root, _payload, _identity):
                require_calls.append((_root, _payload, _identity))
                if inherited_state is not None:
                    raise blocked_error

            @staticmethod
            def is_github_provider_read_blocked(error):
                return isinstance(error, ProviderBlocked)

            @staticmethod
            def emit_producer_result(
                _root,
                producer_payload,
                result,
                producer_kind,
                **kwargs,
            ):
                emitted.append({
                    "payload": producer_payload,
                    "result": result,
                    "producer_kind": producer_kind,
                    "kwargs": kwargs,
                })
                return result

        if scenario == "unknown_terminal":
            failure = github_observation.ObservationError(
                "query_failed",
                provider_retry_unknown=True,
                provider_retry_captured=True,
                provider_retry_observed_at_ns=1_000_000_000_000,
            )
        else:
            failure = github_observation.ObservationError(
                "query_failed",
                rate_limited=True,
                retry_after_seconds=120,
                provider_retry_captured=True,
                provider_retry_observed_at_ns=1_000_000_000_000,
            )

        def observer_factory(*_args, provider_guard=None, **_kwargs):
            def observe_pr(_url):
                if provider_guard is not None:
                    provider_guard()
                child_calls.append("gh")
                raise failure

            return types.SimpleNamespace(observe_pr=observe_pr)

        output = io.BytesIO()
        node_key = "ci_watch" if "github-ci" in watcher_name else "merge_watch"
        protocol = {
            "protocol": "typed-v1",
            "identity": identity,
            "node_key": node_key,
            "target_required": True,
            "context_schema": "workflow-delivery-context-v2",
        }
        max_errors = 2 if scenario == "local_block" else 1
        if scenario == "unknown_terminal":
            max_errors = 5
        with (
            mock.patch.object(module, "runtime_contracts", return_value=runtime),
            mock.patch.object(
                module,
                "github_observation",
                return_value=types.SimpleNamespace(
                    GitHubObserver=observer_factory,
                    ObservationError=github_observation.ObservationError,
                    parse_pr_url=lambda _url: ("owner/repository", 17),
                    provider_retry_constraint=(
                        github_observation.provider_retry_constraint
                    ),
                ),
            ),
            mock.patch.object(
                module,
                "_current_delivery_protocol",
                return_value=protocol,
            ),
            mock.patch.object(module, "_sibling", return_value=Checkpoint),
            mock.patch.object(module, "_archive", archives, create=True),
            mock.patch.object(module.Path, "cwd", return_value=root),
            mock.patch.object(module.time, "sleep", sleep),
            mock.patch.object(
                module.time,
                "time_ns",
                return_value=1_000_000_000_000,
            ),
            mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(
                module.sys,
                "stdout",
                types.SimpleNamespace(buffer=output),
            ),
            mock.patch.dict(os.environ, {
                "KENT_CI_WATCH_TEST_MODE": "1",
                "KENT_CI_WATCH_MAX_POLLS": "1",
                "KENT_CI_WATCH_MAX_ERRORS": str(max_errors),
                "KENT_CI_WATCH_INTERVAL_SECONDS": "30",
                "KENT_PR_WATCH_TEST_MODE": "1",
                "KENT_PR_WATCH_MAX_POLLS": "1",
                "KENT_PR_WATCH_MAX_ERRORS": str(max_errors),
                "KENT_PR_WATCH_INTERVAL_SECONDS": "30",
            }),
        ):
            self.assertEqual(module.main(), 0)
        return {
            "result": json.loads(output.getvalue()),
            "child_calls": child_calls,
            "require_calls": require_calls,
            "sleep": sleep,
            "archives": archives,
            "emitted": emitted,
            "report_text": report_text,
            "payload": payload,
        }

    def test_terminal_errors_capture_retry_constraint_without_stdout_fields(self):
        for watcher_name in (
            "workflow-wait-github-ci",
            "workflow-wait-github-pr",
        ):
            for scenario, expected_kind in (
                ("known_terminal", "not_before"),
                ("unknown_terminal", "unknown"),
            ):
                with self.subTest(watcher=watcher_name, scenario=scenario):
                    outcome = self._run_guarded_waiter(watcher_name, scenario)
                    self.assertEqual(len(outcome["child_calls"]), 1)
                    self.assertEqual(len(outcome["emitted"]), 1)
                    capture = outcome["emitted"][0]
                    self.assertEqual(capture["payload"], outcome["payload"])
                    self.assertEqual(
                        capture["producer_kind"],
                        "wait-ci" if "github-ci" in watcher_name else "wait-pr",
                    )
                    self.assertEqual(
                        json.loads(
                            capture["kwargs"]["github_retry_constraint"]
                        )["kind"],
                        expected_kind,
                    )
                    self.assertNotIn(
                        "github_retry_constraint",
                        capture["result"].decode("utf-8"),
                    )

    def test_guard_denial_does_not_query_sleep_archive_or_rewrite_ci_report(self):
        for watcher_name in (
            "workflow-wait-github-ci",
            "workflow-wait-github-pr",
        ):
            for scenario in ("inherited_block", "local_block"):
                with self.subTest(watcher=watcher_name, scenario=scenario):
                    outcome = self._run_guarded_waiter(watcher_name, scenario)
                    self.assertEqual(
                        len(outcome["child_calls"]),
                        0 if scenario == "inherited_block" else 1,
                    )
                    self.assertEqual(
                        len(outcome["require_calls"]),
                        1 if scenario == "inherited_block" else 2,
                    )
                    if scenario == "local_block":
                        outcome["sleep"].assert_called_once_with(120)
                    else:
                        outcome["sleep"].assert_not_called()
                    outcome["archives"].assert_not_called()
                    if "github-ci" in watcher_name:
                        self.assertEqual(
                            outcome["result"]["transition"],
                            "ci_watch_failed",
                        )
                        self.assertNotIn("ci_report", outcome["result"])
                        context = json.loads(outcome["result"]["delivery_context"])
                        self.assertEqual(context["ci_report"], outcome["report_text"])
                        self.assertEqual(
                            len(
                                json.loads(
                                    context["ci_report"]
                                )["attempts"]
                            ),
                            len(
                                json.loads(outcome["report_text"])["attempts"]
                            ),
                        )
                    else:
                        self.assertEqual(
                            outcome["result"]["transition"],
                            "merge_watch_state_changed",
                        )
                        self.assertEqual(
                            json.loads(
                                outcome["result"]["pr_report"]
                            )["reason"],
                            "github_query_failed",
                        )
                    self.assertNotIn(
                        "github_retry_constraint",
                        outcome["emitted"][0]["result"].decode("utf-8"),
                    )


class CheckpointProviderGuardCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".kent").mkdir()
        self.module = load_template(
            ROOT / "templates/project/workflow-checkpoint",
            "checkpoint_provider_guard_test",
        )
        self.identity = {
            "task_id": "native-task-id",
            "task_short_id": "TASK-17",
            "project_id": "native-project-id",
            "workspace_path": str(self.root),
            "session_id": "11111111-1111-4111-8111-111111111111",
            "step_id": "22222222-2222-4222-8222-222222222222",
        }
        self.refs = [
            "build/kent-workflow/TASK-17/packets/base-"
            + "a" * 64
            + ".json",
        ]

    def _run_guard(self, state: str | None):
        calls = []

        class ProviderBlocked(RuntimeError):
            def __init__(self, blocked_state: str):
                self.state = blocked_state

        def require(root, refs, identity):
            calls.append((root, refs, identity))
            if state is not None:
                raise ProviderBlocked(state)

        fake_delivery = types.SimpleNamespace(
            GitHubProviderRetryBlocked=ProviderBlocked,
            require_github_provider_read=require,
        )
        read_fd, write_fd = os.pipe()
        os.write(
            write_fd,
            json.dumps({"refs": self.refs}).encode("utf-8"),
        )
        os.close(write_fd)
        output = io.StringIO()
        with (
            os.fdopen(read_fd, "r", encoding="utf-8") as input_stream,
            mock.patch.object(self.module.sys, "stdin", input_stream),
            mock.patch.object(self.module.sys, "stdout", output),
            mock.patch.object(
                self.module,
                "current_delivery_identity",
                return_value=self.identity,
            ),
            mock.patch.object(
                self.module,
                "delivery_module",
                return_value=fake_delivery,
            ),
        ):
            code = self.module.require_github_provider_read_command(
                self.root,
                "TASK-17",
            )
        return code, output.getvalue(), calls

    def test_agent_guard_cli_passes_real_identity_and_exact_refs(self):
        code, output, calls = self._run_guard(None)
        self.assertEqual(code, 0)
        self.assertEqual(output, '{"allowed":true}\n')
        self.assertEqual(calls, [(self.root, self.refs, self.identity)])

    def test_active_and_unknown_guard_cli_return_only_fixed_state(self):
        for state in ("not_before", "unknown"):
            with self.subTest(state=state):
                code, output, calls = self._run_guard(state)
                self.assertEqual(code, 3)
                self.assertEqual(
                    json.loads(output),
                    {"allowed": False, "state": state},
                )
                self.assertNotIn("deadline", output)
                self.assertEqual(calls, [(self.root, self.refs, self.identity)])

    def test_foreign_identity_failure_is_not_rewritten_as_a_guard_block(self):
        fake_delivery = types.SimpleNamespace(
            GitHubProviderRetryBlocked=RuntimeError,
            require_github_provider_read=mock.Mock(
                side_effect=ValueError("producer identity mismatch"),
            ),
        )
        read_fd, write_fd = os.pipe()
        os.write(write_fd, json.dumps({"refs": self.refs}).encode("utf-8"))
        os.close(write_fd)
        output = io.StringIO()
        with (
            os.fdopen(read_fd, "r", encoding="utf-8") as input_stream,
            mock.patch.object(self.module.sys, "stdin", input_stream),
            mock.patch.object(self.module.sys, "stdout", output),
            mock.patch.object(
                self.module,
                "current_delivery_identity",
                return_value=self.identity,
            ),
            mock.patch.object(
                self.module,
                "delivery_module",
                return_value=fake_delivery,
            ),
            self.assertRaisesRegex(ValueError, "producer identity mismatch"),
        ):
            self.module.require_github_provider_read_command(
                self.root,
                "TASK-17",
            )
        fake_delivery.require_github_provider_read.assert_called_once_with(
            self.root,
            self.refs,
            self.identity,
        )
        self.assertEqual(output.getvalue(), "")

    def test_emitter_keeps_retry_constraint_binding_only_and_preserves_bytes(self):
        (self.root / ".kent/workflow-profile.toml").write_text(
            '[policies]\ndelivery_reliability = "v1"\n',
            encoding="utf-8",
        )
        captured = {}

        def capture_script_producer(**kwargs):
            captured.update(kwargs)
            return "locator", b"original-emitted-bytes"

        fake_delivery = types.SimpleNamespace(
            packet_decode=lambda raw: json.loads(raw),
            producer_locator=lambda _text: self.refs[0],
            capture_script_producer=capture_script_producer,
        )
        constraint = (
            '{"kind":"not_before","not_before_unix_ms":1234567890,'
            '"provider_host":"github.com","schema":"github-provider-retry-v1"}'
        )
        payload = {
            "_kent": {"task_id": "native-task-id", "node_id": "native-node-id"},
            "commentary": "prior result\n" + self.refs[0],
        }
        with mock.patch.object(
            self.module,
            "delivery_module",
            return_value=fake_delivery,
        ):
            emitted = self.module.emit_producer_result(
                self.root,
                payload,
                b'{"transition":"ci_watch_failed"}\n',
                "wait-ci",
                github_retry_constraint=constraint,
            )
        self.assertEqual(emitted, b"original-emitted-bytes")
        self.assertEqual(
            captured["binding"]["producer_refs"],
            json.dumps(
                {"refs": [self.refs[0]]},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        self.assertEqual(captured["github_retry_constraint"], constraint)
        self.assertEqual(captured["result"], {"transition": "ci_watch_failed"})


class CheckpointFeedbackDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        from workflowkit import delivery

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".kent").mkdir()
        self.module = load_template(
            ROOT / "templates/project/workflow-checkpoint",
            "checkpoint_feedback_decision_test",
        )
        self.delivery = delivery
        self.url = "https://github.com/owner/repository/pull/17"
        self.agent_identity = {
            "task_id": "native-task-id",
            "task_short_id": "TASK-17",
            "project_id": "native-project-id",
            "workspace_path": str(self.root),
            "session_id": "11111111-1111-4111-8111-111111111111",
            "step_id": "22222222-2222-4222-8222-222222222222",
        }
        self.script_identity = {
            "task_id": "native-task-id",
            "task_short_id": "TASK-17",
            "project_id": "native-project-id",
            "workspace_path": str(self.root),
            "invocation_kind": "script",
            "node_id": "native-merge-watch",
            "transition_branch_key": None,
        }
        self.graph = self._graph(
            "native-merge-watch",
            "merge_watch",
            "merge_watch_state_changed",
            ".kent/scripts/workflow-wait-github-pr",
        )
        self.task_detail = {
            "summary": {
                "id": "native-task-id",
                "short_id": "TASK-17",
                "project_id": "native-project-id",
                "workflow_id": "44444444-4444-4444-8444-444444444444",
            }
        }
        self.profile = types.SimpleNamespace(
            delivery_reliability_policy=lambda: "v1",
            capability=lambda key: (
                key == "pull_requests"
                or (key == "ci_monitoring" and getattr(self, "ci_monitoring", False))
            ),
            command=lambda key: {
                "wait_pr": ".kent/scripts/workflow-wait-github-pr",
                "wait_ci": ".kent/scripts/workflow-wait-github-ci",
            }.get(key, ""),
        )
        self.candidate = runtime.canonical_bytes(
            runtime.make_dynamic_pr_feedback_cursor(
                repository="owner/repository",
                pull_number=17,
                items=[],
                review_decision="CHANGES_REQUESTED",
            )
        ).decode()
        self.contract = {
            "schema": "github-ci-contract-v1",
            "repository": "owner/repository",
            "pull_number": 17,
            "head_oid": "a" * 40,
            "base_oid": "b" * 40,
            "require_ci": True,
        }
        self.contract_text = runtime.canonical_bytes(self.contract).decode()
        self.report = runtime.build_dynamic_ci_report(
            contract=self.contract,
            attempts=[{
                "sequence": 1,
                "head_oid": self.contract["head_oid"],
                "base_oid": self.contract["base_oid"],
                "retry": None,
                "checks": [],
                "check_count": 0,
                "checks_sha256": runtime.canonical_sha256([]),
                "reason": "no_checks",
                "watcher_exit_code": 0,
                "error": None,
            }],
        )
        self.report_text = runtime.canonical_bytes(self.report).decode()

    def _graph(
        self,
        node_id: str,
        node_key: str,
        transition: str,
        script_path: str,
    ) -> dict[str, object]:
        group_id = "feedback-state-changed"
        fields = [
            "workspace_path",
            "task_short_id",
            "pr_url",
            "branch_name",
            "merge_strategy",
            "pr_report",
            "pr_feedback_cursor",
            "ci_contract",
            "ci_report",
        ]
        return {
            "workflow_id": "44444444-4444-4444-8444-444444444444",
            "graph": {
                "nodes": [{
                    "id": node_id,
                    "key": node_key,
                    "kind": "script",
                    "script_path": script_path,
                }],
                "transition_groups": [{
                    "id": group_id,
                    "source_node_id": node_id,
                    "transition_id": transition,
                }],
                "edges": [{
                    "id": "feedback-state-changed-edge",
                    "transition_group_id": group_id,
                    "parameters": [{"key": key} for key in fields],
                }],
            },
        }

    def _source(
        self,
        *,
        report_raw: str | None = None,
        input_cursor: str = "uninitialized",
        result_cursor: str | None = None,
        producer_kind: str = "wait-pr",
        transition: str = "merge_watch_state_changed",
        binding: dict[str, str] | None = None,
    ) -> str:
        observed_cursor = json.dumps(self.candidate)
        if report_raw is None:
            report_raw = (
                '{ "reason" : "pull_request_feedback_changed", '
                f'"url" : "{self.url}", '
                f'"observed_feedback_cursor" : {observed_cursor} }} \n'
            )
        context = {
            "schema": "workflow-delivery-context-v2",
            "phase": "post_pr",
            "pr_url": self.url,
            "branch_name": "TASK-17",
            "pr_base_branch": "main",
            "merge_strategy": "squash",
            "pr_feedback_cursor": input_cursor,
            "ci_contract": self.contract_text,
            "ci_report": self.report_text,
        }
        context_raw = json.dumps(context, indent=2)
        inputs = {
            "workspace_path": str(self.root),
            "task_short_id": "TASK-17",
            "pr_url": self.url,
            "branch_name": "TASK-17",
            "pr_base_branch": "main",
            "merge_strategy": "squash",
            "delivery_context": context_raw,
            "pr_feedback_cursor": input_cursor,
            "ci_contract": self.contract_text,
            "ci_report": self.report_text,
        }
        result = {
            **inputs,
            "transition": transition,
            "pr_report": report_raw,
        }
        if result_cursor is None:
            result_cursor = input_cursor
        result["pr_feedback_cursor"] = result_cursor
        identity = dict(self.script_identity)
        if producer_kind == "wait-ci":
            self.ci_monitoring = True
            identity["node_id"] = "native-ci-watch"
            self.graph = self._graph(
                "native-ci-watch",
                "ci_watch",
                "ci_watch_state_changed",
                ".kent/scripts/workflow-wait-github-ci",
            )
        else:
            identity["node_id"] = "native-merge-watch"
            self.graph = self._graph(
                "native-merge-watch",
                "merge_watch",
                "merge_watch_state_changed",
                ".kent/scripts/workflow-wait-github-pr",
            )
        return self.delivery.capture_producer(
            self.root,
            identity=identity,
            producer_kind=producer_kind,
            inputs=inputs,
            result=result,
            binding=binding,
        )[0]

    def _derive(self, source_ref: str, *, decision: str, reason: str = "all changes considered"):
        def native_read(_root, arguments, *, structured=True):
            if arguments[:2] == ["task", "show"]:
                return self.task_detail
            if arguments[:3] == ["workflow", "graph", "inspect"]:
                return self.graph
            raise AssertionError(arguments)

        request = {
            "schema": "workflow-feedback-decision-request-v1",
            "producer_ref": source_ref,
            "decision": decision,
            "reason": reason,
        }
        with (
            mock.patch.object(
                self.module,
                "delivery_module",
                return_value=self.delivery,
            ),
            mock.patch.object(
                self.module,
                "current_delivery_identity",
                return_value=self.agent_identity,
            ),
            mock.patch.object(
                self.module,
                "delivery_native_read",
                side_effect=native_read,
            ),
            mock.patch(
                "workflowkit.profile.ProjectProfile.load",
                return_value=self.profile,
            ),
        ):
            return self.module.derive_feedback_decision(
                self.root,
                "TASK-17",
                request,
            )

    def test_acknowledgement_derivative_retains_original_packets_and_ancestry(self) -> None:
        source_ref = self._source()
        source_path = self.root / source_ref
        source_raw = source_path.read_bytes()
        receipt = self._derive(
            source_ref,
            decision="acknowledge_all_reported_feedback",
        )
        derivative = self.delivery.reconstruct_producer(
            self.root,
            receipt["producer_ref"],
            identity=self.agent_identity,
        )
        original = self.delivery.reconstruct_producer(
            self.root,
            source_ref,
            identity=self.agent_identity,
        )
        self.assertEqual(derivative["producer_kind"], "feedback-decision")
        self.assertEqual(derivative["identity"], self.agent_identity)
        self.assertEqual(
            derivative["result"],
            {
                **original["result"],
                "pr_feedback_cursor": self.candidate,
            },
        )
        for key in ("pr_report", "ci_contract", "ci_report", "delivery_context"):
            self.assertEqual(
                derivative["result"].get(key),
                original["result"].get(key),
            )
        self.assertEqual(source_path.read_bytes(), source_raw)
        self.assertEqual(
            json.loads(derivative["binding"]["producer_refs"]),
            {"refs": [source_ref]},
        )
        binding = json.loads(derivative["binding"]["feedback_decision"])
        self.assertEqual(binding["source_ref"], source_ref)
        self.assertEqual(binding["decision"], "acknowledge_all_reported_feedback")
        self.assertEqual(binding["old_cursor_sha256"], hashlib.sha256(
            b"uninitialized"
        ).hexdigest())
        self.assertEqual(binding["new_cursor_sha256"], hashlib.sha256(
            self.candidate.encode()
        ).hexdigest())
        self.assertEqual(
            set(self.delivery.producer_reference_closure(
                self.root,
                receipt["producer_ref"],
                identity=self.agent_identity,
            )),
            {
                source_ref,
                receipt["producer_ref"],
                str(Path(source_ref).with_name(
                    "emitted-" + hashlib.sha256(original["emitted"]).hexdigest() + ".json"
                )),
                str(Path(receipt["producer_ref"]).with_name(
                    "emitted-" + hashlib.sha256(derivative["emitted"]).hexdigest() + ".json"
                )),
            },
        )

    def test_preserve_decision_does_not_require_or_adopt_candidate(self) -> None:
        report_raw = (
            '{ "reason" : "checks_failed", "url" : '
            f'"{self.url}" }}'
        )
        source_ref = self._source(report_raw=report_raw)
        receipt = self._derive(
            source_ref,
            decision="preserve_acknowledged_cursor",
        )
        derivative = self.delivery.reconstruct_producer(
            self.root,
            receipt["producer_ref"],
            identity=self.agent_identity,
        )
        self.assertEqual(derivative["result"]["pr_feedback_cursor"], "uninitialized")
        self.assertEqual(derivative["result"]["pr_report"], report_raw)
        decision = json.loads(derivative["binding"]["feedback_decision"])
        self.assertEqual(decision["old_cursor_sha256"], decision["new_cursor_sha256"])

    def test_preserve_decision_rejects_present_foreign_or_malformed_candidate(self) -> None:
        foreign_cursor = runtime.canonical_bytes(
            runtime.make_dynamic_pr_feedback_cursor(
                repository="other/repository",
                pull_number=17,
                items=[],
                review_decision="CHANGES_REQUESTED",
            )
        ).decode()
        reports = (
            '{ "reason":"checks_failed", "url":' + json.dumps(self.url)
            + ', "observed_feedback_cursor":"not-canonical" }',
            '{ "reason":"checks_failed", "url":' + json.dumps(self.url)
            + ', "observed_feedback_cursor":' + json.dumps(foreign_cursor) + ' }',
        )
        for report_raw in reports:
            with self.subTest(report=report_raw):
                source_ref = self._source(report_raw=report_raw)
                with self.assertRaises(ValueError):
                    self._derive(
                        source_ref,
                        decision="preserve_acknowledged_cursor",
                    )

    def test_decision_request_rejects_unhashable_decision(self) -> None:
        source_ref = self._source()
        with self.assertRaisesRegex(ValueError, "invalid feedback decision request values"):
            self._derive(source_ref, decision=["acknowledge_all_reported_feedback"])

    def test_ci_waiter_state_change_is_also_a_qualified_source(self) -> None:
        source_ref = self._source(
            producer_kind="wait-ci",
            transition="ci_watch_state_changed",
        )
        receipt = self._derive(
            source_ref,
            decision="acknowledge_all_reported_feedback",
        )
        derivative = self.delivery.reconstruct_producer(
            self.root,
            receipt["producer_ref"],
            identity=self.agent_identity,
        )
        self.assertEqual(derivative["producer_kind"], "feedback-decision")
        self.assertEqual(
            derivative["result"]["pr_feedback_cursor"],
            self.candidate,
        )

    def test_invalid_request_report_cursor_identity_and_ancestry_block(self) -> None:
        source_ref = self._source()
        good_report = (
            '{ "reason" : "change", "url" : '
            f'"{self.url}", "observed_feedback_cursor" : {json.dumps(self.candidate)} }}'
        )
        foreign_cursor = runtime.canonical_bytes(
            runtime.make_dynamic_pr_feedback_cursor(
                repository="other/repository",
                pull_number=17,
                items=[],
                review_decision="CHANGES_REQUESTED",
            )
        ).decode()
        invalid_reports = (
            '{"reason":"one","reason":"two"}',
            '{ "reason":"change", "url":"https://github.com/other/repository/pull/17", '
            f'"observed_feedback_cursor":{json.dumps(self.candidate)} }}',
            '{ "reason":"change", "url":' + json.dumps(self.url)
            + ', "observed_feedback_cursor":"not-canonical" }',
            '{ "reason":"change", "url":' + json.dumps(self.url)
            + ', "observed_feedback_cursor":' + json.dumps(foreign_cursor) + ' }',
        )
        for report_raw in invalid_reports:
            with self.subTest(report=report_raw):
                ref = self._source(report_raw=report_raw)
                with self.assertRaises(ValueError):
                    self._derive(
                        ref,
                        decision="acknowledge_all_reported_feedback",
                    )

        ref = self._source(result_cursor="a" * 64)
        with self.assertRaisesRegex(ValueError, "rewrote the acknowledged cursor"):
            self._derive(ref, decision="preserve_acknowledged_cursor")

        with self.assertRaises(ValueError):
            self._source(
                binding={"producer_refs": '{"refs":["not-a-producer-ref"]}'}
            )
        with self.assertRaisesRegex(ValueError, "invalid feedback decision request"):
            self.module.derive_feedback_decision(
                self.root,
                "TASK-17",
                {
                    "schema": "workflow-feedback-decision-request-v1",
                    "producer_ref": source_ref,
                    "decision": "acknowledge_all_reported_feedback",
                    "reason": "considered",
                    "identity": self.agent_identity,
                },
            )

    def test_source_must_be_waiter_transition_in_actual_native_graph(self) -> None:
        source_ref = self._source(
            producer_kind="prepare-ci",
            transition="ci_prepare_ready_initial",
        )
        with self.assertRaises(ValueError):
            self._derive(source_ref, decision="preserve_acknowledged_cursor")
        source_ref = self._source()
        self.graph = self._graph(
            "native-merge-watch",
            "other_script",
            "merge_watch_state_changed",
            ".kent/scripts/workflow-wait-github-pr",
        )
        with self.assertRaises(ValueError):
            self._derive(source_ref, decision="preserve_acknowledged_cursor")

    def test_checkpoint_command_emits_only_derivative_receipt(self) -> None:
        receipt = {
            "schema": "workflow-feedback-decision-receipt-v1",
            "producer_ref": "build/kent-workflow/TASK-17/packets/base-" + "a" * 64 + ".json",
            "source_ref": "build/kent-workflow/TASK-17/packets/base-" + "b" * 64 + ".json",
            "decision": "preserve_acknowledged_cursor",
            "old_cursor_sha256": "c" * 64,
            "new_cursor_sha256": "c" * 64,
        }
        stdout = io.StringIO()
        with (
            mock.patch.object(
                self.module,
                "read_json_input",
                return_value={"schema": "workflow-feedback-decision-request-v1"},
            ),
            mock.patch.object(
                self.module,
                "derive_feedback_decision",
                return_value=receipt,
            ),
            mock.patch.object(self.module.sys, "stdout", stdout),
        ):
            self.assertEqual(
                self.module.derive_feedback_decision_command(
                    self.root,
                    "TASK-17",
                ),
                0,
            )
        self.assertEqual(json.loads(stdout.getvalue()), receipt)
        self.assertNotIn('"transition"', stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
