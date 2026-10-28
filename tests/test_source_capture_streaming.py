"""Streaming selected-Git capture and pure proof acceptance."""

import unittest
import hashlib
import json
import pickle
from pathlib import Path
import subprocess
import tempfile
import os
import sys
from unittest import mock

from workflowkit import revision, runtime


class StreamingBoundaryTests(unittest.TestCase):
    def schema4_fixture(self):
        from tests.test_revision import RevisionPreflightTest
        fixture = RevisionPreflightTest()
        self.addCleanup(fixture.doCleanups)
        def run_git(root, *args):
            if args[:1] == ("init",):
                self.assertFalse((root / ".git").exists())
                if not hasattr(self, "_owned_git_roots"):
                    self._owned_git_roots = set()
                self._owned_git_roots.add(root.resolve())
            result = self.owned_git(root, *args)
            return subprocess.CompletedProcess(result.args, result.returncode,
                                               result.stdout.decode(), result.stderr.decode())
        fixture.run_git = run_git
        return fixture, fixture.create_project(schema4=True)

    def owned_git(self, root, *args, input=None):
        root = Path(root).resolve()
        self.assertIn(root, getattr(self, "_owned_git_roots", set()))
        self.assertFalse((root / ".git").is_symlink())
        environment = revision.selected_git_environment()
        command = revision.selected_git_command(root, *args)
        if args[:1] != ("init",):
            self.assertTrue((root / ".git").is_dir())
            for query in ("--show-toplevel", "--git-common-dir"):
                observed = subprocess.check_output(
                    revision.selected_git_command(root, "rev-parse", "--path-format=absolute", query),
                    env=environment).decode().strip()
                self.assertEqual(Path(observed).resolve(), root if query == "--show-toplevel" else root / ".git")
        return subprocess.run(command, env=environment, input=input, capture_output=True, check=True)

    def fixture(self, payloads):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        if not hasattr(self, "_owned_git_roots"):
            self._owned_git_roots = set()
        self._owned_git_roots.add(root.resolve())
        for args in (("init", "-q"), ("config", "user.name", "Test"),
                     ("config", "user.email", "test@example.invalid")):
            self.owned_git(root, *args)
        descriptors = []
        for name, raw in payloads.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(raw)
            descriptors.append(runtime.RuntimeExternalRoot(
                "source-sha256", name + "=" + hashlib.sha256(raw).hexdigest(), True))
        self.owned_git(root, "add", ".")
        self.owned_git(root, "commit", "-qm", "fixture")
        commit = self.owned_git(root, "rev-parse", "HEAD").stdout.decode().strip()
        inputs = runtime._make_selected_runtime_source_inputs(
            project_name="fixture", repository="owner/repo", topology_kind="fixture",
            project_commit=commit, source_preview={}, artifact_digests={
                name: "a" * 64 for name in ("spec_raw_blob_sha256", "source_manifest_raw_blob_sha256",
                                           "snapshot_raw_blob_sha256")},
            external_roots=sorted(descriptors, key=lambda row: (row.kind, row.key)),
        )
        return root, inputs

    def test_explicit_streaming_api_exists(self):
        self.assertTrue(callable(getattr(revision, "verify_runtime_source_captures", None)))
        self.assertTrue(callable(getattr(runtime, "capture_runtime_source_envelope_verified", None)))

    def test_budget_nested_identity_and_replacement(self):
        self.assertTrue(issubclass(revision.SourceResourceBudgetExceeded, revision.RevisionPreflightError))
        self.assertTrue(issubclass(revision.SourceResourceBudgetExceeded, runtime.SourceResourceBudgetExceeded))
        with revision.source_read_budget() as outer:
            self.assertIsNotNone(outer)
            with revision.source_read_budget() as inner:
                self.assertIs(outer, inner)
            with self.assertRaises(ValueError):
                with revision.source_read_budget(revision.SourceReadPolicy()):
                    pass

    def test_large_complete_v2_and_explicit_v1_legacy_parity(self):
        root, inputs = self.fixture({"large": b"z" * (5 * 1024 * 1024)})
        with revision.source_read_budget() as budget:
            proof = revision.verify_runtime_source_captures(root, inputs, budget=budget)
        self.assertGreater(budget.logical_bytes, runtime.MAX_EXTERNAL_TOTAL_BYTES)
        result = runtime.capture_runtime_source_envelope_verified(inputs, proof, envelope_version=2)
        self.assertEqual(runtime.validate_runtime_source_envelope(result["runtime_source_envelope"]),
                         result["runtime_source_envelope"])
        with self.assertRaises(runtime.RuntimeContractError):
            runtime.capture_runtime_source_envelope_verified(inputs, proof, envelope_version=1)
        with self.assertRaises(TypeError):
            pickle.dumps(proof)
        for version in (True, False, None, "2", 0, 3):
            with self.assertRaises(runtime.RuntimeContractError):
                runtime.capture_runtime_source_envelope_verified(inputs, proof, envelope_version=version)
        with self.assertRaises(runtime.RuntimeContractError):
            runtime.VerifiedSourceCaptures(inputs, proof.observations)
        root, inputs = self.fixture({"small": b"hello\n"})
        proof = revision.verify_runtime_source_captures(root, inputs)
        self.assertEqual(runtime.capture_runtime_source_envelope_verified(inputs, proof, envelope_version=1),
                         runtime.capture_runtime_source_envelope(
                             inputs, revision.collect_runtime_external_captures(root, inputs)))

    def test_chunk_independence_alias_cache_and_sufficient_budget_identity(self):
        root, inputs = self.fixture({"a": b"\xc3\xa9" * 10000, "b": b"\xc3\xa9" * 10000})
        outputs = []
        for chunk in (1, 1023, 65536):
            with revision.source_read_budget(revision.SourceReadPolicy(chunk_bytes=chunk)) as budget:
                with mock.patch.object(revision, "_stream_blob", wraps=revision._stream_blob) as stream:
                    proof = revision.verify_runtime_source_captures(root, inputs, budget=budget)
                self.assertEqual(stream.call_count, 1)
                self.assertEqual(budget.logical_bytes, 40000)
                self.assertLess(budget.physical_bytes, budget.logical_bytes)
                self.assertLessEqual(budget.peak_chunk_bytes, chunk)
                self.assertEqual(budget.peak_processes, 1)
            outputs.append(runtime.capture_runtime_source_envelope_verified(inputs, proof, envelope_version=2))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])

    def test_budget_exhaustion_and_final_deadline_never_seal(self):
        root, inputs = self.fixture({"a": b"x" * 20000})
        for policy in (revision.SourceReadPolicy(physical_bytes=100),
                       revision.SourceReadPolicy(metadata_bytes=100),
                       revision.SourceReadPolicy(timeout_seconds=0.000001)):
            with self.subTest(policy=policy), mock.patch.object(revision, "_make_verified_source_captures") as seal:
                with self.assertRaises(runtime.SourceResourceBudgetExceeded):
                    with revision.source_read_budget(policy) as budget:
                        revision.verify_runtime_source_captures(root, inputs, budget=budget)
                seal.assert_not_called()
        with revision.source_read_budget() as budget:
            actual = revision._stream_blob
            def late(*args, **kwargs):
                result = actual(*args, **kwargs)
                budget.deadline = 0
                return result
            with mock.patch.object(revision, "_stream_blob", side_effect=late):
                with self.assertRaises(runtime.SourceResourceBudgetExceeded):
                    revision.verify_runtime_source_captures(root, inputs, budget=budget)
            budget.deadline = revision.time.monotonic() + 60  # restore finite cleanup allowance

    def test_partial_digest_failure_wrong_type_and_forged_proofs(self):
        root, inputs = self.fixture({"a": b"hello", "b": b"world"})
        changed = runtime._make_selected_runtime_source_inputs(
            project_name=inputs.project_name, repository=inputs.repository, topology_kind=inputs.topology_kind,
            project_commit=inputs.project_commit, source_preview={}, artifact_digests=inputs.artifact_digests,
            external_roots=(inputs.external_roots[0], runtime.RuntimeExternalRoot(
                "source-sha256", "b=" + "0" * 64, True)))
        with mock.patch.object(revision, "_make_verified_source_captures") as seal:
            with self.assertRaisesRegex(revision.RevisionPreflightError, "digest mismatch"):
                revision.verify_runtime_source_captures(root, changed)
            seal.assert_not_called()
        proof = revision.verify_runtime_source_captures(root, inputs)
        with self.assertRaises(runtime.RuntimeContractError):
            runtime.capture_runtime_source_envelope_verified(changed, proof, envelope_version=2)
        with self.assertRaises(runtime.RuntimeContractError):
            runtime.capture_runtime_source_envelope_verified(
                inputs, object.__new__(runtime.VerifiedSourceCaptures), envelope_version=2)
        for rows in (proof.observations[:1], proof.observations[::-1],
                     (proof.observations[0], proof.observations[0])):
            with self.assertRaises(runtime.RuntimeContractError):
                runtime._make_verified_source_captures(inputs, rows)

    def test_preflight_streams_large_additional_text_and_git_cli(self):
        fixture, root = self.schema4_fixture()
        raw = b"plain text\n" * (1024 * 1024)
        (root / "large.txt").write_bytes(raw)
        manifest_path = root / ".kent/release/source-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["additional_paths"] = ["large.txt"]
        manifest["external_roots"] = [{"kind": "source-sha256",
                                      "key": "large.txt=" + hashlib.sha256(raw).hexdigest(),
                                      "runtime_digest_required": True}]
        manifest_path.write_text(json.dumps(manifest))
        fixture.commit_all(root, "large unparsed source")
        commit = fixture.run_git(root, "rev-parse", "HEAD").stdout.strip()
        with revision.source_read_budget() as budget, mock.patch.object(
            revision, "read_blob_bytes", wraps=revision.read_blob_bytes,
        ) as eager:
            selected = revision.preflight_project_revision(root, commit, budget=budget)
            proof = revision.verify_runtime_source_captures(root, selected.runtime_source_inputs, budget=budget)
            self.assertFalse(any(call.args[2] == "large.txt" for call in eager.call_args_list))
            self.assertEqual(proof.observations[0].byte_count, len(raw))
        command = [sys.executable, "-m", "workflowkit.revision_cli", "--project", str(root),
                   "--ref", commit, "--capture-runtime-envelope-from-git", "--envelope-version", "2"]
        result = subprocess.run(command, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["runtime_source_envelope"]["schema"], "runtime-source-envelope-v2")
        self.assertNotEqual(subprocess.run(command[:-2], capture_output=True).returncode, 0)
        self.assertNotEqual(subprocess.run(command + ["--capture-runtime-envelope"],
                                           capture_output=True).returncode, 0)

    def test_verified_authority_binding_and_standalone_copy_parity(self):
        root, inputs = self.fixture({"small": b"contents"})
        proof = revision.verify_runtime_source_captures(root, inputs)
        execution = {"kind": "github_run", "repository": "owner/repo",
                     "workflow_path": ".github/workflows/release.yml", "workflow_name": "Release",
                     "event": "workflow_dispatch", "run_id": 7, "attempt": 1,
                     "head_sha": inputs.project_commit, "ref": "refs/heads/main"}
        context = runtime.capture_runtime_execution_context(inputs, execution)
        authority = {key: value for key, value in execution.items() if key != "repository"}
        binding = runtime.capture_runtime_authority_binding_verified(
            inputs, proof, context, authority, envelope_version=2)
        self.assertEqual(runtime._resolve_runtime_authority_binding(
            binding, context, runtime_source_envelope_digest=binding.runtime_source_envelope_digest)[
                "project_commit"], inputs.project_commit)
        eager = runtime.capture_runtime_authority_binding(
            inputs, revision.collect_runtime_external_captures(root, inputs), context, authority)
        streamed = runtime.capture_runtime_authority_binding_verified(
            inputs, proof, context, authority, envelope_version=1)
        self.assertEqual(eager.provenance_fingerprint, streamed.provenance_fingerprint)
        repo = Path(__file__).resolve().parents[1]
        self.assertEqual((repo / "workflowkit/runtime.py").read_bytes(),
                         (repo / ".kent/scripts/workflow_runtime_contracts.py").read_bytes())

    def test_stream_size_framing_type_and_process_cleanup(self):
        root, inputs = self.fixture({"small": b"contents"})
        entry = revision.tree_entry(root, inputs.project_commit, "small")
        for advertised in (b"7\n", b"9\n", b"8\nextra", b"-1\n"):
            actual = revision._run_git_bounded
            def wrong_size(*args, **kwargs):
                if args[1:3] == ("cat-file", "-s"):
                    return subprocess.CompletedProcess([], 0, advertised, b"")
                return actual(*args, **kwargs)
            with self.subTest(advertised=advertised), revision.source_read_budget() as budget:
                with mock.patch.object(revision, "_run_git_bounded", side_effect=wrong_size):
                    with self.assertRaises(revision.RevisionPreflightError):
                        revision._stream_blob(root, entry, budget)
                self.assertEqual(budget.active_processes, 0)
        with revision.source_read_budget() as budget, budget.process():
            with self.assertRaises(runtime.SourceResourceBudgetExceeded):
                revision.run_git_bytes(root, "rev-parse", "HEAD")

    def test_git_poison_replacements_modes_and_missing_objects_fail_closed(self):
        root, inputs = self.fixture({"small": b"original"})
        foreign, _ = self.fixture({"small": b"foreign"})
        alternate = self.owned_git(root, "hash-object", "-w", "--stdin", input=b"replaced").stdout.decode().strip()
        oid = revision.tree_entry(root, inputs.project_commit, "small").object_id
        self.owned_git(root, "replace", oid, alternate)
        with mock.patch.dict(os.environ, {
            "GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.bare", "GIT_CONFIG_VALUE_0": "true",
        }):
            proof = revision.verify_runtime_source_captures(root, inputs)
        self.assertEqual(proof.observations[0].sha256, hashlib.sha256(b"original").hexdigest())
        blob = root / ".git/objects" / oid[:2] / oid[2:]
        saved = blob.with_name(blob.name + ".saved")
        blob.rename(saved)
        try:
            with self.assertRaises(revision.RevisionPreflightError):
                revision.verify_runtime_source_captures(root, inputs)
        finally:
            saved.rename(blob)
        self.owned_git(root, "update-index", "--cacheinfo", f"120000,{oid},small")
        self.owned_git(root, "commit", "-qm", "bad mode")
        commit = self.owned_git(root, "rev-parse", "HEAD").stdout.decode().strip()
        changed = runtime._make_selected_runtime_source_inputs(
            project_name=inputs.project_name, repository=inputs.repository, topology_kind=inputs.topology_kind,
            project_commit=commit, source_preview={}, artifact_digests=inputs.artifact_digests,
            external_roots=inputs.external_roots)
        with self.assertRaisesRegex(revision.RevisionPreflightError, "regular Git blob"):
            revision.verify_runtime_source_captures(root, changed)

    def test_stream_stderr_deadline_and_children_reaped(self):
        root, inputs = self.fixture({"small": b"contents"})
        entry = revision.tree_entry(root, inputs.project_commit, "small")
        actual_command = revision.selected_git_command
        actual_popen = subprocess.Popen
        for program in ("import sys; sys.stderr.buffer.write(b'x'*100000)",
                        "import time; time.sleep(5)",
                        "import os,time; os.write(1,b'contents'); os.close(1); os.close(2); time.sleep(5)"):
            started = []
            def command(root, *args):
                if args[:2] == ("cat-file", "blob"):
                    return [sys.executable, "-c", program]
                return actual_command(root, *args)
            def spawn(*args, **kwargs):
                child = actual_popen(*args, **kwargs)
                started.append(child)
                return child
            with revision.source_read_budget(revision.SourceReadPolicy(
                stderr_bytes=32, command_seconds=0.2,
            )) as budget:
                with mock.patch.object(revision, "selected_git_command", side_effect=command), mock.patch.object(
                    revision.subprocess, "Popen", side_effect=spawn,
                ):
                    with self.assertRaises(runtime.SourceResourceBudgetExceeded):
                        revision._stream_blob(root, entry, budget)
                self.assertEqual(budget.active_processes, 0)
            self.assertTrue(started)
            for child in started:
                self.assertIsNotNone(child.poll())
                self.assertFalse(revision.owned_process_group_exists(child.pid))

    def test_stream_rejects_payload_not_matching_selected_object_id(self):
        root, inputs = self.fixture({"small": b"contents"})
        entry = revision.tree_entry(root, inputs.project_commit, "small")
        actual = revision.selected_git_command
        def command(root, *args):
            if args[:2] == ("cat-file", "blob"):
                return [sys.executable, "-c", "import os; os.write(1,b'corrupt!')"]
            return actual(root, *args)
        with revision.source_read_budget() as budget, mock.patch.object(
            revision, "selected_git_command", side_effect=command,
        ):
            with self.assertRaisesRegex(revision.RevisionPreflightError, "OID"):
                revision._stream_blob(root, entry, budget)

    def test_operation_blob_cache_reuses_only_verified_facts_not_path_checks(self):
        root, inputs = self.fixture({"a": b"source" * 10000, "b": b"source" * 10000})
        with revision.source_read_budget() as budget, mock.patch.object(
            revision, "_run_git_bounded", wraps=revision._run_git_bounded,
        ) as reads, mock.patch.object(revision, "tree_entry", wraps=revision.tree_entry) as paths:
            first = revision.verify_runtime_source_captures(root, inputs, budget=budget)
            second = revision.verify_runtime_source_captures(root, inputs, budget=budget)
            self.assertEqual(first.observations, second.observations)
            self.assertEqual(paths.call_count, 4)
            self.assertEqual(budget.logical_bytes, 240000)
            size_reads = [call for call in reads.call_args_list if call.args[1:3] == ("cat-file", "-s")]
            self.assertEqual(len(size_reads), 1, "operation reread an already verified immutable blob")
            actual = revision.tree_entry
            def invalid_mode(*args):
                from dataclasses import replace
                return replace(actual(*args), mode="120000")
            with mock.patch.object(revision, "tree_entry", side_effect=invalid_mode):
                with self.assertRaises(revision.RevisionPreflightError):
                    revision.verify_runtime_source_captures(root, inputs, budget=budget)

    def test_operation_blob_cache_does_not_skip_utf8_or_cross_repository_checks(self):
        root, inputs = self.fixture({"binary": b"\xff"})
        foreign, foreign_inputs = self.fixture({"binary": b"\xff"})
        with revision.source_read_budget() as budget:
            revision.verify_runtime_source_captures(root, inputs, budget=budget)
            physical = budget.physical_bytes
            entry = revision.tree_entry(root, inputs.project_commit, "binary")
            with self.assertRaisesRegex(revision.RevisionPreflightError, "UTF-8"):
                revision._stream_blob(root, entry, budget, textual=True)
            self.assertGreater(budget.physical_bytes, physical)
            physical = budget.physical_bytes
            revision.verify_runtime_source_captures(foreign, foreign_inputs, budget=budget)
            self.assertGreater(budget.physical_bytes - physical, 1)

    def test_operation_tree_cache_walks_each_path_and_never_rebinds_roots_or_commits(self):
        root, inputs = self.fixture({"dir/a": b"a", "dir/b": b"b"})
        with revision.source_read_budget() as budget, mock.patch.object(
            revision, "run_git_bytes", wraps=revision.run_git_bytes,
        ) as reads:
            for _ in range(2):
                a = revision.tree_entry(root, inputs.project_commit, "dir/a")
                b = revision.tree_entry(root, inputs.project_commit, "dir/b")
                self.assertEqual(a.mode, "100644")
                self.assertNotEqual(a.object_id, b.object_id)
            tree_reads = [call for call in reads.call_args_list if call.args[1] == "ls-tree"]
            self.assertEqual(len(tree_reads), 2, "immutable root/directory tree metadata reread")
            self.assertIsNone(revision.tree_entry(root, inputs.project_commit, "dir/absent"))
            (root / "dir/a").chmod(0o755)
            self.owned_git(root, "add", ".")
            self.owned_git(root, "commit", "-qm", "different mode")
            other = self.owned_git(root, "rev-parse", "HEAD").stdout.decode().strip()
            self.assertEqual(revision.tree_entry(root, other, "dir/a").mode, "100755")
            self.assertEqual(revision.tree_entry(root, inputs.project_commit, "dir/a").mode, "100644")
            foreign, foreign_inputs = self.fixture({"dir/a": b"foreign"})
            self.assertNotEqual(revision.tree_entry(foreign, foreign_inputs.project_commit, "dir/a").object_id,
                                a.object_id)

    def test_tree_cache_rejects_changed_immutable_metadata_and_remains_capacity_bounded(self):
        root, inputs = self.fixture({"a": b"one", "b": b"two"})
        oid = self.owned_git(root, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
        actual = revision.run_git_bytes
        def corrupt(*args, **kwargs):
            result = actual(*args, **kwargs)
            if args[1] == "ls-tree":
                raw = result.stdout.replace(b"100644", b"100755", 1)
                return subprocess.CompletedProcess(result.args, result.returncode, raw, result.stderr)
            return result
        with revision.source_read_budget() as budget, mock.patch.object(
            revision, "run_git_bytes", side_effect=corrupt,
        ):
            with self.assertRaisesRegex(revision.RevisionPreflightError, "immutable OID"):
                revision.tree_entries(root, oid)
            self.assertFalse(budget._tree_facts)
        with revision.source_read_budget(revision.SourceReadPolicy(descriptor_records=1)) as budget:
            with self.assertRaises(runtime.SourceResourceBudgetExceeded):
                revision.tree_entries(root, oid)
            self.assertFalse(budget._tree_facts)

    def test_legacy_raw_stream_handles_git_sha256_oid_without_changing_runtime_commit_wire(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        if not hasattr(self, "_owned_git_roots"):
            self._owned_git_roots = set()
        self._owned_git_roots.add(root.resolve())
        try:
            self.owned_git(root, "init", "-q", "--object-format=sha256")
        except subprocess.CalledProcessError:
            self.skipTest("host Git does not support SHA-256 repositories")
        self.owned_git(root, "config", "user.name", "Test")
        self.owned_git(root, "config", "user.email", "test@example.invalid")
        (root / "source").write_bytes(b"contents")
        self.owned_git(root, "add", ".")
        self.owned_git(root, "commit", "-qm", "sha256 fixture")
        commit = self.owned_git(root, "rev-parse", "HEAD").stdout.decode().strip()
        with revision.source_read_budget() as budget:
            entry = revision.tree_entry(root, commit, "source")
            self.assertEqual(len(entry.object_id), 64)
            self.assertEqual(revision._stream_blob(root, entry, budget),
                             (8, hashlib.sha256(b"contents").hexdigest()))

    def test_fixture_git_poison_cannot_mutate_foreign_owned_sentinel(self):
        with mock.patch.dict(os.environ, revision.selected_git_environment(), clear=True):
            foreign, _ = self.fixture({"sentinel": b"untouched"})
        self.owned_git(foreign, "config", "user.name", "Foreign sentinel")
        hook = foreign / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\necho ran > foreign-hook-ran\n")
        hook.chmod(0o755)
        snapshot = lambda: {str(path.relative_to(foreign)): path.read_bytes()
                            for path in foreign.rglob("*") if path.is_file()}
        before = snapshot()
        with mock.patch.dict(os.environ, {"GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
                                         "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
                                         "GIT_CONFIG_VALUE_0": str(foreign / ".git/hooks")}):
            try:
                self.fixture({"new": b"new owned data"})
            except subprocess.CalledProcessError:
                pass  # still compare foreign state on the known unsafe baseline
        self.assertEqual(snapshot(), before)

    def test_metadata_descriptor_document_and_header_limits_are_independent(self):
        root, inputs = self.fixture({"small": b"contents", "alias": b"contents"})
        for policy in (revision.SourceReadPolicy(descriptor_records=1), revision.SourceReadPolicy(header_bytes=1)):
            with revision.source_read_budget(policy) as budget:
                with self.assertRaises(runtime.SourceResourceBudgetExceeded):
                    revision.verify_runtime_source_captures(root, inputs, budget=budget)
        fixture, root = self.schema4_fixture()
        with revision.source_read_budget(revision.SourceReadPolicy(document_bytes=1)) as budget:
            with self.assertRaisesRegex(runtime.SourceResourceBudgetExceeded, "byte limit"):
                revision.preflight_project_revision(root, "HEAD", budget=budget)

    @unittest.skipUnless(os.environ.get("KENT_STREAM_MEASURE") == "1", "explicit resource measurement")
    def test_measure_python_and_git_peaks(self):
        code = """
import json,resource,sys,time,tracemalloc
from pathlib import Path
from workflowkit import revision,runtime
root=Path(sys.argv[1]); data=json.loads(sys.argv[2])
inputs=runtime._make_selected_runtime_source_inputs(project_name="fixture",repository="owner/repo",
 topology_kind="fixture",project_commit=data["project_commit"],source_preview={},
 artifact_digests=data["artifact_digests"],
 external_roots=[runtime.RuntimeExternalRoot.from_mapping(row) for row in data["external_roots"]])
tracemalloc.start(); start=time.monotonic()
with revision.source_read_budget() as budget:
 proof=revision.verify_runtime_source_captures(root,inputs,budget=budget)
 envelope=runtime.capture_runtime_source_envelope_verified(inputs,proof,envelope_version=2)
print(json.dumps(dict(python_peak=tracemalloc.get_traced_memory()[1],
 git_peak=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,elapsed=time.monotonic()-start,
 physical=budget.physical_bytes,logical=budget.logical_bytes,metadata=budget.metadata_bytes,
 records=budget.records,chunk_peak=budget.peak_chunk_bytes,process_peak=budget.peak_processes)))
"""
        rows = []
        for count, size in ((4, 256 * 1024), (4, 1024 * 1024), (4, 4 * 1024 * 1024),
                            (1, 1024), (32, 1024), (128, 1024)):
            root, inputs = self.fixture({f"file-{i:04d}": bytes([65 + i % 26]) * size for i in range(count)})
            result = subprocess.run([sys.executable, "-c", code, str(root), json.dumps(inputs.as_dict())],
                                    capture_output=True, text=True, check=True)
            row = json.loads(result.stdout)
            row.update(count=count, size=size, disk=sum(path.stat().st_size for path in root.rglob("*")
                                                       if path.is_file()))
            rows.append(row)
        print("STREAM_MEASUREMENTS=" + json.dumps(rows, sort_keys=True))
        self.assertLess(max(row["python_peak"] for row in rows[:3]), 2 * 1024 * 1024)

    @unittest.skipUnless(os.environ.get("KENT_STREAM_MEASURE") == "1", "explicit resource measurement")
    def test_measure_preflight_and_verification_peaks(self):
        code = """
import json,resource,sys,time,tracemalloc
from pathlib import Path
from workflowkit import revision,runtime
tracemalloc.start();start=time.monotonic()
with revision.source_read_budget(revision.SourceReadPolicy(physical_bytes=64*1024*1024)) as budget:
 result=revision.preflight_project_revision(Path(sys.argv[1]),sys.argv[2],budget=budget)
 proof=revision.verify_runtime_source_captures(Path(sys.argv[1]),result.runtime_source_inputs,budget=budget)
 runtime.capture_runtime_source_envelope_verified(result.runtime_source_inputs,proof,envelope_version=2)
print(json.dumps(dict(python_peak=tracemalloc.get_traced_memory()[1],
 git_peak=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,elapsed=time.monotonic()-start,
 physical=budget.physical_bytes,logical=budget.logical_bytes,metadata=budget.metadata_bytes,
 records=budget.records,chunk_peak=budget.peak_chunk_bytes,process_peak=budget.peak_processes)))
"""
        rows = []
        for size in (256 * 1024, 1024 * 1024, 4 * 1024 * 1024):
            fixture, root = self.schema4_fixture()
            paths = []
            descriptors = []
            for index in range(4):
                path = f"large-{index}.txt"
                raw = bytes([65 + index]) * size
                (root / path).write_bytes(raw)
                paths.append(path)
                descriptors.append({"kind": "source-sha256", "key": path + "=" + hashlib.sha256(raw).hexdigest(),
                                    "runtime_digest_required": True})
            manifest_path = root / ".kent/release/source-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest.update(additional_paths=paths, external_roots=descriptors)
            manifest_path.write_text(json.dumps(manifest))
            fixture.commit_all(root, "large closure")
            commit = fixture.run_git(root, "rev-parse", "HEAD").stdout.strip()
            result = subprocess.run([sys.executable, "-c", code, str(root), commit],
                                    capture_output=True, text=True, check=True)
            row = json.loads(result.stdout)
            row.update(aggregate=4 * size, disk=sum(path.stat().st_size for path in root.rglob("*")
                                                  if path.is_file()))
            rows.append(row)
        print("PREFLIGHT_MEASUREMENTS=" + json.dumps(rows, sort_keys=True))
        self.assertLess(max(row["python_peak"] for row in rows), 2 * 1024 * 1024)
