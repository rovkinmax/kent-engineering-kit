from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from tests.test_workflow_runtime import (
    EVIDENCE,
    EVIDENCE_FIXTURE_OWNER,
    GitRepositoryTest,
    load_template_module,
)


class EvidenceAuditS5Test(GitRepositoryTest):
    task = "TASK-4"

    def fixture(self):
        root = self.create_repository().resolve()
        context = root / ".kent/context"
        context.mkdir(parents=True)
        (context / "implement.md").write_text("manifest\n")
        self.run_git(root, "add", ".kent/context/implement.md")
        self.run_git(root, "commit", "-q", "-m", "Context fixture")
        scripts = root / ".kent/scripts"
        scripts.mkdir(parents=True)
        shutil.copyfile(EVIDENCE, scripts / "workflow-evidence-ledger")
        shutil.copyfile(
            Path(__file__).resolve().parents[1] / "workflowkit/runtime.py",
            scripts / "workflow_runtime_contracts.py",
        )
        ledger = load_template_module(
            scripts / "workflow-evidence-ledger", "evidence_audit_s5_ledger",
        )
        directory = root / ".kent/runtime" / self.task
        directory.mkdir(parents=True, mode=0o700)
        return root, scripts, directory, ledger

    def environment(self):
        return {
            **os.environ,
            **EVIDENCE_FIXTURE_OWNER,
            "KENT_RUN_ID": "fixture-run",
        }

    def payload(self, *, repeated_questions=0, verification_loops=0):
        return {
            "node_key": "implement",
            "evidence_type": "implementation",
            "summary": "audited event",
            "artifacts": [],
            "checks": ["focused"],
            "decisions": [],
            "context": {
                "manifest_path": ".kent/context/implement.md",
                "files_read": [],
                "repeated_questions": repeated_questions,
                "verification_loops": verification_loops,
            },
        }

    def append_seed(self, root, ledger, payload=None):
        with mock.patch.dict(os.environ, self.environment(), clear=True):
            return ledger.append_entry(
                root,
                root / ".kent/runtime" / self.task / "evidence-ledger.jsonl",
                task=self.task,
                _payload=payload or self.payload(),
            )

    def ordinary_record(
        self, runtime, *, root, sequence, previous_hash, summary="x", task=None,
    ):
        task = self.task if task is None else task
        record = {
            "schema_version": 2,
            "sequence": sequence,
            "task_short_id": task,
            "node_key": "implement",
            "evidence_type": "implementation",
            "summary": summary,
            "artifacts": [],
            "checks": [],
            "decisions": [],
            "context": {
                "manifest_path": ".kent/context/implement.md",
                "files_read": [],
                "instruction_bytes": 9,
                "repeated_file_count": 0,
                "repeated_files": [],
                "model_calls": None,
                "compaction_count": None,
                "repeated_questions": None,
                "verification_loops": None,
            },
            "workspace_path": str(root),
            "git_head": "a" * 40,
            "session_id": EVIDENCE_FIXTURE_OWNER["KENT_SESSION_ID"],
            "step_id": EVIDENCE_FIXTURE_OWNER["KENT_STEP_ID"],
            "created_at": "2026-10-22T00:00:00+00:00",
            "previous_hash": previous_hash,
        }
        record["event_hash"] = runtime._event_hash(record)
        return record

    def write_chain(self, directory, runtime, root, count, *, summary="x", task=None):
        previous = ""
        lines = []
        for sequence in range(1, count + 1):
            record = self.ordinary_record(
                runtime,
                root=root,
                sequence=sequence,
                previous_hash=previous,
                summary=summary,
                task=task,
            )
            previous = record["event_hash"]
            lines.append(runtime.canonical_bytes(record) + b"\n")
        raw = b"".join(lines)
        path = directory / "evidence-ledger.jsonl"
        path.write_bytes(raw)
        path.chmod(0o600)
        return raw

    def seal_request(self):
        return {
            "schema": "terminal-evidence-seal-request-v1",
            "operation_report_digests": [],
            "redaction": {"status": "passed", "report_sha256": "a" * 64},
            "retention_class": "cleanup_report_only",
        }

    def script_archive_context(self):
        return {
            "task_id": "native-task",
            "node_id": "ci-watch",
            "transition_branch_key": None,
        }

    def script_archive_proof(self, root, context=None, digest="b" * 64):
        context = self.script_archive_context() if context is None else context
        return {
            "schema": "script-ci-archive-reader-proof-v1",
            "reader_proof_sha256": digest,
            "native_script_identity": {
                "task_id": context["task_id"],
                "task_short_id": self.task,
                "project_id": "native-project",
                "workspace_path": str(root),
                "invocation_kind": "script",
                "node_id": context["node_id"],
                "transition_branch_key": context.get("transition_branch_key"),
            },
        }

    def script_archive_payload(self, root):
        from tests.test_runtime_contracts import ci_report

        return {
            "schema": "script-ci-archive-request-v1",
            "workspace_path": str(root),
            "task_short_id": self.task,
            "ci_report": ci_report(),
            "native_context": self.script_archive_context(),
        }

    def test_dedup_retry_reuses_projection_without_rereading_removed_context(self):
        root, _, directory, ledger = self.fixture()
        payload = self.payload()
        first, reused = self.append_seed(root, ledger, payload)
        self.assertFalse(reused)
        (root / ".kent/context/implement.md").unlink()
        with mock.patch.dict(os.environ, self.environment(), clear=True):
            retry = ledger.append_entry(
                root,
                directory / "evidence-ledger.jsonl",
                task=self.task,
                _payload=payload,
            )
        self.assertEqual(retry, (first, True))
        self.assertEqual(ledger.read_entries(directory / "evidence-ledger.jsonl", task=self.task), [first])

    def test_duplicate_retry_completes_fsync_after_previous_fsync_failure(self):
        root, _, directory, ledger = self.fixture()
        self.append_seed(root, ledger)
        real_fsync = os.fsync
        second_owner = {
            **self.environment(),
            "KENT_STEP_ID": "33333333-3333-4333-8333-333333333333",
        }
        payload = dict(self.payload(), summary="second event")
        with mock.patch.dict(os.environ, second_owner, clear=True):
            with mock.patch.object(ledger.os, "fsync", side_effect=OSError("injected fsync failure")):
                with self.assertRaisesRegex(OSError, "injected"):
                    ledger.append_entry(
                        root, directory / "evidence-ledger.jsonl",
                        task=self.task, _payload=payload,
                    )
            with mock.patch.object(ledger.os, "fsync", wraps=real_fsync) as sync:
                entry, duplicate = ledger.append_entry(
                    root, directory / "evidence-ledger.jsonl",
                    task=self.task, _payload=payload,
                )
        self.assertTrue(duplicate)
        self.assertEqual(entry["sequence"], 2)
        sync.assert_called()
        self.assertEqual(len(ledger.read_entries(directory / "evidence-ledger.jsonl", task=self.task)), 2)

    def test_nullable_summary_metrics_remain_unknown(self):
        root, _, _, ledger = self.fixture()
        entry, _ = self.append_seed(
            root, ledger,
            self.payload(repeated_questions=None, verification_loops=None),
        )
        summary = ledger.summarize([entry])
        self.assertIsNone(summary["repeated_questions"])
        self.assertIsNone(summary["verification_loops"])

    def test_seal_adds_separator_newline_to_valid_final_json(self):
        root, _, directory, ledger = self.fixture()
        self.append_seed(root, ledger)
        path = directory / "evidence-ledger.jsonl"
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
        before = path.read_bytes()
        with mock.patch.object(ledger, "read_json_input", return_value=self.seal_request()):
            result = ledger.seal_terminal(root, task=self.task)
        self.assertTrue(path.read_bytes().startswith(before + b"\n"))
        records = ledger.read_entries(path, task=self.task)
        self.assertEqual(records[-1]["record_kind"], "terminal_evidence_seal_v1")
        import workflowkit.runtime as runtime

        self.assertEqual(
            runtime.parse_terminal_marker_line(result["terminal_marker"])["event_count"],
            2,
        )

    def test_standalone_seal_preflights_exact_record_limit_without_writing(self):
        import workflowkit.runtime as runtime

        root, _, directory, ledger = self.fixture()
        before = self.write_chain(directory, runtime, root, runtime.MAX_EVIDENCE_RECORDS)
        with mock.patch.object(ledger, "read_json_input", return_value=self.seal_request()):
            with self.assertRaisesRegex(ValueError, "record limit"):
                ledger.seal_terminal(root, task=self.task)
        self.assertEqual((directory / "evidence-ledger.jsonl").read_bytes(), before)

    def test_standalone_seal_preflights_exact_byte_limit_without_writing(self):
        import workflowkit.runtime as runtime

        root, _, directory, ledger = self.fixture()
        record = self.ordinary_record(
            runtime, root=root, sequence=1, previous_hash="",
        )
        record["summary"] = "x" * runtime.MAX_EVIDENCE_BYTES
        while True:
            record["event_hash"] = runtime._event_hash(
                {key: value for key, value in record.items() if key != "event_hash"},
            )
            raw = runtime.canonical_bytes(record) + b"\n"
            difference = runtime.MAX_EVIDENCE_BYTES - len(raw)
            if difference == 0:
                break
            record["summary"] = record["summary"][:difference] if difference < 0 else (
                record["summary"] + "x" * difference
            )
        path = directory / "evidence-ledger.jsonl"
        path.write_bytes(raw)
        path.chmod(0o600)
        with mock.patch.object(ledger, "read_json_input", return_value=self.seal_request()):
            with self.assertRaisesRegex(ValueError, "limit"):
                ledger.seal_terminal(root, task=self.task)
        self.assertEqual(path.read_bytes(), raw)

    def test_typed_script_archive_is_chained_and_retry_fsyncs_existing_bytes(self):
        import workflowkit.runtime as runtime

        root, _, directory, ledger = self.fixture()
        self.append_seed(root, ledger)
        native_context = self.script_archive_context()
        proof = self.script_archive_proof(root, native_context)
        payload = self.script_archive_payload(root)
        resolver = mock.Mock(side_effect=[proof, proof])
        initial = resolver(root, self.task, native_context)
        with (
            mock.patch.object(
                ledger, "read_json_input",
                side_effect=AssertionError("payload is injected by the qualified caller"),
            ),
        ):
            result = ledger.archive_script_ci_report(
                root,
                task=self.task,
                _payload=payload,
                _reader_resolver=resolver,
                _initial_reader_proof=initial,
            )
        self.assertEqual(resolver.call_count, 2)
        report_raw = runtime.canonical_bytes(runtime.validate_ci_archive(payload["ci_report"]))
        report_sha = hashlib.sha256(report_raw).hexdigest()
        self.assertEqual(
            result["ci_report_artifact"],
            f".kent/runtime/{self.task}/ci-report-{report_sha}.json",
        )
        records = ledger.read_entries(directory / "evidence-ledger.jsonl", task=self.task)
        typed = records[-1]
        self.assertEqual(set(typed), {
            "schema_version", "record_kind", "sequence", "task_short_id",
            "workspace_path", "script_identity", "report_sha256", "artifacts",
            "previous_hash", "event_hash",
        })
        self.assertEqual(typed["schema_version"], 3)
        self.assertNotIn("session_id", typed)
        self.assertEqual(
            runtime.validate_script_ci_archive_record(
                typed, task_short_id=self.task, workspace_path=str(root),
                sequence=2, previous_hash=records[0]["event_hash"],
            ),
            typed,
        )
        summary = ledger.summarize(records)
        self.assertEqual(summary["entry_count"], 2)
        self.assertEqual(summary["instruction_bytes"], records[0]["context"]["instruction_bytes"])
        self.assertEqual(summary["repeated_questions"], 0)
        self.assertEqual(summary["verification_loops"], 0)
        self.assertEqual(summary["last_event_hash"], records[-1]["event_hash"])
        sync = os.fsync
        retry_resolver = mock.Mock(side_effect=[proof, proof])
        retry_initial = retry_resolver(root, self.task, native_context)
        with (
            mock.patch.object(
                ledger, "read_json_input",
                side_effect=AssertionError("payload is injected by the qualified caller"),
            ),
            mock.patch.object(ledger.os, "fsync", wraps=sync) as fsync_call,
        ):
            repeated = ledger.archive_script_ci_report(
                root,
                task=self.task,
                _payload=payload,
                _reader_resolver=retry_resolver,
                _initial_reader_proof=retry_initial,
            )
        self.assertEqual(repeated, result)
        self.assertEqual(retry_resolver.call_count, 2)
        self.assertGreaterEqual(fsync_call.call_count, 3)
        self.assertEqual(len(ledger.read_entries(directory / "evidence-ledger.jsonl", task=self.task)), 2)
        before_seal = (directory / "evidence-ledger.jsonl").read_bytes()
        with mock.patch.object(ledger, "read_json_input", return_value=self.seal_request()):
            with self.assertRaisesRegex(ValueError, "ordinary event"):
                ledger.seal_terminal(root, task=self.task)
        self.assertEqual((directory / "evidence-ledger.jsonl").read_bytes(), before_seal)

    def test_typed_script_archive_rechecks_reader_bytes_under_lock(self):
        root, _, directory, ledger = self.fixture()
        self.append_seed(root, ledger)
        context = self.script_archive_context()
        initial = self.script_archive_proof(root, context)
        changed = self.script_archive_proof(root, context, digest="c" * 64)
        payload = self.script_archive_payload(root)
        ledger_path = directory / "evidence-ledger.jsonl"
        before = ledger_path.read_bytes()
        resolver = mock.Mock(return_value=changed)
        with self.assertRaisesRegex(ValueError, "proof changed under Task lock"):
            ledger.archive_script_ci_report(
                root,
                task=self.task,
                _payload=payload,
                _reader_resolver=resolver,
                _initial_reader_proof=initial,
            )
        resolver.assert_called_once_with(root, self.task, context)
        self.assertEqual(ledger_path.read_bytes(), before)
        self.assertEqual(list(directory.glob("ci-report-*.json")), [])

    def test_direct_script_archive_without_qualified_provider_fails_before_input(self):
        root, scripts, directory, ledger = self.fixture()
        self.append_seed(root, ledger)
        ledger_path = directory / "evidence-ledger.jsonl"
        before = ledger_path.read_bytes()
        with (
            mock.patch.object(
                ledger, "read_json_input",
                side_effect=AssertionError("unqualified CLI must not read input"),
            ),
            mock.patch.object(
                ledger, "_runtime_module",
                side_effect=AssertionError("unqualified CLI must not import readers"),
            ),
            self.assertRaisesRegex(ValueError, "qualified shared preparation operation"),
        ):
            ledger.archive_script_ci_report(root, task=self.task)
        completed = subprocess.run(
            [
                sys.executable,
                str(scripts / "workflow-evidence-ledger"),
                "archive-script-ci-report",
                "--script-ci-v1",
                "--task",
                self.task,
                "--workspace",
                str(root),
            ],
            cwd=root,
            input="{}\n",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.environment(),
            check=False,
        )
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertIn("qualified shared preparation operation", completed.stderr)
        self.assertEqual(ledger_path.read_bytes(), before)
        self.assertEqual(list(directory.glob("ci-report-*.json")), [])

    def test_retained_native_observation_uses_serving_root_and_keeps_logical_root(self):
        root, _, _, ledger = self.fixture()
        serving_root = root.parent / f"{root.name}-serving"
        serving_root.mkdir()
        workflow = "11111111-1111-4111-8111-111111111111"
        detail = {
            "summary": {
                "short_id": self.task,
                "project_id": "fixture-project",
                "id": "native-task",
                "workflow_id": workflow,
            },
            "source_workspace": {
                "is_primary": True,
                "workspace_id": "fixture-workspace",
                "root_path": str(serving_root),
            },
            "project": {"default_workspace_id": "fixture-workspace"},
            "worktree_path": str(root),
            "current_nodes": [],
            "live_sessions": [],
        }
        graph = {"workflow_id": workflow, "graph": {"nodes": []}}
        native_kent = {
            "task_id": "native-task",
            "node_id": "cleanup",
            "transition_branch_key": "cleanup",
        }
        calls = []

        class FakeProcess:
            def __init__(self, command, *, cwd, **_kwargs):
                calls.append((command[1:], cwd))
                arguments = command[1:]
                if arguments[0] == "project":
                    output = b"fixture-project\n"
                elif arguments[:2] == ["task", "show"]:
                    output = json.dumps(detail).encode()
                elif arguments[:3] == ["workflow", "graph", "inspect"]:
                    output = json.dumps(graph).encode()
                else:
                    raise AssertionError(f"unexpected native command: {arguments}")
                read_fd, write_fd = os.pipe()
                os.write(write_fd, output)
                os.close(write_fd)
                self.stdout = os.fdopen(read_fd, "rb")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.stdout.close()
                return False

            def wait(self, **_kwargs):
                return 0

            def kill(self):
                raise AssertionError("unexpected native readback timeout")

        import workflowkit.runtime as runtime

        with (
            mock.patch.object(ledger.shutil, "which", return_value="/usr/bin/kent"),
            mock.patch.object(ledger.subprocess, "Popen", FakeProcess),
            mock.patch.object(ledger, "_runtime_module", return_value=runtime),
            mock.patch.object(
                runtime,
                "validate_retained_script_reader",
                return_value={"native_script_identity": "validated"},
            ) as validate_reader,
        ):
            identity = ledger._native_context(
                root,
                self.task,
                native_kent=native_kent,
                observation_cwd=serving_root,
            )
        self.assertEqual(identity["workspace"], str(root))
        self.assertEqual(identity["source_workspace"], str(serving_root))
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(cwd == serving_root for _, cwd in calls))
        self.assertEqual(calls[0][0], ["project", str(serving_root)])
        self.assertEqual(calls[1][0][2], self.task)
        validate_reader.assert_called_once_with(
            native_kent,
            detail=detail,
            graph=graph,
            workspace=str(serving_root),
        )

    def test_typed_script_archive_registers_matching_orphan_and_rejects_conflict(self):
        import workflowkit.runtime as runtime

        root, _, directory, ledger = self.fixture()
        self.append_seed(root, ledger)
        context = self.script_archive_context()
        proof = self.script_archive_proof(root, context)
        payload = self.script_archive_payload(root)
        report = runtime.canonical_bytes(runtime.validate_ci_archive(payload["ci_report"]))
        digest = hashlib.sha256(report).hexdigest()
        artifact = directory / f"ci-report-{digest}.json"
        artifact.write_bytes(report)
        artifact.chmod(0o600)
        resolver = mock.Mock(side_effect=[proof, proof])
        initial = resolver(root, self.task, context)
        with (
            mock.patch.object(
                ledger, "read_json_input",
                side_effect=AssertionError("payload is injected by the qualified caller"),
            ),
        ):
            result = ledger.archive_script_ci_report(
                root,
                task=self.task,
                _payload=payload,
                _reader_resolver=resolver,
                _initial_reader_proof=initial,
            )
        self.assertEqual(result["ci_report_artifact"].rsplit("/", 1)[-1], artifact.name)
        self.assertEqual(len(ledger.read_entries(directory / "evidence-ledger.jsonl", task=self.task)), 2)

    def test_typed_script_archive_rejects_conflicting_orphan_bytes(self):
        import workflowkit.runtime as runtime

        root, _, directory, ledger = self.fixture()
        self.append_seed(root, ledger)
        context = self.script_archive_context()
        proof = self.script_archive_proof(root, context)
        payload = self.script_archive_payload(root)
        report = runtime.canonical_bytes(runtime.validate_ci_archive(payload["ci_report"]))
        digest = hashlib.sha256(report).hexdigest()
        artifact = directory / f"ci-report-{digest}.json"
        artifact.write_bytes(b"conflicting orphan")
        artifact.chmod(0o600)
        resolver = mock.Mock(side_effect=[proof, proof])
        initial = resolver(root, self.task, context)
        with (
            mock.patch.object(
                ledger, "read_json_input",
                side_effect=AssertionError("payload is injected by the qualified caller"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "CI archive"):
                ledger.archive_script_ci_report(
                    root,
                    task=self.task,
                    _payload=payload,
                    _reader_resolver=resolver,
                    _initial_reader_proof=initial,
                )
        records = ledger.read_entries(directory / "evidence-ledger.jsonl", task=self.task)
        self.assertEqual(len(records), 1)

    def test_typed_script_archive_supports_distinct_reports_for_same_native_identity(self):
        import workflowkit.runtime as runtime

        root, _, directory, ledger = self.fixture()
        self.append_seed(root, ledger)
        context = self.script_archive_context()
        proof = self.script_archive_proof(root, context)
        payload_a = self.script_archive_payload(root)
        payload_b = self.script_archive_payload(root)
        payload_b["ci_report"]["attempts"] = [
            {
                **payload_b["ci_report"]["attempts"][0],
                "head_oid": "1" * 40,
            },
        ]
        raw_a = runtime.canonical_bytes(runtime.validate_ci_archive(payload_a["ci_report"]))
        raw_b = runtime.canonical_bytes(runtime.validate_ci_archive(payload_b["ci_report"]))
        digest_a = hashlib.sha256(raw_a).hexdigest()
        digest_b = hashlib.sha256(raw_b).hexdigest()
        self.assertNotEqual(digest_a, digest_b)

        def archive(payload):
            resolver = mock.Mock(side_effect=[proof, proof])
            initial = resolver(root, self.task, context)
            result = ledger.archive_script_ci_report(
                root,
                task=self.task,
                _payload=payload,
                _reader_resolver=resolver,
                _initial_reader_proof=initial,
            )
            self.assertEqual(resolver.call_count, 2)
            return result

        expected_a = {"ci_report_artifact": f".kent/runtime/{self.task}/ci-report-{digest_a}.json"}
        expected_b = {"ci_report_artifact": f".kent/runtime/{self.task}/ci-report-{digest_b}.json"}
        self.assertEqual(archive(payload_a), expected_a)
        records_after_a = ledger.read_entries(directory / "evidence-ledger.jsonl", task=self.task)
        self.assertEqual(len(records_after_a), 2)
        self.assertEqual(archive(payload_a), expected_a)
        self.assertEqual(
            len(ledger.read_entries(directory / "evidence-ledger.jsonl", task=self.task)),
            2,
        )

        self.assertEqual(archive(payload_b), expected_b)
        records_after_b = ledger.read_entries(directory / "evidence-ledger.jsonl", task=self.task)
        self.assertEqual(len(records_after_b), 3)
        self.assertEqual(
            [record["report_sha256"] for record in records_after_b[1:]],
            [digest_a, digest_b],
        )
        self.assertEqual(archive(payload_b), expected_b)
        self.assertEqual(
            len(ledger.read_entries(directory / "evidence-ledger.jsonl", task=self.task)),
            3,
        )
        ledger_before_corruption = (directory / "evidence-ledger.jsonl").read_bytes()
        foreign_proof = {
            **proof,
            "native_script_identity": {
                **proof["native_script_identity"],
                "node_id": "foreign-node",
            },
        }
        with self.assertRaisesRegex(ValueError, "differs from current native context"):
            ledger.archive_script_ci_report(
                root,
                task=self.task,
                _payload=payload_b,
                _reader_resolver=mock.Mock(return_value=foreign_proof),
                _initial_reader_proof=foreign_proof,
            )
        self.assertEqual(
            (directory / "evidence-ledger.jsonl").read_bytes(),
            ledger_before_corruption,
        )
        artifact_b = directory / f"ci-report-{digest_b}.json"
        artifact_b.write_bytes(b"tampered registered artifact")
        corrupted = artifact_b.read_bytes()
        with self.assertRaisesRegex(ValueError, "CI archive"):
            archive(payload_b)
        self.assertEqual(
            (directory / "evidence-ledger.jsonl").read_bytes(),
            ledger_before_corruption,
        )
        self.assertEqual(artifact_b.read_bytes(), corrupted)

    def test_cleanup_retry_reuses_stored_projection_before_context_reads(self):
        from tests.test_kit_development_workflow import CleanupPreparationTest

        cleanup = CleanupPreparationTest()
        self.addCleanup(cleanup.doCleanups)
        root = cleanup.fixture()
        request = cleanup.request(root)
        helper = cleanup.helper(root)
        invoke = helper.invoke_ledger

        def fail_seal(root, task, action, payload, environment):
            if action == "seal":
                raise ValueError("injected failure before seal")
            return invoke(root, task, action, payload, environment)

        with mock.patch.dict(
            os.environ,
            {
                **cleanup.environment,
                "KENT_STEP_ID": "55555555-5555-4555-8555-555555555555",
            },
            clear=True,
        ):
            with mock.patch.object(helper, "invoke_ledger", side_effect=fail_seal):
                with self.assertRaisesRegex(ValueError, "injected failure before seal"):
                    helper.prepare(request)

        preparation_path = root / "build/kent-workflow/TASK-1/cleanup-preparation.json"
        preparation_before = json.loads(preparation_path.read_text())
        ledger_path = root / ".kent/runtime/TASK-1/evidence-ledger.jsonl"
        ledger_before = ledger_path.read_bytes()
        self.assertEqual(len(ledger_before.splitlines()), 2)
        (root / ".kent/context/delivery.md").unlink()
        (root / ".kent/commands/cleanup-task.md").unlink()

        with mock.patch.dict(
            os.environ,
            {
                **cleanup.environment,
                "KENT_STEP_ID": "44444444-4444-4444-8444-444444444444",
            },
            clear=True,
        ):
            result = helper.prepare(request)

        self.assertTrue(result["cleanup_report"])
        records = helper.load_command(
            root, "workflow-evidence-ledger",
        ).read_entries(ledger_path, task="TASK-1")
        self.assertEqual(len(records), 3)
        self.assertEqual(records[1]["context"], preparation_before["event"]["context"])
        self.assertEqual(records[1]["summary"], preparation_before["event"]["summary"])
        self.assertEqual(records[0:2], [
            json.loads(line) for line in ledger_before.decode().splitlines()
        ])
        self.assertEqual(json.loads(preparation_path.read_text())["state"], "completed")

    def test_retention_preflight_rejects_retiring_plan_cache_reference(self):
        from tests.test_kit_development_workflow import CleanupPreparationTest

        cleanup = CleanupPreparationTest()
        self.addCleanup(cleanup.doCleanups)
        root = cleanup.fixture()
        request = cleanup.request(root)
        ledger = load_template_module(
            root / ".kent/scripts/workflow-evidence-ledger",
            "cleanup_retiring_cache_reference_ledger",
        )
        directory = root / ".kent/runtime/TASK-1"
        source = directory / "plan-contract.json"
        source_before = source.read_bytes()
        ledger_path = directory / "evidence-ledger.jsonl"
        ledger_before = ledger_path.read_bytes()
        redaction = directory / "cleanup-redaction-report.json"
        redaction_raw = b"validated redaction report fixture"
        redaction.write_bytes(redaction_raw)
        redaction.chmod(0o600)
        request["final_event"]["artifacts"] = [
            ".kent/runtime/TASK-1/plan-contract.json",
            ".kent/runtime/TASK-1/cleanup-redaction-report.json",
        ]
        request["seal_request"]["redaction"]["report_sha256"] = hashlib.sha256(
            redaction_raw,
        ).hexdigest()
        identity = {
            "task_short_id": "TASK-1",
            "workspace": str(root),
            "source_workspace": str(root),
            "source_native_identity": {
                "project_id": "fixture-project",
                "workspace_id": "fixture-workspace",
                "task_id": "native-task",
            },
            "owner": {
                "session_id": cleanup.environment["KENT_SESSION_ID"],
                "step_id": cleanup.environment["KENT_STEP_ID"],
            },
        }
        with mock.patch.dict(os.environ, cleanup.environment, clear=True):
            with mock.patch.object(ledger, "_verified_context", return_value=identity):
                with self.assertRaisesRegex(ValueError, "plan cache scheduled for retirement"):
                    ledger.preflight_terminal_closure(
                        root,
                        task="TASK-1",
                        final_event=request["final_event"],
                        seal_request=request["seal_request"],
                        baseline={
                            "sequence": 1,
                            "event_hash": json.loads(ledger_before)["event_hash"],
                        },
                        retention=True,
                    )
        self.assertEqual(source.read_bytes(), source_before)
        self.assertEqual(ledger_path.read_bytes(), ledger_before)
        self.assertEqual(redaction.read_bytes(), redaction_raw)
        self.assertFalse(
            (root / "build/kent-workflow/TASK-1/cleanup-preparation.json").exists(),
        )

    def test_cleanup_capacity_preflight_keeps_cache_and_ledger_unchanged(self):
        import workflowkit.runtime as runtime
        from tests.test_kit_development_workflow import CleanupPreparationTest

        for case in ("records", "bytes"):
            with self.subTest(case=case):
                cleanup = CleanupPreparationTest()
                self.addCleanup(cleanup.doCleanups)
                root = cleanup.fixture()
                self.task = "TASK-1"
                directory = root / ".kent/runtime/TASK-1"
                if case == "records":
                    before = self.write_chain(
                        directory, runtime, root, runtime.MAX_EVIDENCE_RECORDS,
                        task="TASK-1",
                    )
                else:
                    record = self.ordinary_record(
                        runtime, root=root, sequence=1, previous_hash="",
                        task="TASK-1",
                    )
                    summary = "x"
                    while True:
                        record["summary"] = summary
                        record["event_hash"] = runtime._event_hash({
                            key: value for key, value in record.items()
                            if key != "event_hash"
                        })
                        before = runtime.canonical_bytes(record) + b"\n"
                        difference = runtime.MAX_EVIDENCE_BYTES - len(before)
                        if difference == 0:
                            break
                        summary += "x" * difference
                    path = directory / "evidence-ledger.jsonl"
                    path.write_bytes(before)
                request = cleanup.request(root)
                source = directory / "plan-contract.json"
                source_before = source.read_bytes()
                ledger_path = directory / "evidence-ledger.jsonl"
                ledger_before = ledger_path.read_bytes()
                result = cleanup.prepare(root, request)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("terminal preparation exceeds", result.stderr)
                self.assertTrue(source.is_file())
                self.assertEqual(source.read_bytes(), source_before)
                self.assertEqual(ledger_path.read_bytes(), ledger_before)
                self.assertFalse(
                    (root / "build/kent-workflow/TASK-1/cleanup-preparation.json").exists(),
                )
                self.assertEqual(ledger_before, before)
                cleanup.doCleanups()

    def test_retention_preflight_rejects_missing_final_artifact_before_effects(self):
        from tests.test_kit_development_workflow import CleanupPreparationTest

        cleanup = CleanupPreparationTest()
        self.addCleanup(cleanup.doCleanups)
        root = cleanup.fixture()
        self.task = "TASK-1"
        request = cleanup.request(root)
        scripts = root / ".kent/scripts"
        ledger = load_template_module(
            scripts / "workflow-evidence-ledger", "cleanup_missing_artifact_ledger",
        )
        directory = root / ".kent/runtime/TASK-1"
        source = directory / "plan-contract.json"
        source_before = source.read_bytes()
        ledger_path = directory / "evidence-ledger.jsonl"
        ledger_before = ledger_path.read_bytes()
        identity = {
            "task_short_id": "TASK-1", "workspace": str(root),
            "source_workspace": str(root),
            "source_native_identity": {
                "project_id": "fixture-project",
                "workspace_id": "fixture-workspace",
                "task_id": "native-task",
            },
            "owner": {
                "session_id": cleanup.environment["KENT_SESSION_ID"],
                "step_id": cleanup.environment["KENT_STEP_ID"],
            },
        }
        with mock.patch.dict(os.environ, cleanup.environment, clear=True):
            with mock.patch.object(ledger, "_verified_context", return_value=identity):
                with self.assertRaisesRegex(ValueError, "terminal report digest"):
                    ledger.preflight_terminal_closure(
                        root,
                        task="TASK-1",
                        final_event=request["final_event"],
                        seal_request=request["seal_request"],
                        baseline={
                            "sequence": 1,
                            "event_hash": json.loads(ledger_before)["event_hash"],
                        },
                        retention=True,
                    )
        self.assertEqual(source.read_bytes(), source_before)
        self.assertEqual(ledger_path.read_bytes(), ledger_before)

    def test_retention_preflight_rejects_native_association_before_effects(self):
        from tests.test_kit_development_workflow import CleanupPreparationTest

        cleanup = CleanupPreparationTest()
        self.addCleanup(cleanup.doCleanups)
        root = cleanup.fixture()
        self.task = "TASK-1"
        request = cleanup.request(root)
        ledger = load_template_module(
            root / ".kent/scripts/workflow-evidence-ledger",
            "cleanup_native_association_ledger",
        )
        directory = root / ".kent/runtime/TASK-1"
        source = directory / "plan-contract.json"
        source_before = source.read_bytes()
        ledger_path = directory / "evidence-ledger.jsonl"
        ledger_before = ledger_path.read_bytes()
        with mock.patch.dict(os.environ, cleanup.environment, clear=True):
            with mock.patch.object(
                ledger, "_verified_context",
                side_effect=ValueError("native association rejected"),
            ):
                with self.assertRaisesRegex(ValueError, "native association rejected"):
                    ledger.preflight_terminal_closure(
                        root,
                        task="TASK-1",
                        final_event=request["final_event"],
                        seal_request=request["seal_request"],
                        baseline={
                            "sequence": 1,
                            "event_hash": json.loads(ledger_before)["event_hash"],
                        },
                        retention=True,
                    )
        self.assertEqual(source.read_bytes(), source_before)
        self.assertEqual(ledger_path.read_bytes(), ledger_before)


if __name__ == "__main__":
    unittest.main()
