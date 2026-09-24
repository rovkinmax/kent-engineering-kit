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
import tomllib
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

from tests.test_revision import (
    CONTEXT_MANIFESTS,
    RevisionPreflightTest,
    WORK_KIND_PROCEDURES,
    release_spec_contents,
    schema4_profile_contents,
    source_manifest_contents,
)


ROOT = Path(__file__).resolve().parents[1]


def source_validation_copy_ignore(directory, names):
    ignored = shutil.ignore_patterns(".git", "__pycache__")(directory, names)
    if Path(directory) == ROOT:
        ignored.add("build")
    elif Path(directory) == ROOT / ".kent":
        ignored.add("runtime")
    return ignored


class CiContractTest(unittest.TestCase):
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
                "workspace_path": str(ROOT),
                "task_short_id": "TASK-17",
                "delivery_context": json.dumps(context),
            }),
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

    def test_required_job_projection_accepts_schema3_native_agent_source(self) -> None:
        fixture = RevisionPreflightTest("test_schema3_native_agent_preflight_binds_snapshot_without_script")
        try:
            root = fixture.create_project(
                schema4=True,
                release_schema_version=3,
                both_templates=True,
                native_agent=True,
            )
            profile = ci_contract._profile_at_revision(root, "HEAD")
            rows = ci_contract._required_rows(
                root,
                profile,
                fixture.run_git(root, "rev-parse", "HEAD").stdout.strip(),
                repository="owner/repository",
            )
            self.assertEqual(
                [row["contract_key"] for row in rows],
                ["required_release_contract"],
            )
        finally:
            fixture.doCleanups()

    def test_required_job_projection_accepts_schema3_native_only_source(self) -> None:
        fixture = RevisionPreflightTest("test_schema3_native_only_preflight_has_zero_effect_jobs")
        try:
            root = fixture.create_project(
                schema4=True,
                release_schema_version=3,
                native_agent=True,
                native_only=True,
            )
            profile = ci_contract._profile_at_revision(root, "HEAD")
            rows = ci_contract._required_rows(
                root,
                profile,
                fixture.run_git(root, "rev-parse", "HEAD").stdout.strip(),
                repository="owner/repository",
            )
            self.assertEqual(
                [row["contract_key"] for row in rows],
                ["required_release_contract"],
            )
        finally:
            fixture.doCleanups()

    def test_runner_assertion_matches_supported_puber_bytes_only(self) -> None:
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
        payload = {
            "workspace_path": str(ROOT),
            "pr_url": "https://github.com/owner/repository/pull/1",
            "branch_name": "TASK-1", "merge_strategy": "rebase",
            "task_short_id": "TASK-1",
        }
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts/prepare-github-ci")],
                input=json.dumps(payload), text=True, capture_output=True, timeout=10,
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
        with (
            mock.patch.object(ci_contract, "read_pull_request", side_effect=[pr, pr]),
            mock.patch.object(
                ci_contract,
                "materialize_exact_commit",
                side_effect=AssertionError("dynamic prepare must not materialize commits"),
            ),
            mock.patch.object(
                ci_contract,
                "preflight_project_revision",
                side_effect=AssertionError("dynamic prepare must not preflight source"),
            ),
            mock.patch.object(
                ci_contract,
                "_profile_at_revision",
                side_effect=AssertionError("dynamic prepare must not read profile"),
            ),
            mock.patch.object(
                ci_contract,
                "_required_rows",
                side_effect=AssertionError("dynamic prepare must not read release jobs"),
            ),
            mock.patch.object(
                ci_contract,
                "_validate_job_sources",
                side_effect=AssertionError("dynamic prepare must not validate source jobs"),
            ),
            mock.patch.object(
                ci_contract,
                "_validate_adoption",
                side_effect=AssertionError("dynamic prepare must not inspect adoption"),
            ),
        ):
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
            "ci_contract": json.dumps(contract, separators=(",", ":")),
            "ci_report": json.dumps(report, separators=(",", ":")),
        }
        with (
            mock.patch.object(ci_contract, "read_pull_request", side_effect=[pr, pr]),
            mock.patch.object(ci_contract, "archive_ci_report") as archive,
        ):
            retried = prepare_ci_payload(retry_payload, gh_bin="/bin/true")
        self.assertEqual(retried["transition"], "ci_prepare_ready_retry")
        self.assertEqual(retried["ci_report"], retry_payload["ci_report"])
        archive.assert_called_once()

        changed_head = "c" * 40
        changed_pr = {**pr, "headRefOid": changed_head}
        changed = {
            **retry_payload,
            "ci_report": retry_payload["ci_report"],
        }
        with (
            mock.patch.object(
                ci_contract,
                "read_pull_request",
                side_effect=[changed_pr, changed_pr],
            ),
            mock.patch.object(ci_contract, "archive_ci_report") as archive_changed,
        ):
            fresh = prepare_ci_payload(changed, gh_bin="/bin/true")
        self.assertEqual(fresh["transition"], "ci_prepare_ready_initial")
        self.assertNotIn("ci_report", fresh)
        self.assertEqual(json.loads(fresh["ci_contract"])["head_oid"], changed_head)
        archive_changed.assert_called_once()

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

    def test_exact_missing_object_fetch_changes_only_local_object_cache(self) -> None:
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin = root / "origin"
            cache = root / "cache"
            origin.mkdir()

            def git(cwd, *args):
                return subprocess.run(
                    [real_git, "-C", str(cwd), *args], check=True,
                    text=True, capture_output=True,
                ).stdout.strip()

            git(origin, "init", "-q")
            git(origin, "config", "user.name", "Kent Test")
            git(origin, "config", "user.email", "kent@example.invalid")
            (origin / "source").write_text("target\n")
            git(origin, "add", "source")
            git(origin, "commit", "-qm", "target")
            git(root, "clone", "--no-local", str(origin), str(cache))
            (origin / "source").write_text("head\n")
            git(origin, "commit", "-qam", "head")
            head = git(origin, "rev-parse", "HEAD")
            refs = git(cache, "for-each-ref", "--format=%(refname) %(objectname)")
            tracked = (cache / "source").read_bytes()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            calls = root / "calls.jsonl"
            credentials_called = root / "credential-helper-called"
            fake_gh = fake_bin / "gh-auth-fixture"
            fake_gh.write_text(
                f"#!{sys.executable}\n"
                "import pathlib, sys\n"
                "assert sys.argv[1:] == ['auth', 'git-credential', 'get']\n"
                "request = sys.stdin.read()\n"
                "assert 'protocol=https\\n' in request and 'host=github.com\\n' in request\n"
                "assert 'path=owner/repository.git\\n' in request\n"
                f"pathlib.Path({str(credentials_called)!r}).touch()\n"
                "print('username=fixture\\npassword=synthetic-test-credential\\n')\n"
            )
            fake_gh.chmod(0o755)
            forbidden_helper = root / "forbidden-helper-called"
            git(
                cache, "config", "credential.https://github.com/owner/repository.git.helper",
                f"!touch {forbidden_helper}; exit 42",
            )
            fake_git = fake_bin / "git"
            fake_git.write_text(
                f"#!{sys.executable}\n"
                "import json, os, subprocess, sys\n"
                "args = sys.argv[1:]\n"
                f"with open({str(calls)!r}, 'a') as log: log.write(json.dumps(args) + '\\n')\n"
                "if 'fetch' in args:\n"
                "    assert args[-2] == 'https://github.com/owner/repository.git'\n"
                # Exercise real Git credential selection before accepting the
                # fake private transport. Neither a global rewrite nor an
                # unrelated local credential helper may supply this identity.
                f"    credential = subprocess.run([{real_git!r}, *args[:args.index('fetch')], 'credential', 'fill'],\n"
                "        input='protocol=https\\nhost=github.com\\npath=owner/repository.git\\n\\n',\n"
                "        text=True, capture_output=True, check=True)\n"
                "    assert 'password=synthetic-test-credential\\n' in credential.stdout\n"
                # Only the test transport redirects the exact verified HTTPS
                # authority to a private Git repository. Production does not.
                f"    args[-2] = {str(origin)!r}\n"
                "    args[0:0] = ['-c', 'protocol.file.allow=always']\n"
                f"os.execv({real_git!r}, [{real_git!r}, *args])\n"
            )
            fake_git.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{fake_bin}:{os.environ['PATH']}"}):
                ci_contract.materialize_exact_commit(cache, "owner/repository", head, gh_bin=str(fake_gh))
                ci_contract.materialize_exact_commit(cache, "owner/repository", head, gh_bin=str(fake_gh))
            self.assertTrue(credentials_called.exists())
            self.assertFalse(forbidden_helper.exists())
            self.assertEqual(git(cache, "cat-file", "-t", head), "commit")
            self.assertEqual(git(cache, "for-each-ref", "--format=%(refname) %(objectname)"), refs)
            self.assertEqual((cache / "source").read_bytes(), tracked)
            self.assertFalse((cache / ".git/FETCH_HEAD").exists())
            commands = [json.loads(line) for line in calls.read_text().splitlines()]
            fetches = [args for args in commands if "fetch" in args]
            self.assertEqual(len(fetches), 1)
            self.assertEqual(fetches[0][-1], head)
            self.assertIn("--no-write-fetch-head", fetches[0])
            self.assertIn("--no-tags", fetches[0])
            self.assertIn("--no-recurse-submodules", fetches[0])
            self.assertIn("--no-auto-maintenance", fetches[0])
            with self.assertRaises(CiContractError):
                ci_contract.materialize_exact_commit(cache, "owner/repository", "HEAD")

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

    def test_required_policy_uses_complete_observed_dtos_unconditionally(self) -> None:
        from copy import deepcopy
        row = tomllib.loads(release_spec_contents())["required_jobs_v1"]["jobs"][0]
        script = 'test "$RUNNER_ENVIRONMENT" = github-hosted'
        row["steps"][0]["run"] = script
        document = {
            "name": "Release", "on": "pull_request",
            "permissions": {"contents": "read"},
            "jobs": {"required_release": {
                "name": "Required Release", "runs-on": "ubuntu-latest",
                "steps": [{"name": "validate", "shell": "bash", "run": script}],
            }},
        }
        path = row["workflow_path"] = ".github/workflows/ci.json"
        mutations = [
            lambda job: job.update({"if": False}),
            lambda job: job.update({"environment": {"name": "production"}}),
            lambda job: job.update({"container": "image@sha256:" + "a" * 64}),
            lambda job: job.update({"permissions": {"contents": "write"}}),
            lambda job: job.update({"continue-on-error": "${{ inputs.ignore }}"}),
            lambda job: job["steps"][0].update({"run": 'echo "$RUNNER_ENVIRONMENT"'}),
            lambda job: job["steps"][0].update({"continue-on-error": "${{ true }}"}),
            lambda job: job["steps"][0].pop("shell"),
            lambda job: job.update({"env": {"SECRET": "${{ secrets.TOKEN }}"}}),
        ]
        for mutation in [None, *mutations]:
            head = deepcopy(document)
            if mutation:
                mutation(head["jobs"]["required_release"])
            with mock.patch.object(
                ci_contract, "read_blob_bytes",
                side_effect=lambda root, revision, path, **kw: json.dumps(
                    document if revision == "p" else head
                ).encode(),
            ), mock.patch.object(
                ci_contract, "validate_required_job_sources",
                wraps=ci_contract.validate_required_job_sources,
            ) as validator, self.subTest(mutation=mutation):
                if mutation:
                    with self.assertRaises(CiContractError):
                        ci_contract._validate_job_sources(Path("/fixture"), "p", "h", [row])
                else:
                    result = ci_contract._validate_job_sources(
                        Path("/fixture"), "p", "h", [row],
                    )
                    self.assertEqual(result, [{
                        "workflow_name": "Release", "check_name": "Required Release",
                        "allow_skipped": False,
                    }])
                    self.assertEqual(validator.call_count, 2)
                    self.assertTrue(all(
                        isinstance(call.args[0][0], NormalizedGitHubWorkflowSourceV1)
                        for call in validator.call_args_list
                    ))
                    self.assertEqual(
                        validator.call_args_list[0].args[0][0].workflow_path, path,
                    )

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
            profile.write_text(schema4_profile_contents())
            (root / ".kent" / "project-contract.md").write_text(
                "# Project contract\n"
            )
            for relative in WORK_KIND_PROCEDURES + CONTEXT_MANIFESTS:
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
            (root / ".kent" / "release").mkdir(parents=True)
            (root / ".kent" / "release" / "spec.toml").write_text(
                release_spec_contents().replace(
                    '"run" = "echo ok"',
                    '"run" = "test \\"$RUNNER_ENVIRONMENT\\" '
                    '= github-hosted"',
                )
            )
            (root / ".kent" / "release" / "source-manifest.json").write_text(
                source_manifest_contents()
            )
            (root / ".kent" / "release" / "snapshot.json").write_text(
                "{}\n"
            )
            builder = root / ".kent" / "release" / "build.sh"
            builder.write_text("#!/usr/bin/env bash\nexit 0\n")
            builder.chmod(0o755)
            workflow = root / ".github" / "workflows" / "release.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """name: Release
