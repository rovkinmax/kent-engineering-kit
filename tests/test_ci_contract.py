from __future__ import annotations

import os
from pathlib import Path
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from workflowkit import runtime
from workflowkit import ci_contract
from workflowkit.release import NormalizedGitHubWorkflowSourceV1
from workflowkit.ci_contract import (
    CiContractError,
    normalize_github_workflow,
    preparation_failure,
    prepare_ci_payload,
)

from tests import test_revision as revision_fixtures


ROOT = Path(__file__).resolve().parents[1]


def source_validation_copy_ignore(directory, names):
    ignored = shutil.ignore_patterns(".git", "__pycache__")(directory, names)
    if Path(directory) == ROOT:
        ignored.add("build")
    elif Path(directory) == ROOT / ".kent":
        ignored.add("runtime")
    return ignored


class CiContractTest(unittest.TestCase):
    def test_prepare_cli_capture_receives_original_input_before_unwrap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            scripts = root / ".kent/scripts"
            scripts.mkdir(parents=True)
            (root / ".kent/workflow-profile.toml").write_text(
                # Keep this producer-capture fixture on the legacy wire
                # contract; current reliability profiles require native
                # Script/graph identity before CI preparation.
                '[policies]\ndelivery_reliability = "legacy"\n')
            (scripts / "workflow-checkpoint").write_text(
                "import json\n"
                "def emit_producer_result(root, payload, result, producer_kind):\n"
                "    assert producer_kind == 'prepare-ci'\n"
                "    assert 'pr_url' not in payload\n"
                "    assert payload['delivery_context'].startswith(' {')\n"
                "    output = json.loads(result)\n"
                "    output['commentary'] = 'capture-observed'\n"
                "    return json.dumps(output).encode()\n")
            context = ' {"schema":"workflow-delivery-context-v1","phase":"post_pr",' \
                      '"pr_url":"https://github.com/owner/repository/pull/17",' \
                      '"branch_name":"feature/TASK-17","merge_strategy":"rebase",' \
                      '"pr_feedback_cursor":"uninitialized"} '
            result = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts/prepare-github-ci")],
                cwd=root, input=json.dumps({
                    "workspace_path": str(root), "task_short_id": "TASK-17",
                    "delivery_context": context}), text=True, capture_output=True,
                env={**os.environ, "KENT_GH_BIN": "/nonexistent-gh"}, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["commentary"], "capture-observed")

    @mock.patch.dict(os.environ, {
        "KENT_SESSION_ID": "11111111-1111-4111-8111-111111111111",
        "KENT_STEP_ID": "22222222-2222-4222-8222-222222222222",
    })
    def test_ci_archive_uses_recovered_logical_view_and_ignores_suffix_refs(self):
        from tests.test_workflow_runtime import WorkflowEvidenceLedgerTest, load_template_module
        from tests.test_runtime_contracts import dynamic_contract

        fixture = WorkflowEvidenceLedgerTest()
        self.addCleanup(fixture.doCleanups)
        root, scripts, directory, original = fixture.recovered_fixture()
        ledger = load_template_module(scripts / "workflow-evidence-ledger", "ci_recovered_view")
        contract = dynamic_contract()
        report = runtime.build_dynamic_ci_report(
            contract=contract, attempts=[{
                "sequence": 1, "head_oid": contract["head_oid"], "base_oid": contract["base_oid"],
                "retry": None, "checks": [], "check_count": 0,
                "checks_sha256": runtime.canonical_sha256([]), "reason": "no_checks",
                "watcher_exit_code": 0, "error": None,
            }],
        )
        payload = {
            "node_key": "ci_prepare", "evidence_type": "ci_report",
            "summary": "actual recovered report", "artifacts": [], "checks": [], "decisions": [],
            "context": {"manifest_path": ".kent/context/implement.md", "files_read": []},
        }
        with mock.patch.dict(os.environ, {
            "KENT_RUN_ID": "stale-ignored",
            "KENT_STEP_ID": "11111111-1111-4111-8111-111111111111",
        }), mock.patch.object(
            ledger, "read_json_input", side_effect=lambda _: payload,
        ):
            # A fresh append happens through the real template module below;
            # the CI producer's source-only template shares the pure validator.
            payload["artifacts"] = [f".kent/runtime/TASK-4/ci-report-{runtime.canonical_sha256(report)}.json"]
            ledger.append_entry(root, directory / "evidence-ledger.jsonl", task="TASK-4")
        with mock.patch.object(ci_contract.subprocess, "run", wraps=subprocess.run) as effects:
            artifact = ci_contract.archive_ci_report(root, "TASK-4", report)
        self.assertEqual(artifact, payload["artifacts"][0])
        self.assertFalse(any("append" in call.args[0] for call in effects.call_args_list))
        self.assertEqual((directory / "evidence-ledger.jsonl").read_bytes(), original)
        (root / ".kent/context/delivery.md").write_text("actual CI manifest\n")
        second_report = json.loads(json.dumps(report))
        second_report["contract"]["pull_number"] += 1
        with mock.patch.dict(os.environ, {
            "KENT_RUN_ID": "stale-ignored",
            "KENT_STEP_ID": "22222222-2222-4222-8222-222222222222",
        }):
            second_artifact = ci_contract.archive_ci_report(root, "TASK-4", second_report)
        records = ledger.read_entries(directory / "evidence-ledger.jsonl", task="TASK-4")
        self.assertIn(second_artifact, records[-1]["artifacts"])
        self.assertEqual((directory / "evidence-ledger.jsonl").read_bytes(), original)
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        (directory / "evidence-continuation.jsonl").write_bytes(b"broken")
        third_report = json.loads(json.dumps(second_report))
        third_report["contract"]["pull_number"] += 1
        with self.assertRaises(ValueError):
            ci_contract.archive_ci_report(root, "TASK-4", third_report)
        self.assertEqual(set(path.name for path in directory.iterdir()), set(before))

    def test_delivery_context_unwraps_before_identity_and_failure_routing(self) -> None:
        context = {
            "schema": "workflow-delivery-context-v1",
            "phase": "post_pr",
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "feature/TASK-17",
            "merge_strategy": "rebase",
            "pr_feedback_cursor": "uninitialized",
            "ci_contract": "malformed-but-preserved",
            "ci_report": ' { "schema" : "invalid" } ',
        }
        payload = {
            "workspace_path": str(ROOT),
            "task_short_id": "TASK-17",
            "delivery_context": json.dumps(context),
        }
        with mock.patch.object(
            ci_contract,
            "_prepare_ci_payload",
            return_value={"transition": "prepared"},
        ) as prepare:
            result = prepare_ci_payload(payload)
        self.assertEqual(result, {"transition": "prepared"})
        passed = prepare.call_args.args[0]
        for key in (
            "pr_url",
            "branch_name",
            "merge_strategy",
            "pr_feedback_cursor",
            "ci_contract",
            "ci_report",
        ):
            self.assertEqual(passed[key], context[key])
        self.assertNotIn("delivery_context", passed)
        self.assertEqual(passed["workspace_path"], str(ROOT))
        self.assertEqual(passed["task_short_id"], "TASK-17")

        failure = preparation_failure(payload)
        self.assertEqual(failure["transition"], "ci_prepare_failed")
        self.assertEqual(failure["pr_url"], context["pr_url"])
        self.assertEqual(failure["ci_contract"], context["ci_contract"])
        self.assertEqual(failure["ci_report"], context["ci_report"])

    def test_delivery_context_rejects_contradictory_mixed_flat_values(self) -> None:
        context = {
            "schema": "workflow-delivery-context-v1",
            "phase": "post_pr",
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "feature/TASK-17",
            "merge_strategy": "rebase",
            "pr_feedback_cursor": "uninitialized",
        }
        payload = {
            "workspace_path": str(ROOT),
            "task_short_id": "TASK-17",
            "delivery_context": json.dumps(context),
            "branch_name": "feature/TASK-18",
        }
        with self.assertRaises(CiContractError):
            prepare_ci_payload(payload)
        with self.assertRaises(CiContractError):
            preparation_failure(payload)

    def test_context_packet_strings_reach_existing_ci_diagnostic_validator(self) -> None:
        context = {
            "schema": "workflow-delivery-context-v1",
            "phase": "post_pr",
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "feature/TASK-17",
            "merge_strategy": "rebase",
            "pr_feedback_cursor": "uninitialized",
            "ci_contract": "malformed-but-still-a-string",
        }
        payload = {
            "workspace_path": str(ROOT),
            "task_short_id": "TASK-17",
            "delivery_context": json.dumps(context),
        }
        pull_request = {
            "url": context["pr_url"],
            "headRefName": context["branch_name"],
            "state": "OPEN",
            "baseRefName": "main",
            "headRefOid": "a" * 40,
            "baseRefOid": "b" * 40,
        }
        with mock.patch.object(
            ci_contract,
            "read_pull_request",
            return_value=pull_request,
        ):
            with self.assertRaisesRegex(
                CiContractError,
                "previous CI contract is invalid",
            ):
                prepare_ci_payload(payload)

    def test_prepare_ci_cli_routes_context_identity_after_unwrap(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        workspace = Path(temporary.name).resolve()
        # Exercise legacy routing independently of the Kit's opted-in profile.
        context = {
            "schema": "workflow-delivery-context-v1",
            "phase": "post_pr",
            "pr_url": "https://github.com/owner/repository/pull/17",
            "branch_name": "feature/TASK-17",
            "merge_strategy": "rebase",
            "pr_feedback_cursor": "uninitialized",
        }
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts/prepare-github-ci")],
            input=json.dumps({
                "workspace_path": str(workspace),
                "task_short_id": "TASK-17",
                "delivery_context": json.dumps(context),
            }),
            cwd=workspace,
            text=True,
            capture_output=True,
            timeout=10,
            env={**os.environ, "KENT_GH_BIN": "/nonexistent-gh"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["transition"], "ci_prepare_failed")
        self.assertEqual(output["pr_url"], context["pr_url"])
        self.assertEqual(output["branch_name"], context["branch_name"])
        self.assertEqual(output["pr_feedback_cursor"], "uninitialized")
        self.assertEqual(output["ci_contract"], "")
        self.assertEqual(output["ci_report"], "")

    def test_prepare_ci_cli_does_not_fallback_from_invalid_context_to_flat_values(self) -> None:
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts/prepare-github-ci")],
            input=json.dumps({
                "workspace_path": str(ROOT),
                "task_short_id": "TASK-17",
                "pr_url": "https://github.com/owner/repository/pull/17",
                "branch_name": "feature/TASK-17",
                "merge_strategy": "rebase",
                "pr_feedback_cursor": "uninitialized",
                "delivery_context": '{"schema":"wrong","phase":"pre_pr"}',
            }),
            text=True,
            capture_output=True,
            timeout=10,
            env={**os.environ, "KENT_GH_BIN": "/nonexistent-gh"},
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            json.loads(result.stdout),
            {"error": "ci_prepare_input_invalid"},
        )

    def test_runner_assertion_accepts_only_supported_unconditional_forms(self) -> None:
        assertion = 'test "${RUNNER_ENVIRONMENT:-github-hosted}" = github-hosted'
        for step, asserted in (
            ({"run": assertion}, True),
            ({"run": assertion, "if": "false"}, False),
            ({"run": assertion, "continue-on-error": True}, False),
            ({"run": assertion, "shell": "pwsh"}, False),
            ({"run": f"echo {assertion!r}"}, False),
            ({"run": f"{assertion} || true"}, False),
        ):
            with self.subTest(step=step):
                raw = json.dumps({
                    "name": "PR", "on": "pull_request",
                    "jobs": {"build": {"runs-on": "ubuntu-latest", "steps": [step]}},
                }).encode()
                job = normalize_github_workflow(raw, ".github/workflows/ci.json")["jobs"][0]
                self.assertEqual(job["runner_environment_asserted"], asserted)

    def test_cli_rejects_invalid_identity_and_bounded_json_without_raw_leaks(self) -> None:
        for raw in (
            "[]", '{"workspace_path":"/a","workspace_path":"/b"}',
            "[" * 110 + "0" + "]" * 110, " " * (1024 * 1024 + 1),
            '{"workspace_path":42,"pr_url":"secret-value"}',
        ):
            result = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts/prepare-github-ci")],
                input=raw, text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout), {"error": "ci_prepare_input_invalid"})
            self.assertEqual(result.stderr, "")

    def test_preparation_failure_retry_does_not_fabricate_a_ci_report(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        workspace = Path(temporary.name).resolve()
        # Retry remains a legacy CLI scenario, without native delivery capture.
        payload = {
            "workspace_path": str(workspace),
            "pr_url": "https://github.com/owner/repository/pull/1",
            "branch_name": "TASK-1", "merge_strategy": "rebase",
            "task_short_id": "TASK-1",
        }
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts/prepare-github-ci")],
                input=json.dumps(payload), cwd=workspace,
                text=True, capture_output=True, timeout=10,
                env={**os.environ, "KENT_GH_BIN": "/nonexistent-gh"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["transition"], "ci_prepare_failed")
            self.assertEqual(payload["ci_contract"], "")
            self.assertEqual(payload["ci_report"], "")
            self.assertNotIn("diagnostic-v1", result.stdout)

    def test_dynamic_prepare_emits_identity_contract_without_source_preflight(self) -> None:
        payload = {
            "workspace_path": str(ROOT),
            "pr_url": "https://github.com/owner/repository/pull/1",
            "branch_name": "TASK-1",
            "merge_strategy": "rebase",
            "task_short_id": "TASK-1",
            "pr_feedback_cursor": "uninitialized",
        }
        head = "a" * 40
        base = "b" * 40
        pr = {
            "state": "OPEN",
            "headRefOid": head,
            "baseRefOid": base,
            "baseRefName": "main",
            "headRefName": "TASK-1",
            "url": payload["pr_url"],
            "mergedAt": None,
            "mergeCommit": None,
            "statusCheckRollup": [],
        }
        with mock.patch.object(ci_contract, "read_pull_request", side_effect=[pr, pr]):
            prepared = prepare_ci_payload(payload, gh_bin="/bin/true")
        self.assertEqual(prepared["transition"], "ci_prepare_ready_initial")
        self.assertEqual(
            json.loads(prepared["ci_contract"]),
            {
                "schema": "github-ci-contract-v1",
                "repository": "owner/repository",
                "pull_number": 1,
                "head_oid": head,
                "base_oid": base,
                "require_ci": True,
            },
        )
        self.assertEqual(prepared["pr_feedback_cursor"], "uninitialized")
        self.assertNotIn("ci_report", prepared)
        self.assertNotIn("pr_head_oid", prepared)
        self.assertNotIn("pr_base_oid", prepared)
        self.assertEqual(
            set(prepared),
            {
                "transition",
                "workspace_path",
                "pr_url",
                "branch_name",
                "merge_strategy",
                "task_short_id",
                "ci_contract",
                "pr_feedback_cursor",
            },
        )
        for legacy in (
            "expected_ci_checks",
            "expected_ci_checks_sha256",
            "runtime_source_envelope_digest",
            "ci_policy_snapshot",
        ):
            self.assertNotIn(legacy, prepared)

    def test_dynamic_prepare_retries_same_contract_and_resets_changed_identity(self) -> None:
        payload = {
            "workspace_path": str(ROOT),
            "pr_url": "https://github.com/owner/repository/pull/1",
            "branch_name": "TASK-1",
            "merge_strategy": "rebase",
            "task_short_id": "TASK-1",
            "pr_feedback_cursor": "uninitialized",
        }
        head = "a" * 40
        base = "b" * 40
        pr = {
            "state": "OPEN",
            "headRefOid": head,
            "baseRefOid": base,
            "baseRefName": "main",
            "headRefName": "TASK-1",
            "url": payload["pr_url"],
            "mergedAt": None,
            "mergeCommit": None,
            "statusCheckRollup": [{"name": "external"}],
        }
        contract = {
            "schema": "github-ci-contract-v1",
            "repository": "owner/repository",
            "pull_number": 1,
            "head_oid": head,
            "base_oid": base,
            "require_ci": True,
        }
        check = {
            "workflow_name": "",
            "check_name": "external",
            "event": "",
            "bucket": "pass",
            "state": "SUCCESS",
            "link": "https://app.aikido.dev/checks/1",
        }
        attempt = {
            "sequence": 1,
            "head_oid": head,
            "base_oid": base,
            "retry": None,
            "checks": [check],
            "check_count": 1,
            "checks_sha256": runtime.canonical_sha256([check]),
            "reason": "green",
            "watcher_exit_code": 0,
            "error": None,
        }
        report = runtime.build_dynamic_ci_report(
            contract=contract,
            attempts=[attempt],
        )
        retry_payload = {
            **payload,
            "ci_contract": runtime.canonical_bytes(contract).decode("utf-8"),
            "ci_report": runtime.canonical_bytes(report).decode("utf-8"),
        }
        with (
            mock.patch.object(ci_contract, "read_pull_request", side_effect=[pr, pr]),
            mock.patch.object(ci_contract, "archive_ci_report") as archive,
        ):
            retried = prepare_ci_payload(retry_payload, gh_bin="/bin/true")
        self.assertEqual(retried["transition"], "ci_prepare_ready_retry")
        self.assertEqual(retried["ci_report"], retry_payload["ci_report"])
        self.assertNotIn("pr_head_oid", retried)
        self.assertNotIn("pr_base_oid", retried)
        self.assertEqual(
            set(retried),
            {
                "transition",
                "workspace_path",
                "pr_url",
                "branch_name",
                "merge_strategy",
                "task_short_id",
                "ci_contract",
                "pr_feedback_cursor",
                "ci_report",
            },
        )
        archive.assert_called_once()

        for observed_key, contract_key in (("headRefOid", "head_oid"), ("baseRefOid", "base_oid")):
            changed_pr = {**pr, observed_key: "c" * 40}
            with (
                self.subTest(changed=observed_key),
                mock.patch.object(
                    ci_contract, "read_pull_request", side_effect=[changed_pr, changed_pr],
                ),
                mock.patch.object(ci_contract, "archive_ci_report") as archive_changed,
            ):
                fresh = prepare_ci_payload(retry_payload, gh_bin="/bin/true")
                self.assertEqual(fresh["transition"], "ci_prepare_ready_initial")
                self.assertNotIn("ci_report", fresh)
                self.assertEqual(json.loads(fresh["ci_contract"])[contract_key], "c" * 40)
                archive_changed.assert_called_once_with(ROOT, "TASK-1", report)

        for drift in ({"headRefOid": "c" * 40}, {"baseRefOid": "c" * 40},
                      {"baseRefName": "release"}):
            with (
                self.subTest(drift=drift),
                mock.patch.object(ci_contract, "read_pull_request", side_effect=[pr, {**pr, **drift}]),
                mock.patch.object(ci_contract, "archive_ci_report") as archive,
                self.assertRaisesRegex(CiContractError, "identity changed"),
            ):
                prepare_ci_payload(retry_payload, gh_bin="/bin/true")
            archive.assert_not_called()
        merged = {
            **pr, "state": "MERGED", "mergedAt": "2026-09-06T12:00:00Z",
            "mergeCommit": {"oid": "d" * 40},
        }
        for observations in ([merged], [pr, merged]):
            with (
                self.subTest(observations=len(observations)),
                mock.patch.object(ci_contract, "read_pull_request", side_effect=observations),
                mock.patch.object(ci_contract, "archive_ci_report") as archive,
            ):
                recovered = prepare_ci_payload(retry_payload, gh_bin="/bin/true")
                self.assertEqual(recovered["transition"], "ci_prepare_pr_merged")
                self.assertEqual(json.loads(recovered["merge_report"]), merged)
                self.assertEqual(
                    set(recovered),
                    {
                        "transition",
                        "workspace_path",
                        "pr_url",
                        "branch_name",
                        "merge_report",
                    },
                )
                archive.assert_called_once_with(ROOT, "TASK-1", report)

    def test_dynamic_prepare_rejects_noncanonical_packets_without_rewriting(self):
        contract = {
            "schema": "github-ci-contract-v1",
            "repository": "owner/repository",
            "pull_number": 17,
            "head_oid": "a" * 40,
            "base_oid": "b" * 40,
            "require_ci": True,
        }
        attempt = {
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
        }
        report = runtime.build_dynamic_ci_report(
            contract=contract,
            attempts=[attempt],
        )
        cursor = runtime.make_dynamic_pr_feedback_cursor(
            repository="owner/repository",
            pull_number=17,
            items=[],
        )
        pr_url = "https://github.com/owner/repository/pull/17"
        pr = {
            "state": "OPEN",
            "headRefOid": contract["head_oid"],
            "baseRefOid": contract["base_oid"],
            "baseRefName": "main",
            "headRefName": "TASK-17",
            "url": pr_url,
            "mergedAt": None,
            "mergeCommit": None,
            "statusCheckRollup": [],
        }
        packets = {
            "ci_contract": json.dumps(contract, indent=2),
            "ci_report": json.dumps(report, indent=2),
            "pr_feedback_cursor": json.dumps(cursor, indent=2),
        }
        for field, raw in packets.items():
            with self.subTest(field=field):
                payload = {
                    "workspace_path": str(ROOT),
                    "pr_url": pr_url,
                    "branch_name": "TASK-17",
                    "merge_strategy": "rebase",
                    "task_short_id": "TASK-17",
                    "pr_feedback_cursor": "uninitialized",
                    "ci_contract": runtime.canonical_bytes(contract).decode("utf-8"),
                    field: raw,
                }
                with mock.patch.object(
                    ci_contract,
                    "read_pull_request",
                    return_value=pr,
                ), self.assertRaisesRegex(CiContractError, "invalid"):
                    prepare_ci_payload(payload, gh_bin="/bin/true")
                diagnosis = preparation_failure(payload)
                self.assertEqual(diagnosis[field], raw)

    @mock.patch.dict(os.environ, {
        "KENT_SESSION_ID": "11111111-1111-4111-8111-111111111111",
        "KENT_STEP_ID": "22222222-2222-4222-8222-222222222222",
    })
    def test_archive_cli_requires_nested_v3_contract_identity(self) -> None:
        contract = {
            "schema": "github-ci-contract-v1",
            "repository": "owner/repository",
            "pull_number": 1,
            "head_oid": "a" * 40,
            "base_oid": "b" * 40,
            "require_ci": True,
        }
        check = {
            "workflow_name": "",
            "check_name": "external",
            "event": "",
            "bucket": "pass",
            "state": "SUCCESS",
            "link": "https://app.aikido.dev/checks/1",
        }
        attempt = {
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
        }
        report = runtime.build_dynamic_ci_report(
            contract=contract,
            attempts=[attempt],
        )
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Kent Test"],
                cwd=workspace,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "kent@example.invalid"],
                cwd=workspace,
                check=True,
            )
            (workspace / ".gitignore").write_text("/.kent/runtime/\n")
            (workspace / ".kent/context").mkdir(parents=True)
            (workspace / ".kent/context/delivery.md").write_text("# Delivery\n")
            # An old helper/graph uses the legacy archive operation without
            # native Script context. A schema-3 profile remains on that path.
            (workspace / ".kent/workflow-profile.toml").write_bytes(
                (ROOT / "contracts/project-profile.example.toml").read_bytes()
            )
            (workspace / "README.md").write_text("test\n")
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "test"],
                cwd=workspace,
                check=True,
            )
            base = {
                "operation": "archive_ci_report",
                "workspace_path": str(workspace),
                "pr_url": "https://github.com/owner/repository/pull/1",
                "branch_name": "TASK-1",
                "merge_strategy": "rebase",
                "task_short_id": "TASK-1",
                "ci_report": json.dumps(report, separators=(",", ":")),
            }
            accepted = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts/prepare-github-ci")],
                input=json.dumps(base),
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertIn("ci_report_artifact", json.loads(accepted.stdout))
            wrong_identity = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts/prepare-github-ci")],
                input=json.dumps({
                    **base,
                    "pr_url": "https://github.com/other/repository/pull/1",
                }),
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(wrong_identity.returncode, 2)
            self.assertEqual(
                json.loads(wrong_identity.stdout),
                {"error": "ci_report_archive_failed"},
            )
            legacy = {
                **base,
                "ci_report": json.dumps({
                    "schema": "github-ci-report-v2",
                }),
            }
            rejected_legacy = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts/prepare-github-ci")],
                input=json.dumps(legacy),
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(rejected_legacy.returncode, 2)
            self.assertEqual(
                json.loads(rejected_legacy.stdout),
                {"error": "ci_report_archive_failed"},
            )

    def test_metadata_requires_exact_identity_open_state_and_merge_proof(self) -> None:
        payload = {"pr_url": "https://github.com/owner/repository/pull/1", "branch_name": "TASK-1"}
        metadata = {
            "state": "OPEN", "url": payload["pr_url"], "headRefName": "TASK-1",
            "baseRefName": "main", "headRefOid": "a" * 40, "baseRefOid": "b" * 40,
        }
        ci_contract._validate_pr_metadata(metadata, payload)
        for changes in (
            {"url": "https://github.com/other/repository/pull/1"},
            {"headRefName": "other"}, {"state": "CLOSED"},
            {"state": "MERGED"}, {"baseRefOid": []},
        ):
            with self.subTest(changes=changes), self.assertRaises(CiContractError):
                ci_contract._validate_pr_metadata({**metadata, **changes}, payload)
        ci_contract._validate_pr_metadata({
            **metadata, "state": "MERGED", "mergedAt": "2026-09-06T12:00:00Z",
            "mergeCommit": {"oid": "c" * 40},
        }, payload)

    def test_bounded_readers_kill_owned_grandchildren_when_parent_exits(self) -> None:
        from workflowkit import revision

        popen = subprocess.Popen
        for reader in ("ci", "revision"):
            for failure in ("timeout", "overflow", "success_orphan"):
                with self.subTest(reader=reader, failure=failure), tempfile.TemporaryDirectory() as temporary:
                    pid_path = Path(temporary) / "grandchild.pid"
                    child = (
                        "import os, signal, time; "
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        f"open({str(pid_path)!r}, 'w').write(str(os.getpid())); "
                        + ("os.write(1, b'x' * 2048); " if failure == "overflow" else "")
                        + ("os.close(1); os.close(2); " if failure == "success_orphan" else "")
                        + "time.sleep(60)"
                    )
                    parent = (
                        "import subprocess, sys, time; "
                        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
                        + (
                            f"\nfrom pathlib import Path\nwhile not Path({str(pid_path)!r}).exists(): time.sleep(0.001)"
                            if failure == "success_orphan" else "time.sleep(60)"
                        )
                    )
                    command = [sys.executable, "-c", parent]
                    unrelated = popen(
                        [sys.executable, "-c", "import time; time.sleep(60)"],
                        start_new_session=True,
                    )
                    try:
                        if reader == "ci":
                            with self.assertRaises(ci_contract.BoundedCommandError):
                                ci_contract.run_bounded_command(
                                    command, timeout=0.5, output_limit=1024,
                                )
                        else:
                            with (
                                mock.patch.object(
                                    revision.subprocess, "Popen",
                                    side_effect=lambda *a, **kw: popen(command, **kw),
                                ),
                                mock.patch.object(revision, "GIT_COMMAND_TIMEOUT_SECONDS", 0.5),
                                mock.patch.object(revision, "GIT_COMMAND_OUTPUT_BYTES", 1024),
                                self.assertRaises(revision.RevisionPreflightError),
                            ):
                                revision.run_git_bytes(Path(temporary), "cat-file", "blob", "HEAD:x")
                        self.assertTrue(pid_path.exists(), "descendant did not start")
                        pid = int(pid_path.read_text())
                        deadline = time.monotonic() + 2
                        while time.monotonic() < deadline:
                            state = subprocess.run(
                                ["ps", "-o", "stat=", "-p", str(pid)],
                                capture_output=True, text=True,
                            ).stdout.strip()
                            if not state or state.startswith("Z"):
                                break
                            time.sleep(0.02)
                        self.assertTrue(not state or state.startswith("Z"), state)
                        self.assertIsNone(unrelated.poll())
                    finally:
                        if pid_path.exists():
                            try:
                                os.kill(int(pid_path.read_text()), signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        unrelated.kill()
                        unrelated.wait(timeout=5)

    def test_normalizer_emits_actual_typed_observations(self) -> None:
        raw = b"""name: PR
on: pull_request
permissions:
  contents: write
  issues: write
env:
  WORKFLOW: value
defaults:
  run:
    shell: bash
    working-directory: src
jobs:
  build:
    name: Build
    runs-on: ubuntu-latest
    permissions:
      contents: read
    env:
      JOB: value
      TOKEN: ${{ secrets.TOKEN }}
    steps:
      - run: echo ok
        env:
          STEP: value
"""
        value = normalize_github_workflow(raw, ".github/workflows/ci.yml")
        source = NormalizedGitHubWorkflowSourceV1.from_dict(
            {key: item for key, item in value.items() if key != "workflow_name"}
        )
        self.assertEqual(source.permissions, {"contents": "write", "issues": "write"})
        job = source.jobs[0]
        self.assertEqual(job.effective_permissions, {"contents": "read"})
        self.assertEqual(job.secret_refs, ("TOKEN",))
        self.assertEqual(job.steps[0].secret_refs, ("TOKEN",))
        self.assertEqual(job.steps[0].effective_shell, "bash")
        self.assertEqual(job.steps[0].effective_working_directory, "src")
        self.assertEqual(
            job.steps[0].effective_environment,
            {"WORKFLOW": "value", "TOKEN": "${{ secrets.TOKEN }}",
             "JOB": "value", "STEP": "value"},
        )
        self.assertFalse(job.runner_environment_asserted)

    def test_unselected_matrix_does_not_block_required_jobs(self) -> None:
        raw = b"""name: PR
on: pull_request
permissions: {contents: read}
jobs:
  build:
    name: Build
    runs-on: ubuntu-latest
    steps:
      - run: echo ok
  extra:
    strategy:
      matrix: {os: [linux, macos]}
    runs-on: ${{ matrix.os }}
    steps:
      - run: echo extra
"""
        value = normalize_github_workflow(
            raw, ".github/workflows/ci.yml", selected_jobs=["build"],
        )
        self.assertEqual([job["job_key"] for job in value["jobs"]], ["build"])
        self.assertEqual(value["jobs"][0]["steps"][0]["effective_shell"], "")
        with self.assertRaises(CiContractError):
            normalize_github_workflow(
                raw, ".github/workflows/ci.yml", selected_jobs=["missing"],
            )

    def test_normalizer_rejects_ambiguous_masks_and_preserves_fixtures(self) -> None:
        base = {
            "name": "PR", "on": "pull_request",
            "jobs": {"build": {
                "name": "Build", "runs-on": "ubuntu-latest",
                "steps": [{"run": 'test "$RUNNER_ENVIRONMENT" = github-hosted'}],
            }},
        }
        from copy import deepcopy
        for field, value in (
            ("continue-on-error", "${{ inputs.ignore }}"),
            ("environment", {"name": "production", "url": "https://example.invalid"}),
            ("permissions", "write-all"),
        ):
            document = deepcopy(base)
            document["jobs"]["build"][field] = value
            with self.subTest(field=field), self.assertRaises(CiContractError):
                normalize_github_workflow(
                    json.dumps(document).encode(), ".github/workflows/ci.json",
                )
        document = deepcopy(base)
        image = "example/image@sha256:" + "a" * 64
        document["jobs"]["build"]["container"] = image
        document["jobs"]["build"]["services"] = {
            "db": {"image": image, "env": {"PORT": 1234}, "ports": ["1234:1234"]},
        }
        normalized = normalize_github_workflow(
            json.dumps(document).encode(), ".github/workflows/ci.json",
        )["jobs"][0]
        self.assertEqual(normalized["container"]["image"], image)
        self.assertEqual(normalized["services"]["db"]["environment"], {"PORT": 1234})
        self.assertEqual(normalized["services"]["db"]["ports"], ["1234:1234"])
        self.assertTrue(normalized["runner_environment_asserted"])
        document["jobs"]["build"]["steps"][0]["run"] = 'echo "$RUNNER_ENVIRONMENT"'
        normalized = normalize_github_workflow(
            json.dumps(document).encode(), ".github/workflows/ci.json",
        )["jobs"][0]
        self.assertFalse(normalized["runner_environment_asserted"])

    @mock.patch.dict(os.environ, {
        "KENT_SESSION_ID": "11111111-1111-4111-8111-111111111111",
        "KENT_STEP_ID": "22222222-2222-4222-8222-222222222222",
    })
    def test_prepare_ci_payload_and_cli_bind_real_target_and_head_revisions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(
                ["git", "init", "-q"],
                cwd=root,
                check=True,
            )
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
            profile = root / ".kent" / "workflow-profile.toml"
            profile.parent.mkdir(parents=True)
            (root / ".gitignore").write_text("/.kent/runtime/\n/fake-bin/\n")
            profile.write_text(revision_fixtures.schema4_profile_contents())
            (root / ".kent" / "project-contract.md").write_text(
                "# Project contract\n"
            )
            for relative in revision_fixtures.WORK_KIND_PROCEDURES + revision_fixtures.CONTEXT_MANIFESTS:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# Test procedure\n")
            scripts = root / ".kent" / "scripts"
            scripts.mkdir(parents=True)
            for name in (
                "workflow-checkpoint",
                "workflow-evidence-ledger",
                "workflow-plan-contract",
                "workflow-plan-contract-accept",
                "workflow-plan-contract-continue",
                "workflow-plan-contract-fix-continue",
                "workflow-plan-contract-verify",
                "workflow-task-janitor",
                "workflow-verification-dispatch",
                "workflow-verify",
                "workflow-wait-github-ci",
                "workflow-wait-github-pr",
            ):
                command = scripts / name
                command.write_text("#!/usr/bin/env bash\nexit 0\n")
                command.chmod(0o755)

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", *args],
                    cwd=root,
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout.strip()

            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "target"],
                cwd=root,
                check=True,
            )
            target = git("rev-parse", "HEAD")

            head_profile = revision_fixtures.schema4_profile_contents().replace(
                'kit_managed_commands = ["dispatch"]',
                'kit_managed_commands = ["runtime_contracts", "verify", '
                '"evidence", "janitor", "wait_pr", "wait_ci", '
                '"github_observation", "prepare_ci"]',
            ).replace(
                '[command_versions]\n'
                'dispatch = "1.2.3"\n',
                '[command_versions]\n'
                'runtime_contracts = "2.0.0"\n'
                'verify = "2.0.0"\n'
                'evidence = "2.0.0"\n'
                'janitor = "2.0.0"\n'
                'wait_pr = "3.0.0"\n'
                'wait_ci = "3.0.0"\n'
                'github_observation = "1.0.0"\n'
                'prepare_ci = "2.0.0"\n',
            ).replace(
                'dispatch = ".kent/scripts/workflow-verification-dispatch"\n',
                'dispatch = ".kent/scripts/workflow-verification-dispatch"\n'
                'runtime_contracts = ".kent/scripts/workflow_runtime_contracts.py"\n'
                'prepare_ci = ".kent/scripts/workflow-prepare-github-ci"\n',
            )
            profile.write_text(head_profile)
            (scripts / "workflow_runtime_contracts.py").write_bytes(
                (ROOT / "workflowkit" / "runtime.py").read_bytes()
            )
            (scripts / "workflow_runtime_contracts.py").chmod(0o755)
            for name in (
                "workflow-prepare-github-ci", "workflow-wait-github-ci",
                "workflow-wait-github-pr", "workflow-evidence-ledger",
            ):
                (scripts / name).write_bytes((ROOT / "templates/project" / name).read_bytes())
                (scripts / name).chmod(0o755)
            (scripts / "workflow_github_observation.py").write_bytes(
                (ROOT / "workflowkit" / "github_observation.py").read_bytes()
            )
            (scripts / "workflow_github_observation.py").chmod(0o755)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "head"],
                cwd=root,
                check=True,
            )
            head = git("rev-parse", "HEAD")

            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            fake_gh = fake_bin / "gh"
            fake_gh.write_text(
                "#!/bin/sh\n"
                "cat <<'JSON'\n"
                "{"
                f'"state":"OPEN","headRefOid":"{head}",'
                f'"baseRefOid":"{target}",'
                '"baseRefName":"main","headRefName":"TASK-1",'
                '"url":"https://github.com/owner/repository/pull/1"'
                "}\n"
                "JSON\n"
            )
            fake_gh.chmod(0o755)
            payload = {
                "workspace_path": str(root),
                "pr_url": "https://github.com/owner/repository/pull/1",
                "branch_name": "TASK-1",
                "merge_strategy": "rebase",
                "task_short_id": "TASK-1",
            }
            direct = prepare_ci_payload(payload, gh_bin=str(fake_gh))
            self.assertEqual(direct["transition"], "ci_prepare_ready_initial")
            self.assertNotIn("pr_head_oid", direct)
            self.assertNotIn("pr_base_oid", direct)
            self.assertEqual(
                json.loads(direct["ci_contract"]),
                {
                    "schema": "github-ci-contract-v1",
                    "repository": "owner/repository",
                    "pull_number": 1,
                    "head_oid": head,
                    "base_oid": target,
                    "require_ci": True,
                },
            )
            for legacy in (
                "expected_ci_checks",
                "expected_ci_checks_sha256",
                "runtime_source_envelope_digest",
                "ci_policy_snapshot",
            ):
                self.assertNotIn(legacy, direct)
            cli = subprocess.run(
                [str(ROOT / "scripts" / "prepare-github-ci")],
                cwd=root,
                input=json.dumps(payload),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                },
                check=False,
            )
            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertEqual(
                json.loads(cli.stdout)["transition"],
                "ci_prepare_ready_initial",
            )
            # Drive actual generated edge inputs through the first-party CLI,
            # installed-template consumers and the task evidence ledger.
            from workflowkit import build_delivery_workflow
            from workflowkit.profile import ProjectProfile
            graph = build_delivery_workflow(ProjectProfile.from_toml(
                root, head_profile, check_files=False,
            ), 1)
            edges = {edge.key: edge for edge in graph.edges}

            def transport(edge_key, values):
                keys = [parameter.key for parameter in edges[edge_key].parameters]
                self.assertFalse(set(keys) - values.keys(), edge_key)
                self.assertTrue(all(isinstance(values[key], str) for key in keys), edge_key)
                return {key: values[key] for key in keys}

            pr_state = {
                "state": "OPEN", "headRefOid": head, "baseRefOid": target,
                "baseRefName": "main", "headRefName": "TASK-1",
                "url": payload["pr_url"], "mergedAt": None, "mergeCommit": None,
                "reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN",
                "mergeable": "MERGEABLE",
                # The real PR rollup does NOT include workflow identity.
                "statusCheckRollup": [{"name": "Required Release", "conclusion": "SUCCESS"}],
            }
            observed = [
                {"name": "Required Release", "workflow": "Release",
                 "bucket": "pass", "state": "SUCCESS", "link": None},
                {"name": "Extra", "workflow": "Extras",
                 "bucket": "fail", "state": "FAILURE", "link": None},
            ]
            state_path = fake_bin / "state.json"
            checks_path = fake_bin / "checks.json"
            state_path.write_text(json.dumps(pr_state))
            checks_path.write_text(json.dumps(observed))
            comments_path = fake_bin / "comments.json"
            comments_path.write_text("[]")
            fake_gh.write_text(
                f"#!{sys.executable}\n"
                "import json, pathlib, sys\n"
                f"root = pathlib.Path({str(fake_bin)!r})\n"
                "args = sys.argv[1:]\n"
                "if args[:2] == ['pr', 'view']:\n"
                "    print((root / 'state.json').read_text())\n"
                "elif args[:2] == ['pr', 'checks']:\n"
                "    if '--watch' in args or '--required' in args: sys.exit(99)\n"
                "    rows = json.loads((root / 'checks.json').read_text())\n"
                "    print(json.dumps(rows))\n"
                "    sys.exit(8 if any(row['bucket'] == 'pending' for row in rows) else 1)\n"
                "elif args[:2] == ['api', 'graphql']:\n"
                "    print(json.dumps({'data': {'repository': {'pullRequest': "
                "{'reviewThreads': {'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}}))\n"
                "elif args[:1] == ['api']:\n"
                "    print(json.dumps([json.loads((root / 'comments.json').read_text()) "
                "if any('issues/' in arg for arg in args) else []]))\n"
                "else: sys.exit(2)\n"
            )
            environment = {
                **os.environ, "KENT_GH_BIN": str(fake_gh),
                "KENT_PREPARE_GITHUB_CI_BIN": str(ROOT / "scripts/prepare-github-ci"),
                "KENT_CI_WATCH_TEST_MODE": "1", "KENT_CI_WATCH_INTERVAL_SECONDS": "0",
                "KENT_CI_WATCH_MAX_POLLS": "1", "KENT_CI_WATCH_MAX_ERRORS": "1",
                "KENT_PR_WATCH_TEST_MODE": "1", "KENT_PR_WATCH_INTERVAL_SECONDS": "0",
                "KENT_PR_WATCH_MAX_POLLS": "1", "KENT_PR_WATCH_MAX_ERRORS": "1",
            }
            # A test run has no Kent evidence run identity; real runs preserve it.
            environment.pop("KENT_RUN_ID", None)
            environment.pop("PYTHONDONTWRITEBYTECODE", None)

            def invoke(command, values):
                result = subprocess.run(
                    [str(command)], input=json.dumps(values), text=True, capture_output=True,
                    cwd=root, env=environment, timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            delivery_context = json.dumps({
                "schema": "workflow-delivery-context-v1",
                "phase": "post_pr",
                "pr_url": payload["pr_url"],
                "branch_name": payload["branch_name"],
                "merge_strategy": payload["merge_strategy"],
                "pr_feedback_cursor": "uninitialized",
            })
            prepared = invoke(
                scripts / "workflow-prepare-github-ci",
                transport(
                    "prepare_pr_ci_prepare",
                    {
                        "workspace_path": str(root),
                        "task_short_id": payload["task_short_id"],
                        "delivery_context": delivery_context,
                    },
                ),
            )
            invalid_packet = invoke(
                scripts / "workflow-prepare-github-ci",
                {**prepared, "ci_report": '{"schema":"invalid"}'},
            )
            self.assertEqual(invalid_packet["transition"], "ci_prepare_failed")
            self.assertEqual(invalid_packet["ci_report"], '{"schema":"invalid"}')
            green = invoke(
                scripts / "workflow-wait-github-ci",
                transport("ci_prepare_ready_initial", prepared),
            )
            self.assertEqual(green["transition"], "ci_watch_failed", green)
            self.assertNotIn("ci_report", green)
            context = runtime.validate_delivery_context(json.loads(green["delivery_context"]))
            report = runtime.validate_dynamic_ci_report(json.loads(context["ci_report"]))
            self.assertEqual(report["schema"], "github-ci-report-v3")
            self.assertEqual(report["attempts"][-1]["reason"], "failed")
            self.assertEqual(len(report["attempts"][-1]["checks"]), 2)
            artifact = root / ".kent/runtime/TASK-1" / f"ci-report-{runtime.canonical_sha256(report)}.json"
            self.assertEqual(json.loads(artifact.read_text()), report)
            ledger = root / ".kent/runtime/TASK-1/evidence-ledger.jsonl"
            self.assertIn(str(artifact.relative_to(root)), ledger.read_text())
            self.assertEqual(git("status", "--porcelain"), "")

    def test_ruby_normalizer_rejects_aliases_duplicates_and_matrices(self) -> None:
        with self.assertRaises(CiContractError):
            normalize_github_workflow(
                b"name: PR\njobs:\n  build: &job\n    name: Build\n  copy: *job\n",
                ".github/workflows/ci.yml",
            )
        with self.assertRaises(CiContractError):
            normalize_github_workflow(
                b"name: PR\njobs:\n  build:\n    name: Build\n    name: Duplicate\n",
                ".github/workflows/ci.yml",
            )
        with self.assertRaises(CiContractError):
            normalize_github_workflow(
                b"name: PR\njobs:\n  build:\n    strategy:\n      matrix: {os: [linux, macos]}\n",
                ".github/workflows/ci.yml",
            )

    def test_workflow_is_credential_free_and_source_only(self) -> None:
        source = normalize_github_workflow(
            (ROOT / ".github/workflows/ci.yml").read_bytes(),
            ".github/workflows/ci.yml",
        )
        job = source["jobs"][0]
        self.assertEqual(job["effective_permissions"], {"contents": "read"})
        self.assertEqual(job["runs_on"], "ubuntu-latest")
        self.assertTrue(job["runner_environment_asserted"])
        self.assertNotIn("RUNNER_ENVIRONMENT", job["effective_environment"])
        checkout = [step for step in job["steps"] if step["uses"].startswith("actions/checkout@")]
        self.assertEqual(len(checkout), 1)
        self.assertRegex(checkout[0]["uses"], r"^actions/checkout@[0-9a-f]{40}$")
        self.assertFalse(job["checkout_persist_credentials"])
        for step in job["steps"]:
            self.assertNotIn("RUNNER_ENVIRONMENT", step["effective_environment"])
            self.assertFalse(step["secret_refs"])
        self.assertEqual(
            [step["run"] for step in job["steps"] if step["uses"] == ""],
            ['test "$RUNNER_ENVIRONMENT" = github-hosted', "./scripts/validate"],
        )

    def test_source_only_validation_does_not_create_installed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            home = temporary_root / "home"
            persistence = Path(temporary) / "persistence"
            home.mkdir()
            copied_root = temporary_root / "kit"
            shutil.copytree(
                ROOT,
                copied_root,
                ignore=source_validation_copy_ignore,
            )
            copied_tests_root = copied_root / "tests"
            temporary_test_files = sorted(copied_tests_root.rglob("test_*.py"))
            for test_file in temporary_test_files:
                self.assertFalse(test_file.is_symlink(), test_file)
                self.assertTrue(test_file.is_file(), test_file)
            for test_file in temporary_test_files:
                test_file.unlink()
            probe = copied_tests_root / "test_source_validation_probe.py"
            probe.write_text(
                "import unittest\n\n"
                "class SourceValidationProbe(unittest.TestCase):\n"
                "    def test_probe(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            self.assertEqual(
                sorted(copied_tests_root.rglob("test_*.py")),
                [probe],
            )
            result = subprocess.run(
                [str(copied_root / "scripts" / "validate")],
                env={
                    **os.environ,
                    "HOME": str(home),
                    "KENT_PERSISTENCE_ROOT": str(persistence),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=300,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((persistence / "config.toml").exists())
            self.assertFalse((home / ".kent" / "config.toml").exists())

    def test_validate_rejects_unknown_arguments(self) -> None:
        result = subprocess.run(
            [str(ROOT / "scripts" / "validate"), "--unexpected"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
