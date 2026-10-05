from __future__ import annotations

import hashlib
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import json
import os
import stat
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests/fixtures/mock-scenario"
FIXTURE_MANIFEST = FIXTURE_ROOT / "baseline-verifiers.json"
PRODUCTION_FIXTURE_MANIFEST = FIXTURE_ROOT / "production-source-manifest.json"
PRE_EDIT_ARTIFACT_DIR = ROOT / "build/kent-workflow/KEN-11"
PRE_EDIT_REPORT = PRE_EDIT_ARTIFACT_DIR / "pre-edit-red.json"
PRE_EDIT_LOG = PRE_EDIT_ARTIFACT_DIR / "pre-edit-red.log"
SUPPORTED_WORK_KINDS = {
    "feature",
    "bugfix",
    "refactor",
    "migration",
    "dependency",
    "test",
}
FAKE_GRADLE = (
    "#!/bin/sh\n"
    "set -eu\n"
    'if [ "$*" != ":app:compileDevDebugKotlin" ]; then\n'
    '  printf "unexpected fixture compile command: %s\\n" "$*" >&2\n'
    "  exit 77\n"
    "fi\n"
    'printf "fixture compile accepted\\n"\n'
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_deterministic_artifact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise AssertionError(f"existing evidence differs: {path}")
        return
    path.write_bytes(content)


class MockScenarioBaselineTest(unittest.TestCase):
    def load_manifest(self) -> dict[str, object]:
        manifest = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "mock-scenario-baseline-verifiers-v1")
        self.assertEqual(manifest["task_short_id"], "KEN-11")
        self.assertEqual(manifest["fixed_point"], "22c7e1821a194d7e867dde81ddfd10187c8486c3")
        self.assertEqual(manifest["expected_transition"], "blocked")
        self.assertEqual(
            set(manifest["projects"]),
            {"AppsomeAndroid", "Puber"},
        )
        return manifest

    def run_baseline_verifier(
        self,
        project: str,
        metadata: dict[str, object],
    ) -> tuple[dict[str, object], str]:
        fixture_path = (FIXTURE_ROOT / str(metadata["fixture_path"])).resolve()
        fixture_path.relative_to(FIXTURE_ROOT.resolve())
        verifier_bytes = fixture_path.read_bytes()
        self.assertEqual(sha256(verifier_bytes), metadata["sha256"], project)

        with tempfile.TemporaryDirectory(prefix="kent-11-scenario-") as temporary:
            root = Path(temporary).resolve()
            subprocess.run(
                ["git", "init", "--quiet", str(root)],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            verifier = root / str(metadata["verifier_path"])
            verifier.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fixture_path, verifier)
            verifier.chmod(0o755)

            gradle = root / "gradlew"
            gradle.write_text(FAKE_GRADLE, encoding="utf-8")
            gradle.chmod(0o755)

            private_tmp = root / ".fixture-tmp"
            private_tmp.mkdir(mode=0o700)
            private_tmp.chmod(0o700)
            environment = dict(os.environ)
            environment["TMPDIR"] = str(private_tmp)
            environment["PWD"] = str(root)

            command = [str(verifier)]
            if project == "AppsomeAndroid":
                # This is a standalone synthetic Git root, not a managed
                # worktree. A verifier-owned TMPDIR may nevertheless put its
                # physical path beneath /.kent/worktrees/. Model the logical
                # non-worktree PWD after Bash startup so the frozen verifier
                # selects the fixture's fake gradlew, not the managed agentw.
                command = [
                    "/bin/bash",
                    "-c",
                    'PWD=/synthetic-git-root; source "$1"',
                    "baseline-fixture",
                    str(verifier),
                ]

            payload = {
                "workspace_path": str(root),
                "review_context": (
                    "Behavior changed, but no mock-backed scenario was added "
                    "or updated."
                ),
            }
            result = subprocess.run(
                command,
                cwd=root,
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                env=environment,
                check=False,
                timeout=20,
            )
            self.assertLessEqual(len(result.stdout), 4096, project)
            self.assertLessEqual(len(result.stderr), 8192, project)
            self.assertEqual(result.returncode, 0, project)
            response = json.loads(result.stdout)
            self.assertEqual(response, {"transition": "passed"}, project)

            compile_log = root / "build/kent-workflow/compile-dev-debug.log"
            compile_output = ""
            if compile_log.exists():
                compile_output = compile_log.read_text(encoding="utf-8")
            captured = (
                f"project={project}\n"
                f"verifier_sha256={metadata['sha256']}\n"
                f"verifier_stdout={result.stdout.strip()}\n"
                f"verifier_stderr={result.stderr.strip()}\n"
                f"compile_output={compile_output.strip()}\n"
            )
            captured = captured.replace(str(root), "<synthetic-git-root>")
            observation: dict[str, object] = {
                "project": project,
                "repository": metadata["repository"],
                "baseline_revision": metadata["revision"],
                "verifier_path": metadata["verifier_path"],
                "verifier_git_blob": metadata["git_blob"],
                "verifier_sha256": metadata["sha256"],
                "verifier_mode": metadata["mode"],
                "scenario_delta_present": False,
                "compile_command": metadata["fake_compile_command"],
                "compile_result": "passed (synthetic stub)",
                "observed_transition": response["transition"],
                "expected_transition": manifest_expected_transition(),
                "finding": "baseline accepts missing scenario evidence",
            }
            return observation, captured

    def test_frozen_project_verifiers_accept_missing_scenario_update(self) -> None:
        manifest = self.load_manifest()
        observations = []
        log_sections = []
        for project, metadata in manifest["projects"].items():
            observation, log_section = self.run_baseline_verifier(project, metadata)
            observations.append(observation)
            log_sections.append(log_section)

        log_bytes = ("\n".join(log_sections)).encode("utf-8")
        report = {
            "schema": "mock-scenario-pre-edit-red-v1",
            "task_short_id": "KEN-11",
            "fixed_point": manifest["fixed_point"],
            "scenario_id": manifest["scenario_id"],
            "expected_transition": manifest["expected_transition"],
            "results": observations,
            "verdict": "RED",
            "log_path": "build/kent-workflow/KEN-11/pre-edit-red.log",
            "log_sha256": sha256(log_bytes),
        }
        report_bytes = (
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        write_deterministic_artifact(PRE_EDIT_LOG, log_bytes)
        write_deterministic_artifact(PRE_EDIT_REPORT, report_bytes)


class MockScenarioProductionFixtureTest(unittest.TestCase):
    def load_manifest(self) -> dict[str, object]:
        manifest = json.loads(
            PRODUCTION_FIXTURE_MANIFEST.read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["schema"], "mock-scenario-production-fixtures-v1")
        self.assertEqual(manifest["task_short_id"], "KEN-11")
        self.assertEqual(
            manifest["accepted_scope_sha256"],
            "de0b5a1f75164db70ef4510d2244019418dee68aefbc54058fadbb48e71bdb59",
        )
        self.assertEqual(set(manifest["projects"]), {"appsome", "puber"})
        return manifest

    def project_root(self, project: str) -> Path:
        return FIXTURE_ROOT / "production" / project

    def load_helper(self, project: str) -> object:
        helper_path = self.project_root(project) / (
            ".kent/scripts/workflow-mock-scenario-evidence"
        )
        module_name = f"ken11_{project}_mock_scenario_helper"
        loader = SourceFileLoader(module_name, str(helper_path))
        spec = importlib.util.spec_from_loader(module_name, loader)
        self.assertIsNotNone(spec, helper_path)
        module = importlib.util.module_from_spec(spec)
        old_bytecode_setting = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = old_bytecode_setting
        return module

    def run_project_cases(
        self,
        project: str,
        names: tuple[str, ...] | None,
        *,
        project_root: Path | None = None,
        exclude_names: tuple[str, ...] = (),
    ) -> tuple[int, str, object]:
        root = project_root or self.project_root(project)
        suite_path = root / (
            "tools/test-workflow-mock-scenario-evidence"
        )
        old_bytecode_setting = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            module_name = f"ken11_{project}_mock_scenario_acceptance"
            loader = SourceFileLoader(module_name, str(suite_path))
            spec = importlib.util.spec_from_loader(module_name, loader)
            self.assertIsNotNone(spec, suite_path)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = old_bytecode_setting

        available = set(
            unittest.defaultTestLoader.getTestCaseNames(module.MockScenarioEvidenceTest)
        )
        if names is None:
            missing_exclusions = set(exclude_names) - available
            self.assertFalse(
                missing_exclusions,
                f"{project} fixture suite lacks exclusions: {sorted(missing_exclusions)}",
            )
            names = tuple(sorted(available - set(exclude_names)))
        missing = set(names) - available
        self.assertFalse(missing, f"{project} fixture suite lacks cases: {sorted(missing)}")
        suite = unittest.TestSuite(
            module.MockScenarioEvidenceTest(name) for name in names
        )
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        return result.testsRun, stream.getvalue(), result

    def test_production_fixture_inventory_binds_exact_source_bytes_and_modes(self) -> None:
        manifest = self.load_manifest()
        project_paths: dict[str, set[str]] = {}
        for project, metadata in manifest["projects"].items():
            with self.subTest(project=project):
                self.assertEqual(len(metadata["files"]), 15)
                paths = [item["path"] for item in metadata["files"]]
                self.assertEqual(paths, sorted(paths))
                self.assertEqual(len(paths), len(set(paths)))
                root = self.project_root(project)
                project_paths[project] = set(paths)
                for item in metadata["files"]:
                    path = root / item["path"]
                    self.assertTrue(path.is_file(), item["path"])
                    self.assertFalse(path.is_symlink(), item["path"])
                    self.assertEqual(sha256(path.read_bytes()), item["sha256"])
                    mode = (
                        "100755"
                        if path.stat().st_mode
                        & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                        else "100644"
                    )
                    self.assertEqual(item["mode"], mode, item["path"])
                actual_paths = {
                    path.relative_to(root).as_posix()
                    for path in root.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(actual_paths, set(paths))

        appsome = manifest["projects"]["appsome"]
        self.assertEqual(
            appsome["delivered_revision"],
            "397244248eb1ecd62fd40f412a7f233d2a589802",
        )
        self.assertEqual(
            appsome["observed_target_revision"],
            "532797e49a6b37f0da295d06150f3217297bee16",
        )
        self.assertEqual(
            appsome["source_delta_sha256"],
            "82a7db79992e4d538577ca923c4329cb0ea198278cb1414ed30b576bf1b92eb2",
        )
        puber = manifest["projects"]["puber"]
        self.assertEqual(
            puber["immutable_task_baseline"],
            "f640524f31696592f8532bda1162953601b9fa9f",
        )
        self.assertEqual(
            puber["delivered_revision"],
            "b755826c5d2cbfd73949d6d6fc41a7cba6bb6741",
        )
        self.assertEqual(
            puber["observed_target_revision"],
            "d51d489222e63c9ff215864b48b5022be99692ee",
        )
        self.assertEqual(
            puber["source_delta_sha256"],
            "335eacc0981008bc5716eaed98f2640a06ff73d7e0a46c6076fe82ece4805600",
        )
        self.assertEqual(
            puber["source_state"],
            "PR #91 and PR #92 merged to origin/master; current 15-file source bytes verified against the PR #92 tree",
        )
        project_paths["appsome"].remove(
            ".kent/workflows/appsome-release-publication-v22.manifest.json"
        )
        project_paths["puber"].remove(".kent/workflows/puber-release.manifest.json")
        self.assertEqual(project_paths["appsome"], project_paths["puber"])

    def test_project_helpers_and_policies_use_all_six_work_kinds(self) -> None:
        for project, enum_name in (
            ("appsome", "WORK_KINDS"),
            ("puber", "SUPPORTED_WORK_KINDS"),
        ):
            with self.subTest(project=project):
                helper = self.load_helper(project)
                policy = (
                    self.project_root(project)
                    / ".kent/commands/mock-scenario-policy.md"
                ).read_text(encoding="utf-8")
                self.assertEqual(set(getattr(helper, enum_name)), SUPPORTED_WORK_KINDS)
                for work_kind in SUPPORTED_WORK_KINDS:
                    self.assertIn(f"`{work_kind}`", policy)
                self.assertIn("narrative", policy.casefold())
                self.assertIn("failed", policy.casefold())
                self.assertIn("blocked", policy.casefold())

    def test_appsome_production_suite_covers_six_kinds_security_and_narrative(self) -> None:
        names = (
            "test_all_kinds_execution_failure_skip_and_missing_case",
            "test_all_kinds_applicable_and_missing_scenario_delta",
            "test_all_six_work_kinds_allow_concrete_non_applicability",
            "test_fifo_artifacts_fail_closed_without_waiting_for_a_writer",
            "test_multiline_narrative_is_retained_and_bounded",
        )
        count, output, result = self.run_project_cases("appsome", names)
        self.assertTrue(result.wasSuccessful(), output)
        self.assertEqual(len(names), count)

    def test_puber_production_suite_covers_cli_failure_narrative_and_races(self) -> None:
        names = (
            "test_cli_commands_round_trip_source_bound_evidence",
            "test_applicable_behavior_still_requires_changed_assertion_paths",
            "test_not_applicable_behavior_requires_receipted_alternative",
            "test_selected_failure_is_a_failure_not_a_pass",
            "test_selected_failure_cli_reaches_compile_as_failed",
            "test_passing_scenarios_still_run_successful_compile",
            "test_failure_evidence_invalid_stale_or_unavailable_still_blocks",
            "test_unchanged_old_junit_cannot_issue_receipt",
            "test_non_feature_work_kinds_enforce_policy_and_reentry",
            "test_source_delta_tracks_log_and_build_named_fixtures_and_stale_receipts",
            "test_source_delta_bounds_growth_during_read",
            "test_source_delta_enforces_remaining_aggregate_budget_during_read",
            "test_source_delta_rejects_file_and_parent_symlink_swaps",
            "test_read_file_rejects_symlink_replaced_before_open",
            "test_read_file_bounds_content_growth_after_open",
        )
        count, output, result = self.run_project_cases("puber", names)
        self.assertTrue(result.wasSuccessful(), output)
        self.assertEqual(len(names), count)

    def test_puber_fixture_cases_run_clean_without_ignored_build_outputs(self) -> None:
        manifest = self.load_manifest()
        # These three cases require the full Puber Git tree and release builder;
        # they are covered by the native Puber acceptance run, not the exact
        # 15-file production-delta fixture.
        full_repository_cases = (
            "test_manifest_binds_final_task_sources",
            "test_manifest_mismatched_bindings_reject_in_builder",
            "test_manifest_missing_task_files_reject_in_builder",
        )
        with tempfile.TemporaryDirectory(prefix="ken11-puber-clean-fixture-") as directory:
            project_root = Path(directory).resolve()
            for item in manifest["projects"]["puber"]["files"]:
                source = self.project_root("puber") / item["path"]
                destination = project_root / item["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                destination.chmod(0o755 if item["mode"] == "100755" else 0o644)
            self.assertFalse(
                (project_root / "build/kent-workflow/PUB-84/pre-edit-red.json").exists()
            )
            self.assertFalse(
                (project_root / "build/kent-workflow/PUB-84/pre-edit-red.log").exists()
            )
            count, output, result = self.run_project_cases(
                "puber",
                None,
                project_root=project_root,
                exclude_names=full_repository_cases,
            )
            self.assertTrue(result.wasSuccessful(), output)
            self.assertEqual(count, 33)

    def test_release_manifests_bind_scenario_policy_helpers_and_suites(self) -> None:
        manifest = self.load_manifest()
        manifest_paths = {
            "appsome": ".kent/workflows/appsome-release-publication-v22.manifest.json",
            "puber": ".kent/workflows/puber-release.manifest.json",
        }
        common_bindings = (
            ".kent/commands/mock-scenario-policy.md",
            ".kent/scripts/workflow-mock-scenario-evidence",
            "tools/test-workflow-mock-scenario-evidence",
        )
        puber_only_bindings = (
            ".kent/project-contract.md",
            ".kent/scripts/workflow-compile-verify",
            ".kent/scripts/tests/test-workflow-verify-report",
        )
        for project in ("appsome", "puber"):
            with self.subTest(project=project):
                root = self.project_root(project)
                project_manifest = json.loads(
                    (root / manifest_paths[project]).read_text(encoding="utf-8")
                )
                additional_paths = set(project_manifest["additional_paths"])
                if project == "puber":
                    readme_bindings = [
                        item["key"]
                        for item in project_manifest["external_roots"]
                        if item["kind"] == "source-sha256"
                        and item["key"].startswith(".kent/workflows/README.md=")
                    ]
                    self.assertEqual(
                        readme_bindings,
                        [
                            ".kent/workflows/README.md="
                            "0dfa155161e5096cea91a966713babe8ae69cee7165f2a13adfa8073b27cd95d"
                        ],
                    )
                for path in common_bindings:
                    self.assertIn(path, additional_paths)
                bindings: dict[str, str] = {}
                for item in project_manifest["external_roots"]:
                    if item["kind"] != "source-sha256":
                        continue
                    path, raw_digest = item["key"].split("=", 1)
                    bindings[path] = raw_digest.removeprefix("sha256:").replace(":", "")
                expected_files = {
                    item["path"]: item["sha256"]
                    for item in manifest["projects"][project]["files"]
                }
                required_bindings = (
                    common_bindings + puber_only_bindings
                    if project == "puber"
                    else common_bindings
                )
                for path in required_bindings:
                    self.assertEqual(
                        expected_files[path],
                        bindings.get(path),
                        f"missing or mismatched manifest source digest: {path}",
                    )

    def test_production_source_delta_rejects_symlink_swap(self) -> None:
        for project in ("appsome", "puber"):
            with self.subTest(project=project):
                helper = self.load_helper(project)
                with tempfile.TemporaryDirectory(
                    prefix=f"ken11-{project}-source-race-"
                ) as directory:
                    root = (Path(directory) / "workspace").resolve()
                    root.mkdir()
                    source = root / "src/changed.json"
                    source.parent.mkdir()
                    source.write_text('{"value":"baseline"}\n', encoding="utf-8")
                    for arguments in (
                        ["git", "init", "--quiet", str(root)],
                        ["git", "-C", str(root), "config", "user.name", "KEN-11 fixture"],
                        [
                            "git",
                            "-C",
                            str(root),
                            "config",
                            "user.email",
                            "ken-11@example.invalid",
                        ],
                        ["git", "-C", str(root), "add", "src/changed.json"],
                        [
                            "git",
                            "-C",
                            str(root),
                            "commit",
                            "--quiet",
                            "-m",
                            "synthetic source baseline",
                        ],
                    ):
                        subprocess.run(
                            arguments,
                            check=True,
                            capture_output=True,
                            timeout=10,
                        )
                    baseline = subprocess.run(
                        ["git", "-C", str(root), "rev-parse", "HEAD"],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    ).stdout.strip()
                    source.write_text('{"value":"changed"}\n', encoding="utf-8")
                    outside = Path(directory) / "outside-benign-fixture.json"
                    outside.write_text(
                        '{"value":"outside benign fixture"}\n', encoding="utf-8"
                    )

                    original_open = os.open
                    swapped = False
                    descriptor_paths: list[str] = []

                    def replace_before_open(
                        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                        flags: int,
                        mode: int = 0o777,
                        *,
                        dir_fd: int | None = None,
                    ) -> int:
                        nonlocal swapped
                        if dir_fd is not None:
                            descriptor_paths.append(os.fspath(path))
                        if (
                            dir_fd is not None
                            and os.fspath(path) == source.name
                            and not swapped
                        ):
                            source.unlink()
                            source.symlink_to(outside)
                            swapped = True
                        if dir_fd is None:
                            return original_open(path, flags, mode)
                        return original_open(path, flags, mode, dir_fd=dir_fd)

                    rejected = False
                    with mock.patch.object(os, "open", new=replace_before_open):
                        try:
                            helper.source_delta(root, baseline)
                        except (helper.EvidenceError, OSError):
                            rejected = True
                    self.assertTrue(
                        swapped,
                        "source digest skipped descriptor-relative reads: "
                        f"{descriptor_paths!r}",
                    )
                    self.assertTrue(
                        rejected,
                        "source digest followed a regular-file-to-symlink swap",
                    )


def manifest_expected_transition() -> str:
    manifest = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    return str(manifest["expected_transition"])


if __name__ == "__main__":
    unittest.main()