on:
  pull_request:
permissions:
  contents: read
jobs:
  required_release:
    name: Required Release
    runs-on: ubuntu-latest
    steps:
      - name: validate
        shell: bash
        run: test "$RUNNER_ENVIRONMENT" = github-hosted
"""
            )

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

            head_profile = schema4_profile_contents().replace(
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
            (root / ".kent/release/source-manifest.json").write_text(
                source_manifest_contents(external_roots=[{
                    "kind": "builder-sha256",
                    "key": ci_contract._sha256(builder.read_bytes()),
                    "runtime_digest_required": True,
                }, {
                    "kind": "source-sha256",
                    "key": ".github/workflows/release.yml=" + ci_contract._sha256(workflow.read_bytes()),
                    "runtime_digest_required": True,
                }])
            )
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
            self.assertEqual(direct["pr_head_oid"], head)
            self.assertEqual(direct["pr_base_oid"], target)
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
            graph = build_delivery_workflow(ProjectProfile.load(root), 1)
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
                "    print(json.dumps([{'data': {'repository': {'pullRequest': "
                "{'reviewThreads': {'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}}]))\n"
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
            report = runtime.validate_dynamic_ci_report(json.loads(green["ci_report"]))
            self.assertEqual(report["schema"], "github-ci-report-v3")
            self.assertEqual(report["attempts"][-1]["reason"], "failed")
            self.assertEqual(len(report["attempts"][-1]["checks"]), 2)
            return
            for extra_state in ("PENDING", "IN_PROGRESS"):
                checks_path.write_text(json.dumps([
                    observed[0], {**observed[1], "bucket": "pending", "state": extra_state},
                ]))
                pending_extra = invoke(
                    scripts / "workflow-wait-github-ci", transport("ci_prepare_ready_initial", prepared),
                )
                self.assertEqual(pending_extra["transition"], "ci_watch_passed", pending_extra)
                self.assertEqual(json.loads(pending_extra["ci_report"])["attempts"][-1]["watcher_exit_code"], 8)
            checks_path.write_text(json.dumps([
                {**observed[0], "bucket": "pending", "state": "PENDING"}, observed[1],
            ]))
            pending_required = invoke(
                scripts / "workflow-wait-github-ci", transport("ci_prepare_ready_initial", prepared),
            )
            self.assertEqual(pending_required["transition"], "ci_watch_failed")
            self.assertEqual(json.loads(pending_required["ci_report"])["attempts"][-1]["reason"], "pending_limit")
            checks_path.write_text(json.dumps(observed))
            artifact = root / ".kent/runtime/TASK-1" / f"ci-report-{runtime.canonical_sha256(report)}.json"
            self.assertEqual(json.loads(artifact.read_text()), report)
            ledger = root / ".kent/runtime/TASK-1/evidence-ledger.jsonl"
            self.assertIn(str(artifact.relative_to(root)), ledger.read_text())
            waiting_edge = next(edge.key for edge in graph.edges if edge.source == "ci_watch" and edge.target == "waiting_pr")
            wait_values = transport(waiting_edge, green)
            merge_edge = next(edge.key for edge in graph.edges if edge.source == "waiting_pr" and edge.target == "merge_watch")
            # Waiting PR owns merge-method revalidation and supplies the exact
            # observed head/base, as required by its existing node contract.
            wait_input = transport(merge_edge, {
                **wait_values, "pr_head_oid": pr_state["headRefOid"],
                "pr_base_oid": pr_state["baseRefOid"],
            })
            feedback = invoke(scripts / "workflow-wait-github-pr", wait_input)
            self.assertEqual(feedback["transition"], "merge_watch_still_waiting", feedback)
            comments_path.write_text(json.dumps([{
                "id": 1, "body": "Please review this note",
                "created_at": "2026-09-06T12:00:00Z", "updated_at": "2026-09-06T12:00:00Z",
                "user": {"login": "reviewer"},
            }]))
            changed_feedback = invoke(
                scripts / "workflow-wait-github-pr",
                {**wait_input, "pr_feedback_cursor": feedback["pr_feedback_cursor"]},
            )
            self.assertEqual(changed_feedback["transition"], "merge_watch_state_changed", changed_feedback)
            self.assertEqual(json.loads(changed_feedback["pr_report"])["reason"], "pull_request_feedback_changed")
            stable = invoke(
                scripts / "workflow-wait-github-pr",
                {**wait_input, "pr_feedback_cursor": changed_feedback["pr_feedback_cursor"]},
            )
            self.assertEqual(stable["transition"], "merge_watch_still_waiting", stable)
            self.assertEqual(stable["ci_report"], green["ci_report"])
            self.assertFalse(list(root.rglob("__pycache__")))
            environment["PYTHONDONTWRITEBYTECODE"] = "0"
            for required in ([], [{**observed[0], "bucket": "skipping", "state": "SKIPPED"}],
                             [{**observed[0], "bucket": "fail", "state": "FAILURE"}]):
                checks_path.write_text(json.dumps([*required, observed[1]]))
                blocked = invoke(scripts / "workflow-wait-github-pr", {**wait_input, **stable})
                self.assertEqual(blocked["transition"], "merge_watch_state_changed")
                self.assertIn(json.loads(blocked["pr_report"])["reason"], {"checks_failed", "required_checks_not_green"})
            checks_path.write_text(json.dumps(observed))
            retry = invoke(
                scripts / "workflow-prepare-github-ci",
                transport("waiting_pr_ci_monitor", stable),
            )
            self.assertEqual(retry["transition"], "ci_prepare_ready_retry", retry)
            again = invoke(
                scripts / "workflow-wait-github-ci", transport("ci_prepare_ready_retry", retry),
            )
            self.assertEqual(again["transition"], "ci_watch_passed", again)
            retried_report = runtime.validate_ci_report(json.loads(again["ci_report"]))
            self.assertEqual(retried_report["attempts"][:-1], report["attempts"])
            # A P-only advance with identical effective policy keeps every attempt.
            advanced_target = git("commit-tree", f"{target}^{{tree}}", "-p", target, "-m", "target advance")
            state_path.write_text(json.dumps({**pr_state, "baseRefOid": advanced_target}))
            advanced = invoke(
                scripts / "workflow-prepare-github-ci", transport("ci_monitor_watch", again),
            )
            self.assertEqual(advanced["transition"], "ci_prepare_ready_retry", advanced)
            self.assertEqual(advanced["ci_report"], again["ci_report"])
            self.assertEqual(json.loads(advanced["ci_policy_snapshot"])["target_commit"], advanced_target)
            stale = invoke(
                scripts / "workflow-wait-github-ci", transport("ci_prepare_ready_retry", retry),
            )
            self.assertEqual(stale["transition"], "ci_watch_source_changed", stale)
            self.assertEqual(stale["ci_report"], retry["ci_report"])
            refreshed = invoke(
                scripts / "workflow-prepare-github-ci", transport("ci_watch_source_changed", stale),
            )
            self.assertEqual(refreshed["transition"], "ci_prepare_ready_retry", refreshed)
            # Policy-only P drift (same names, same required source, same H)
            # must invalidate the cycle even though the expected digest matches.
            spec_path = root / ".kent/release/spec.toml"
            original_spec = spec_path.read_text()
            spec_path.write_text(original_spec.replace(
                "github-hosted-standard-ephemeral", "github-hosted-larger-ephemeral",
            ))
            git("add", ".kent/release/spec.toml")
            policy_target = git("commit-tree", git("write-tree"), "-p", target, "-m", "required policy drift")
            spec_path.write_text(original_spec)
            git("add", ".kent/release/spec.toml")
            state_path.write_text(json.dumps({**pr_state, "baseRefOid": policy_target}))
            changed_policy = invoke(
                scripts / "workflow-prepare-github-ci", transport("ci_monitor_watch", again),
            )
            self.assertEqual(changed_policy["transition"], "ci_prepare_ready_initial", changed_policy)
            self.assertEqual(changed_policy["expected_ci_checks_sha256"], again["expected_ci_checks_sha256"])
            self.assertNotEqual(
                json.loads(changed_policy["ci_policy_snapshot"])["source_policy_sha256"],
                json.loads(again["ci_policy_snapshot"])["source_policy_sha256"],
            )
            self.assertNotIn("ci_report", changed_policy)
            self.assertTrue(artifact.exists())
            # Retargeting during the bounded producer read cannot validate a
            # mixed packet; a newly merged PR instead takes proven recovery.
            with mock.patch.object(
                ci_contract, "read_pull_request",
                side_effect=[pr_state, {**pr_state, "baseRefName": "release"}],
            ):
                with self.assertRaisesRegex(CiContractError, "identity changed"):
                    prepare_ci_payload(payload)
            merged_state = {
                **pr_state, "state": "MERGED", "mergedAt": "2026-09-06T12:00:00Z",
                "mergeCommit": {"oid": head},
            }
            with mock.patch.object(
                ci_contract, "read_pull_request", side_effect=[pr_state, merged_state],
            ):
                merged = prepare_ci_payload(payload)
                self.assertEqual(merged["transition"], "ci_prepare_pr_merged")
                self.assertEqual(json.loads(merged["merge_report"])["mergeCommit"]["oid"], head)
            # A failing mandatory job reaches actual diagnosis, with safe
            # retry transport retaining all prior observations.
            state_path.write_text(json.dumps(pr_state))
            checks_path.write_text(json.dumps([
                {**observed[0], "bucket": "fail", "state": "FAILURE"}, observed[1],
            ]))
            failed = invoke(
                scripts / "workflow-wait-github-ci", transport("ci_prepare_ready_retry", retry),
            )
            self.assertEqual(failed["transition"], "ci_watch_failed")
            diagnosis = transport("ci_watch_diagnose", failed)
            retry_after_failure = invoke(
                scripts / "workflow-prepare-github-ci", transport("ci_monitor_watch", diagnosis),
            )
            self.assertEqual(retry_after_failure["transition"], "ci_prepare_ready_retry")
            self.assertEqual(retry_after_failure["ci_report"], failed["ci_report"])
            checks_path.write_text(json.dumps(observed))
            # A new H starts a fresh report while preserving the old evidence.
            new_head = git("commit-tree", f"{head}^{{tree}}", "-p", head, "-m", "new head")
            state_path.write_text(json.dumps({**pr_state, "headRefOid": new_head}))
            fresh = invoke(scripts / "workflow-prepare-github-ci", transport("ci_monitor_watch", again))
            self.assertEqual(fresh["transition"], "ci_prepare_ready_initial", fresh)
            self.assertNotIn("ci_report", fresh)
            self.assertTrue(artifact.exists())
            self.assertTrue(list(artifact.parent.glob("ci-report-*.json")))
            self.assertFalse(list(root.rglob("__pycache__")))
            self.assertEqual(
                git("status", "--porcelain"),
                "",
                "read-only preparation/consumers changed tracked or unignored project source",
            )
            # Resume the original fixture's partial-adoption/drift checks.
            fake_gh.write_text(
                "#!/bin/sh\n"
                f"echo '{json.dumps(pr_state)}'\n"
            )
            # Names and declared versions alone cannot claim template adoption.
            adopted = scripts / "workflow-wait-github-ci"
            adopted.write_text("#!/bin/sh\nexit 0\n")
            git("add", ".kent/scripts/workflow-wait-github-ci")
            git("commit", "-qm", "partial adoption")
            partial_head = git("rev-parse", "HEAD")
            fake_gh.write_text(fake_gh.read_text().replace(head, partial_head))
            with self.assertRaisesRegex(CiContractError, "source_adoption_incomplete"):
                prepare_ci_payload(payload, gh_bin=str(fake_gh))
            adopted.write_bytes((ROOT / "templates/project/workflow-wait-github-ci").read_bytes())
            fake_gh.write_text(fake_gh.read_text().replace(partial_head, head))
            workflow.write_text(
                workflow.read_text().replace(
                    'run: test "$RUNNER_ENVIRONMENT" = github-hosted',
                    "run: echo changed",
                )
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "head source drift"],
                cwd=root,
                check=True,
            )
            drifted_head = git("rev-parse", "HEAD")
            fake_gh.write_text(
                fake_gh.read_text().replace(head, drifted_head)
            )
            with self.assertRaisesRegex(CiContractError, "required_job_source_invalid"):
                prepare_ci_payload(payload, gh_bin=str(fake_gh))

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

    def test_policy_snapshot_binds_target_and_mandatory_projection(self) -> None:
        expected = {
            "schema": "github-ci-expected-checks-v1",
            "repository": "owner/repository",
            "project_commit": "a" * 40,
            "runtime_source_envelope_digest": "b" * 64,
            "checks": [
                {
                    "workflow_name": "PR",
                    "check_name": "Build",
                    "allow_skipped": False,
                }
            ],
        }
        snapshot = runtime.make_ci_policy_snapshot("c" * 40, expected)
        self.assertEqual(
            runtime.validate_ci_policy_snapshot(snapshot),
            snapshot,
        )
        self.assertEqual(
            runtime.ci_policy_projection_sha256(expected),
            snapshot["policy_sha256"],
        )

    def test_workflow_is_credential_free_and_source_only(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertEqual(
            workflow,
            """name: validate

on:
  pull_request:
  push:
    branches:
      - main

permissions:
  contents: read

jobs:
  validate:
    runs-on: ubuntu-latest
    env:
      RUNNER_ENVIRONMENT: github-hosted
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
        with:
          persist-credentials: false
      - name: Assert hosted runner
        run: test "$RUNNER_ENVIRONMENT" = github-hosted
      - name: Validate source tree
        run: ./scripts/validate
""",
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

    def test_validate_has_an_explicit_installed_state_mode(self) -> None:
        script = (ROOT / "scripts" / "validate").read_text()
        self.assertIn("--installed-state", script)
        self.assertIn("usage: scripts/validate [--installed-state]", script)
        self.assertIn('if [[ "$installed_state" -eq 1 ]]; then', script)

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
