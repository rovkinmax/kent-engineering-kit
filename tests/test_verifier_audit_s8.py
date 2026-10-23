from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import unittest
from unittest import mock

import test_kit_delivery_slice as delivery_test_helpers
import test_workflow_runtime as workflow_test_helpers


def load_identity(path: Path = delivery_test_helpers.IDENTITY):
    spec = importlib.util.spec_from_file_location("verifier_audit_identity", path)
    if spec is None or spec.loader is None:
        raise AssertionError("identity helper cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VerifierAuditS8Tests(unittest.TestCase):
    def delivery_fixture(self) -> tuple[unittest.TestCase, Path]:
        owner = delivery_test_helpers.DeliveryBaselineFixtureTest()
        self.addCleanup(owner.doCleanups)
        return owner, owner.fixture()

    def report_fixture(self) -> tuple[unittest.TestCase, Path]:
        owner = workflow_test_helpers.WorkflowVerifyReportTest()
        self.addCleanup(owner.doCleanups)
        return owner, owner.create_repository()

    def test_explicit_python_override_is_forwarded_and_unrelated_env_is_not(self):
        owner, root = self.delivery_fixture()
        wrapper = root / ".kent/scripts/workflow-verify-report"
        shutil.copyfile(workflow_test_helpers.VERIFY_REPORT, wrapper)
        wrapper.chmod(0o755)
        tools = root / "build/kent-workflow/approved-python"
        tools.mkdir(parents=True)
        marker = tools / "selected"
        launcher = tools / "python"
        launcher.write_text(
            "#!/bin/bash\n"
            f"echo selected >> {shlex.quote(str(marker))}\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n"
        )
        launcher.chmod(0o700)
        validator = root / "scripts/validate"
        validator.write_text(
            "#!/bin/bash\nset -eu\n"
            "printf '%s\\n' "
            '"${KENT_VERIFIER_AUDIT_UNRELATED-unset}" '
            "> build/kent-workflow/unrelated-env\n"
            "echo fixture-validation-log\n"
        )
        validator.chmod(0o755)

        environment = dict(os.environ)
        environment["KENT_ENGINEERING_KIT_PYTHON"] = str(launcher)
        environment["KENT_VERIFIER_AUDIT_UNRELATED"] = "must-not-forward"
        result = subprocess.run(
            [str(root / ".kent/scripts/workflow-verify-report")],
            cwd=root,
            input=json.dumps({"workspace_path": str(root)}),
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["verification_status"], "passed", payload)
        self.assertTrue(marker.exists(), "wrapper dropped the named Python override")
        self.assertEqual(
            (root / "build/kent-workflow/unrelated-env").read_text().strip(),
            "unset",
        )
        self.assertGreater(marker.read_text().count("selected"), 0)

    def test_environment_identity_fingerprints_path_resolved_python3(self):
        owner, root = self.delivery_fixture()
        identity = load_identity()
        tools = root / "fixture-tools"
        tools.mkdir()
        python3 = tools / "python3"
        python3.write_text("#!/bin/sh\necho fixture-python3-v1\n")
        python3.chmod(0o700)
        tmp = root / "build/kent-workflow/.verify-tmp-identity"
        tmp.mkdir(parents=True, mode=0o700)
        path = os.pathsep.join((str(tools), os.environ.get("PATH", "")))
        selected = Path(sys.executable).resolve()

        with mock.patch.dict(os.environ, {"PATH": path, "TMPDIR": str(tmp)}):
            before = identity._environment_identity(root, selected)
            python3.write_text("#!/bin/sh\necho fixture-python3-v2\n")
            after = identity._environment_identity(root, selected)

        self.assertNotEqual(before, after)

    def test_git_source_entry_budget_is_applied_after_deduplication(self):
        owner, root = self.delivery_fixture()
        identity = load_identity()
        validator = root / "scripts/validate"
        validator.write_text(validator.read_text() + "# modified\n")
        git = identity._which("git", os.environ.get("PATH", ""))
        self.assertIsNotNone(git)
        git = Path(git)
        command = [
            str(git), "-C", str(root), "ls-files", "--cached", "--modified",
            "--deleted", "--others", "--exclude-standard", "--full-name", "-z",
        ]
        raw = subprocess.check_output(command)
        entries = [entry for entry in raw.split(b"\0") if entry]
        unique_count = len(set(entries))
        self.assertGreater(len(entries), unique_count)

        with mock.patch.object(identity, "MAX_SOURCE_ENTRIES", unique_count):
            inventory = identity._git_path_inventory(root, git)
        self.assertEqual(inventory, sorted(set(entries)))

        with mock.patch.object(identity, "MAX_SOURCE_ENTRIES", unique_count - 1):
            with self.assertRaisesRegex(identity.IdentityError, "entry bound"):
                identity._git_path_inventory(root, git)

    def test_ignored_regular_symlink_endpoints_are_hashed_once_and_stably(self):
        owner, root = self.delivery_fixture()
        identity = load_identity()
        target = root / "build/kent-workflow/referenced-input"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"stable endpoint\n")
        unrelated = target.parent / "unrelated-output"
        unrelated.write_bytes(b"not referenced\n")
        ignored_directory = target.parent / "ignored-directory"
        ignored_directory.mkdir()
        directory_child = ignored_directory / "child"
        directory_child.write_bytes(b"not recursively inventoried\n")
        for name in ("referenced-input", "referenced-input-alias"):
            (root / name).symlink_to(
                os.path.relpath(target, root),
            )
        (root / "referenced-directory").symlink_to(
            os.path.relpath(ignored_directory, root),
            target_is_directory=True,
        )

        git = identity._which("git", os.environ.get("PATH", ""))
        self.assertIsNotNone(git)
        head = identity._git_head(root, Path(git))
        original_hash = identity._hash_file
        endpoint_hashes: list[Path] = []

        def track_endpoint(path: Path, *, limit: int):
            if path.resolve() == target.resolve():
                endpoint_hashes.append(path)
            return original_hash(path, limit=limit)

        source_paths = identity._git_path_inventory(root, Path(git))
        direct_bytes = sum(
            (root / os.fsdecode(item)).lstat().st_size
            for item in source_paths
            if stat.S_ISREG((root / os.fsdecode(item)).lstat().st_mode)
            and not (root / os.fsdecode(item)).is_symlink()
        )
        with mock.patch.object(identity, "MAX_SOURCE_ENTRIES", len(source_paths)):
            with self.assertRaisesRegex(identity.IdentityError, "entry bound"):
                identity._source_identity(root, Path(git), head)
        with (
            mock.patch.object(
                identity,
                "MAX_SOURCE_ENTRIES",
                len(source_paths) + 1,
            ),
            mock.patch.object(identity, "MAX_SOURCE_BYTES", direct_bytes + target.stat().st_size),
            mock.patch.object(identity, "_hash_file", side_effect=track_endpoint),
        ):
            before = identity._source_identity(root, Path(git), head)
            unrelated.write_bytes(b"changed unrelated output\n")
            directory_child.write_bytes(b"ignored child changed\n")
            stable = identity._source_identity(root, Path(git), head)
            target.write_bytes(b"newish endpoint\n")
            changed = identity._source_identity(root, Path(git), head)

        self.assertEqual(before, stable)
        self.assertNotEqual(before, changed)
        self.assertEqual(len(endpoint_hashes), 3)

    def test_owned_build_descriptor_closes_when_log_preparation_fails(self):
        owner, root = self.report_fixture()
        scripts = owner.install_verify_fixture(
            root,
            "#!/bin/sh\n"
            "echo '{\"transition\":\"passed\"}'\n",
        )
        build = root / "build"
        build.mkdir()
        unsafe_workflow = build / "kent-workflow"
        unsafe_workflow.write_text("not a directory\n")
        module = workflow_test_helpers.load_template_module(
            scripts / "workflow-verify-report",
            "verifier_audit_fd_cleanup",
        )
        opened: list[int] = []
        real_open = os.open

        def capture_open(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        try:
            with mock.patch.object(module.os, "open", side_effect=capture_open):
                report = owner.run_module_in_workspace(module, root)
            self.assertEqual(report["code"], "log_path_unsafe", report)
            leaked_build_descriptors = []
            build_info = build.stat()
            for descriptor in opened:
                try:
                    opened_info = os.fstat(descriptor)
                except OSError as error:
                    if error.errno != errno.EBADF:
                        raise
                    continue
                if (
                    opened_info.st_dev,
                    opened_info.st_ino,
                ) == (build_info.st_dev, build_info.st_ino):
                    leaked_build_descriptors.append(descriptor)
            self.assertEqual(leaked_build_descriptors, [])
        finally:
            for descriptor in opened:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def test_malformed_child_transition_retains_created_log_reference(self):
        owner = workflow_test_helpers.WorkflowVerifyReportTest()
        self.addCleanup(owner.doCleanups)
        for transition in ([], {}):
            with self.subTest(transition=transition):
                root = owner.create_repository()
                output = json.dumps({"transition": transition})
                scripts = owner.install_verify_fixture(
                    root,
                    "#!/bin/sh\n"
                    f"echo '{output}'\n"
                    "echo DIAGNOSTIC >&2\n",
                )
                payload = owner.run_verify_command(root)
                report = owner.parse_verification_report(payload)

                self.assertEqual(report["code"], "child_output_invalid", report)
                self.assertTrue(report["log_path"], report)
                log = root / str(report["log_path"])
                content = log.read_bytes()
                self.assertEqual(
                    hashlib.sha256(content).hexdigest(),
                    report["log_sha256"],
                )
                self.assertIn(b"DIAGNOSTIC", content)
