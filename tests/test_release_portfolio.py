from __future__ import annotations

import ast
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import workflowkit.operations as operations
from workflowkit.operations import (
    JournalError,
    OperationError,
    PlanValidationError,
    canonical_bytes,
    load_plan,
    verify_release_portfolio,
)
from tests import test_revision as revision_fixtures


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(root), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def plan_file(
    root: Path,
    value: dict,
    name: str = "plan.json",
) -> tuple[Path, str]:
    path = root / name
    raw = canonical_bytes(value)
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def load_portfolio_plan(
    root: Path,
    value: dict,
    name: str = "plan.json",
) -> operations.LoadedPlan:
    path, digest = plan_file(root, value, name)
    return load_plan(
        path,
        schema="release-portfolio-plan-v1",
        expected_sha256=digest,
    )


def create_repository(root: Path, name: str, repository: str) -> tuple[Path, str]:
    path = root / name
    path.mkdir()
    git(path, "init", "-q")
    git(path, "branch", "-M", "main")
    git(path, "config", "user.name", "Kent Test")
    git(path, "config", "user.email", "kent@example.invalid")
    (path / "tracked").write_text(repository + "\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "selected revision")
    git(path, "remote", "add", "origin", f"https://github.com/{repository}.git")
    return path, git(path, "rev-parse", "HEAD")


def portfolio_fixture(root: Path, report_path: Path | None = None) -> dict:
    kit_root, kit_commit = create_repository(root, "kit", "owner/kit")
    project_rows = []
    for index in range(4):
        repository = f"owner/project-{index + 1}"
        project_root, commit = create_repository(
            root,
            f"project-{index + 1}",
            repository,
        )
        project_rows.append(
            {
                "root": str(project_root),
                "repository": repository,
                "commit": commit,
            }
        )
    value = {
        "schema": "release-portfolio-plan-v1",
        "kit": {
            "root": str(kit_root),
            "repository": "owner/kit",
            "commit": kit_commit,
        },
        "projects": project_rows,
    }
    if report_path is not None:
        value["report_path"] = str(report_path)
    return {
        "root": root,
        "value": value,
        "plan": load_portfolio_plan(root, value),
        "kit_root": kit_root,
        "projects": [Path(row["root"]) for row in project_rows],
    }


def ready_revision(_root: Path, commit: str) -> SimpleNamespace:
    return SimpleNamespace(
        commit_oid=commit,
        release_preview={"commit": commit, "ready": True},
        runtime_source_inputs=SimpleNamespace(
            selected_runtime_source_inputs_sha256=hashlib.sha256(
                f"runtime:{commit}".encode()
            ).hexdigest()
        ),
    )


def selected_digests(_root: Path, commit: str) -> dict[str, str | None]:
    values = {
        key: hashlib.sha256(f"{key}:{commit}".encode()).hexdigest()
        for key in (
            "profile_sha256",
            "release_spec_sha256",
            "source_manifest_sha256",
            "snapshot_sha256",
        )
    }
    values["builder_sha256"] = hashlib.sha256(f"builder:{commit}".encode()).hexdigest()
    return values


def verify_ready(fixture: dict, **kwargs: object) -> dict:
    with mock.patch.object(
        operations,
        "preflight_project_revision",
        side_effect=ready_revision,
    ):
        with mock.patch.object(
            operations,
            "_selected_source_digests",
            side_effect=selected_digests,
        ):
            return verify_release_portfolio(fixture["plan"], **kwargs)


def duplicate_top_level_names(source: str) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in seen:
                duplicates.add(node.name)
            seen.add(node.name)
    return sorted(duplicates)


class ReleasePortfolioTest(unittest.TestCase):
    def test_raw_plan_boundaries_are_canonical_closed_and_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "raw.json"
            path.write_bytes(b'{ "schema": "release-portfolio-plan-v1" }')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaises(PlanValidationError):
                load_plan(
                    path,
                    schema="release-portfolio-plan-v1",
                    expected_sha256=digest,
                )
            path.write_bytes(
                b'{"schema":"release-portfolio-plan-v1",'
                b'"schema":"release-portfolio-plan-v1"}'
            )
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaises(PlanValidationError):
                load_plan(
                    path,
                    schema="release-portfolio-plan-v1",
                    expected_sha256=digest,
                )
            value = {"schema": "release-portfolio-plan-v1"}
            canonical_path, canonical_digest = plan_file(root, value, "canonical.json")
            loaded = load_plan(
                canonical_path,
                schema="release-portfolio-plan-v1",
                expected_sha256=canonical_digest,
            )
            self.assertEqual(loaded.raw, canonical_bytes(value))
            with self.assertRaises(PlanValidationError):
                load_plan(
                    canonical_path,
                    schema="release-portfolio-plan-v1",
                    expected_sha256="0" * 64,
                )
            with self.assertRaises(PlanValidationError):
                load_plan(
                    canonical_path,
                    schema="release-portfolio-plan-v1",
                    expected_sha256=canonical_digest,
                    mutation=True,
                    confirm=None,
                )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = portfolio_fixture(root)
            for label, mutate in (
                ("top-level", lambda value: value.update(unexpected=True)),
                (
                    "nested",
                    lambda value: value["projects"][0].update(unexpected=True),
                ),
            ):
                with self.subTest(label=label):
                    value = json.loads(json.dumps(fixture["value"]))
                    mutate(value)
                    plan = load_portfolio_plan(root, value, f"unknown-{label}.json")
                    with self.assertRaises(PlanValidationError):
                        verify_release_portfolio(plan)

    def test_schema_three_selected_project_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = portfolio_fixture(root)
            builder = revision_fixtures.RevisionPreflightTest()
            self.addCleanup(builder.doCleanups)
            schema_three = builder.create_project()
            repository = fixture["value"]["projects"][0]["repository"]
            git(
                schema_three,
                "remote",
                "add",
                "origin",
                f"https://github.com/{repository}.git",
            )
            commit = git(schema_three, "rev-parse", "HEAD")
            value = json.loads(json.dumps(fixture["value"]))
            value["projects"][0] = {
                "root": str(schema_three),
                "repository": repository,
                "commit": commit,
            }
            fixture["plan"] = load_portfolio_plan(root, value, "schema-three.json")
            schema_three_result = operations.preflight_project_revision(
                schema_three,
                commit,
            )
            self.assertIsNone(schema_three_result.release_preview)
            self.assertIsNone(schema_three_result.runtime_source_inputs)

            def preflight(project_root: Path, selected: str) -> SimpleNamespace:
                if Path(project_root) == schema_three:
                    return schema_three_result
                return ready_revision(project_root, selected)

            with mock.patch.object(
                operations,
                "preflight_project_revision",
                side_effect=preflight,
            ):
                with mock.patch.object(
                    operations,
                    "_selected_source_digests",
                    side_effect=AssertionError("schema-3 source accepted"),
                ):
                    with self.assertRaisesRegex(OperationError, "schema-4"):
                        verify_release_portfolio(fixture["plan"])

    def test_missing_runtime_source_input_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = portfolio_fixture(Path(temporary))

            def incomplete(_root: Path, commit: str) -> SimpleNamespace:
                return SimpleNamespace(
                    commit_oid=commit,
                    release_preview={"ready": True},
                    runtime_source_inputs=None,
                )

            with mock.patch.object(
                operations,
                "preflight_project_revision",
                side_effect=incomplete,
            ):
                with mock.patch.object(
                    operations,
                    "_selected_source_digests",
                    side_effect=AssertionError("incomplete source accepted"),
                ):
                    with self.assertRaisesRegex(OperationError, "schema-4"):
                        verify_release_portfolio(fixture["plan"])

    def test_selected_commit_proof_ignores_dirty_working_tree_bytes(self) -> None:
        builder = revision_fixtures.RevisionPreflightTest()
        self.addCleanup(builder.doCleanups)
        project = builder.create_project(schema4=True)
        commit = git(project, "rev-parse", "HEAD")
        expected = operations._selected_source_digests(project, commit)
        (project / ".kent/workflow-profile.toml").write_bytes(b"\xff")
        (project / ".kent/release/spec.toml").write_text("invalid = true\n")
        self.assertTrue(git(project, "status", "--porcelain"))
        result = operations.preflight_project_revision(project, commit)
        self.assertEqual(result.commit_oid, commit)
        self.assertIsNotNone(result.release_preview)
        self.assertIsNotNone(result.runtime_source_inputs)
        self.assertEqual(operations._selected_source_digests(project, commit), expected)

    def test_origin_commit_and_unique_project_identity_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = portfolio_fixture(Path(temporary))
            git(fixture["kit_root"], "remote", "remove", "origin")
            with self.assertRaises(OperationError):
                verify_ready(fixture)
        with tempfile.TemporaryDirectory() as temporary:
            fixture = portfolio_fixture(Path(temporary))
            git(
                fixture["projects"][0],
                "remote",
                "set-url",
                "origin",
                "https://github.com/owner/foreign.git",
            )
            with self.assertRaises(OperationError):
                verify_ready(fixture)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = portfolio_fixture(root)
            value = json.loads(json.dumps(fixture["value"]))
            value["kit"]["commit"] = "f" * 40
            fixture["plan"] = load_portfolio_plan(root, value, "missing-commit.json")
            with self.assertRaises(OperationError):
                verify_ready(fixture)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = portfolio_fixture(root)
            value = json.loads(json.dumps(fixture["value"]))
            duplicate = value["projects"][0]["repository"]
            value["projects"][1]["repository"] = duplicate
            git(
                fixture["projects"][1],
                "remote",
                "set-url",
                "origin",
                f"https://github.com/{duplicate}.git",
            )
            fixture["plan"] = load_portfolio_plan(root, value, "duplicate.json")
            with self.assertRaises(PlanValidationError):
                verify_ready(fixture)

    def test_report_path_is_plan_bound_and_write_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = portfolio_fixture(root)
            supplied = root / "supplied.json"
            with self.assertRaises(PlanValidationError):
                verify_release_portfolio(
                    fixture["plan"],
                    report_path=supplied,
                    write_report=False,
                )
            with self.assertRaises(PlanValidationError):
                verify_ready(fixture, write_report=True)
            self.assertFalse(supplied.exists())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "report.json"
            fixture = portfolio_fixture(root, report)
            other = root / "other.json"
            with self.assertRaises(PlanValidationError):
                verify_ready(
                    fixture,
                    report_path=other,
                    write_report=True,
                )
            self.assertFalse(report.exists())
            self.assertFalse(other.exists())
            self.assertTrue(verify_ready(fixture)["ready"])
            self.assertFalse(report.exists())

    def test_report_lock_contention_blocks_the_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "report.json"
            fixture = portfolio_fixture(root, report)
            lock = report.with_name(f".{report.name}.lock")
            descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(JournalError):
                    verify_ready(
                        fixture,
                        report_path=report,
                        write_report=True,
                    )
                self.assertFalse(report.exists())
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def test_report_write_has_exact_bytes_and_readback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "report.json"
            fixture = portfolio_fixture(root, report_path)
            report = verify_ready(
                fixture,
                report_path=report_path,
                write_report=True,
            )
            self.assertEqual(
                report_path.read_bytes(),
                canonical_bytes(report) + b"\n",
            )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "report.json"
            fixture = portfolio_fixture(root, report_path)
            read_bytes = Path.read_bytes

            def mismatching_readback(path: Path) -> bytes:
                if path == report_path:
                    return b"mismatch\n"
                return read_bytes(path)

            with mock.patch.object(Path, "read_bytes", mismatching_readback):
                with self.assertRaises(JournalError):
                    verify_ready(
                        fixture,
                        report_path=report_path,
                        write_report=True,
                    )

    def test_operations_module_rejects_duplicate_top_level_symbols(self) -> None:
        source = Path(operations.__file__).read_text()
        self.assertEqual(duplicate_top_level_names(source), [])
        duplicated = "def repeated():\n    pass\nclass repeated:\n    pass\n"
        self.assertEqual(duplicate_top_level_names(duplicated), ["repeated"])


if __name__ == "__main__":
    unittest.main()
