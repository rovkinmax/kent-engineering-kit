from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "scripts" / "check-release-source-bindings"
PUBER_README = (
    Path(__file__).parent
    / "fixtures"
    / "source-bindings"
    / "puber-workflow-readme.md"
)

# This is the exact workflow README blob from the delivered Puber source that
# exhibits the historical stale source binding.
PUBER_SOURCE_COMMIT = "d51d489222e63c9ff215864b48b5022be99692ee"
PUBER_README_BLOB = "718da920e0ca4cc5d35cbcb0de3cf1a4ed08a84a"
PUBER_README_EXPECTED_SHA256 = (
    "322e3948b172dc8b5bda36e8cf5facc6d5b8df0a7d60d8010703014ed1210f52"
)
PUBER_README_ACTUAL_SHA256 = (
    "0dfa155161e5096cea91a966713babe8ae69cee7165f2a13adfa8073b27cd95d"
)
PUBER_BUILDER_PATH = ".kent/workflows/builders/puber_release.py"
PUBER_README_PATH = ".kent/workflows/README.md"


class HistoricalPuberSourceBindingTest(unittest.TestCase):
    def git(self, root: Path, *args: str) -> str:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": os.devnull,
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_NO_REPLACE_OBJECTS": "1",
            }
        )
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        return result.stdout.strip()

    def write(self, root: Path, relative_path: str, content: bytes) -> None:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def test_historical_puber_stale_readme_pin_is_rejected(self) -> None:
        readme = PUBER_README.read_bytes()
        self.assertEqual(
            hashlib.sha256(readme).hexdigest(),
            PUBER_README_ACTUAL_SHA256,
        )
        git_blob = hashlib.sha1(
            b"blob " + str(len(readme)).encode() + b"\0" + readme
        ).hexdigest()
        self.assertEqual(
            git_blob,
            PUBER_README_BLOB,
            f"fixture provenance: {PUBER_SOURCE_COMMIT}:{PUBER_README_PATH}",
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.git(root, "init", "-q")

            builder = b"historical Puber release-builder fixture\n"
            builder_digest = hashlib.sha256(builder).hexdigest()
            self.write(
                root,
                ".kent/workflow-profile.toml",
                (
                    'schema_version = 4\n'
                    'project_name = "Puber"\n'
                    '[release]\n'
                    'topology_kind = "puber-release"\n'
                    'adoption_mode = "managed-in-place"\n'
                    'spec_path = ".kent/workflows/specs/puber-release.toml"\n'
                    f'builder_path = "{PUBER_BUILDER_PATH}"\n'
                ).encode(),
            )
            self.write(
                root,
                ".kent/workflows/specs/puber-release.toml",
                (
                    'schema_version = 2\n'
                    'spec_kind = "release"\n'
                    'topology_kind = "puber-release"\n'
                    'adoption_mode = "managed-in-place"\n'
                    'project_name = "Puber"\n'
                    'repository = "rovkinmax/Puber"\n'
                    'runtime_attested = false\n'
                    '[source_manifest]\n'
                    'schema = "release_source_manifest_v1"\n'
                    'path = ".kent/workflows/puber-release.manifest.json"\n'
                    'revision_binding = "runtime-source-envelope"\n'
                    'runtime_attested = false\n'
                ).encode(),
            )
            manifest = {
                "schema": "release_source_manifest_v1",
                "project_name": "Puber",
                "repository": "rovkinmax/Puber",
                "topology_kind": "puber-release",
                "external_roots": [
                    {
                        "kind": "builder-sha256",
                        "key": builder_digest,
                        "runtime_digest_required": True,
                    },
                    {
                        "kind": "source-sha256",
                        "key": f"{PUBER_BUILDER_PATH}={builder_digest}",
                        "runtime_digest_required": True,
                    },
                    {
                        "kind": "source-sha256",
                        "key": (
                            f"{PUBER_README_PATH}="
                            f"{PUBER_README_EXPECTED_SHA256}"
                        ),
                        "runtime_digest_required": True,
                    },
                ],
                "runtime_attested": False,
            }
            self.write(
                root,
                ".kent/workflows/puber-release.manifest.json",
                (json.dumps(manifest, sort_keys=True) + "\n").encode(),
            )
            self.write(root, PUBER_BUILDER_PATH, builder)
            self.write(root, PUBER_README_PATH, readme)

            self.git(root, "add", "--all")
            self.git(
                root,
                "-c",
                "user.name=Source Binding Fixture",
                "-c",
                "user.email=source-binding-fixture@example.invalid",
                "commit",
                "-q",
                "-m",
                "Create historical Puber source-binding fixture",
            )
            selected_commit = self.git(root, "rev-parse", "HEAD")

            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            }
            environment.update(
                {
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "core.hooksPath",
                    "GIT_CONFIG_VALUE_0": os.devnull,
                    "GIT_NO_LAZY_FETCH": "1",
                    "GIT_NO_REPLACE_OBJECTS": "1",
                }
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(CHECKER),
                    "--project",
                    str(root),
                    "--ref",
                    selected_commit,
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            report = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, report)
            self.assertIn(PUBER_README_PATH, report)
            self.assertIn(PUBER_README_EXPECTED_SHA256, report)
            self.assertIn(PUBER_README_ACTUAL_SHA256, report)


class ReleaseSourceBindingsCheckerTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.git("init", "-q")

        self.builder_path = ".kent/workflows/builders/puber_release.py"
        self.readme_path = ".kent/workflows/README.md"
        self.spec_path = ".kent/workflows/specs/puber-release.toml"
        self.manifest_path = ".kent/workflows/puber-release.manifest.json"
        self.builder_marker = self.root / "builder-was-executed"
        self.builder = (
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            f"Path({str(self.builder_marker)!r}).write_text('executed')\n"
        ).encode()
        self.readme = b"Selected release workflow documentation.\n"
        self.builder_digest = hashlib.sha256(self.builder).hexdigest()
        self.readme_digest = hashlib.sha256(self.readme).hexdigest()

        self.write(
            ".gitignore",
            b"/.kent/runtime/\n",
        )
        self.write(
            ".kent/workflow-profile.toml",
            (
                'schema_version = 4\n'
                'project_name = "Puber"\n'
                '[release]\n'
                'topology_kind = "puber-release"\n'
                'adoption_mode = "managed-in-place"\n'
                f'spec_path = "{self.spec_path}"\n'
                f'builder_path = "{self.builder_path}"\n'
                'snapshot_path = ".kent/workflows/puber-release.json"\n'
            ).encode(),
        )
        self.write(
            self.spec_path,
            (
                'schema_version = 2\n'
                'spec_kind = "release"\n'
                'topology_kind = "puber-release"\n'
                'adoption_mode = "managed-in-place"\n'
                'project_name = "Puber"\n'
                'repository = "rovkinmax/Puber"\n'
                'runtime_attested = false\n'
                '[source_manifest]\n'
                'schema = "release_source_manifest_v1"\n'
                f'path = "{self.manifest_path}"\n'
                'revision_binding = "runtime-source-envelope"\n'
                'runtime_attested = false\n'
            ).encode(),
        )
        self.manifest = {
            "schema": "release_source_manifest_v1",
            "project_name": "Puber",
            "repository": "rovkinmax/Puber",
            "topology_kind": "puber-release",
            "external_roots": [
                {
                    "kind": "builder-sha256",
                    "key": self.builder_digest,
                    "runtime_digest_required": True,
                },
                {
                    "kind": "source-sha256",
                    "key": (
                        f"{self.builder_path}="
                        f"{self.group_digest(self.builder_digest)}"
                    ),
                    "runtime_digest_required": True,
                },
                {
                    "kind": "source-sha256",
                    "key": f"{self.readme_path}={self.readme_digest}",
                    "runtime_digest_required": True,
                },
            ],
            "runtime_attested": False,
        }
        self.write_manifest()
        self.write(self.builder_path, self.builder, mode=0o755)
        self.write(self.readme_path, self.readme)
        self.base_commit = self.commit("Create source-binding fixture")

    @staticmethod
    def git_environment() -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": os.devnull,
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_NO_REPLACE_OBJECTS": "1",
            }
        )
        return environment

    @staticmethod
    def group_digest(digest: str) -> str:
        return "sha256:" + ":".join(
            digest[offset : offset + 8] for offset in range(0, 64, 8)
        )

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.git_environment(),
        )
        self.assertEqual(
            result.returncode,
            0,
            result.stderr or result.stdout or f"git {' '.join(arguments)} failed",
        )
        return result.stdout.strip()

    def write(
        self,
        relative_path: str,
        content: bytes,
        *,
        mode: int | None = None,
    ) -> Path:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if mode is not None:
            path.chmod(mode)
        return path

    def write_manifest(self) -> None:
        self.write(
            self.manifest_path,
            (json.dumps(self.manifest, sort_keys=True) + "\n").encode(),
        )

    def commit(self, message: str) -> str:
        self.git("add", "--all")
        self.git(
            "-c",
            "user.name=Source Binding Fixture",
            "-c",
            "user.email=source-binding-fixture@example.invalid",
            "commit",
            "-q",
            "-m",
            message,
        )
        return self.git("rev-parse", "HEAD")

    def run_checker(
        self,
        ref: str | None,
        *,
        kind_map: str | None = None,
        project: Path | None = None,
        extra_environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(CHECKER),
            "--project",
            str(project or self.root),
        ]
        if ref is not None:
            command.extend(["--ref", ref])
        if kind_map is not None:
            command.extend(["--kind-map", kind_map])
        environment = self.git_environment()
        if extra_environment:
            environment.update(extra_environment)
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=35,
        )

    def assert_rejected(
        self,
        result: subprocess.CompletedProcess[str],
        expected_message: str,
    ) -> None:
        report = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, report)
        self.assertIn(expected_message, report)

    def test_cli_is_executable_and_help_needs_no_project_checkout(self) -> None:
        metadata = CHECKER.lstat()
        self.assertTrue(stat.S_ISREG(metadata.st_mode))
        self.assertFalse(CHECKER.is_symlink())
        self.assertTrue(os.access(CHECKER, os.X_OK))
        result = subprocess.run(
            [str(CHECKER), "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--project", result.stdout)
        self.assertIn("--ref", result.stdout)

    def test_cli_requires_an_explicit_full_object_id_and_exact_root(self) -> None:
        missing_ref = self.run_checker(None)
        self.assertEqual(missing_ref.returncode, 2)
        self.assertIn("--ref", missing_ref.stderr)

        symbolic_ref = self.run_checker("HEAD")
        self.assert_rejected(symbolic_ref, "full lowercase Git")

        subdirectory = self.run_checker(
            self.base_commit,
            project=self.root / ".kent",
        )
        self.assert_rejected(subdirectory, "exact repository root")

    def test_full_commit_and_tree_selectors_return_source_only_report(self) -> None:
        for selected_ref, expected_commit in (
            (self.base_commit, self.base_commit),
            (
                self.git("rev-parse", f"{self.base_commit}^{{tree}}"),
                None,
            ),
        ):
            with self.subTest(selected_ref=selected_ref):
                result = self.run_checker(selected_ref)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(
                    set(report),
                    {
                        "schema",
                        "requested_ref",
                        "tree_oid",
                        "commit_oid",
                        "checked_count",
                        "source_bindings_valid",
                    },
                )
                self.assertEqual(
                    report["schema"],
                    "release-source-bindings-report-v1",
                )
                self.assertEqual(report["requested_ref"], selected_ref)
                self.assertEqual(report["commit_oid"], expected_commit)
                self.assertEqual(report["checked_count"], 3)
                self.assertIs(report["source_bindings_valid"], True)
                self.assertNotIn("ready", report)
                self.assertNotIn("runtime_attested", report)
                self.assertNotIn("activation_authorized", report)

    def test_builtin_digest_codecs_and_equal_path_aliases_are_accepted(self) -> None:
        result = self.run_checker(self.base_commit)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["checked_count"], 3)
        # builder-sha256 and source-sha256 bind the same Git path using
        # different accepted encodings of the same expected digest.
        builder_key = next(
            row["key"]
            for row in self.manifest["external_roots"]
            if row["kind"] == "builder-sha256"
        )
        source_alias_key = next(
            row["key"]
            for row in self.manifest["external_roots"]
            if row["kind"] == "source-sha256"
            and row["key"].startswith(f"{self.builder_path}=")
        )
        self.assertEqual(builder_key, self.builder_digest)
        self.assertEqual(
            source_alias_key,
            f"{self.builder_path}={self.group_digest(self.builder_digest)}",
        )
        self.assertNotEqual(builder_key, source_alias_key)

    def test_mapped_sdk_policy_kind_reads_mapping_from_selected_tree(self) -> None:
        policy_path = (
            ".kent/workflows/specs/"
            "osome-sdk-publication-principal-policy.json"
        )
        policy = b'{"policy":"selected source fixture"}\n'
        policy_digest = hashlib.sha256(policy).hexdigest()
        self.write(policy_path, policy)
        self.manifest["external_roots"].append(
            {
                "kind": "principal-policy-sha256",
                "key": policy_digest,
                "runtime_digest_required": True,
            }
        )
        map_path = ".kent/workflows/source-binding-kinds.json"
        kind_map = {
            "schema": "release-source-binding-kind-map-v1",
            "paths": {"principal-policy-sha256": policy_path},
        }
        self.write(
            map_path,
            (json.dumps(kind_map, sort_keys=True) + "\n").encode(),
        )
        self.write_manifest()
        selected_commit = self.commit("Add SDK principal-policy fixture")

        without_map = self.run_checker(selected_commit)
        self.assert_rejected(without_map, "unmapped kind")

        # Dirty bytes do not replace the selected map blob.
        self.write(map_path, b"not valid JSON in the working tree\n")
        with_selected_map = self.run_checker(selected_commit, kind_map=map_path)
        self.assertEqual(with_selected_map.returncode, 0, with_selected_map.stderr)
        self.assertEqual(json.loads(with_selected_map.stdout)["checked_count"], 4)

    def test_unused_kind_map_entry_is_rejected(self) -> None:
        map_path = ".kent/workflows/source-binding-kinds.json"
        self.write(
            map_path,
            (
                json.dumps(
                    {
                        "schema": "release-source-binding-kind-map-v1",
                        "paths": {"unused-policy-sha256": "policy.json"},
                    }
                )
                + "\n"
            ).encode(),
        )
        selected_commit = self.commit("Add unused source-kind mapping")
        result = self.run_checker(selected_commit, kind_map=map_path)
        self.assert_rejected(result, "unused custom kind")

    def test_kind_map_cannot_override_builtin_kinds(self) -> None:
        map_path = ".kent/workflows/source-binding-kinds.json"
        self.write(
            map_path,
            (
                json.dumps(
                    {
                        "schema": "release-source-binding-kind-map-v1",
                        "paths": {"source-sha256": self.readme_path},
                    }
                )
                + "\n"
            ).encode(),
        )
        selected_commit = self.commit("Add prohibited builtin mapping")
        result = self.run_checker(selected_commit, kind_map=map_path)
        self.assert_rejected(result, "cannot override built-in")

    def test_duplicate_json_toml_and_kind_key_declarations_fail(self) -> None:
        with self.subTest(kind="duplicate JSON key"):
            raw = (json.dumps(self.manifest, sort_keys=True) + "\n").replace(
                '"schema": "release_source_manifest_v1",',
                (
                    '"schema": "release_source_manifest_v1", '
                    '"schema": "release_source_manifest_v1",'
                ),
                1,
            )
            self.write(self.manifest_path, raw.encode())
            commit = self.commit("Add duplicate manifest key")
            self.assert_rejected(
                self.run_checker(commit),
                "duplicate JSON key",
            )

        with self.subTest(kind="duplicate TOML key"):
            self.write(
                ".kent/workflow-profile.toml",
                (
                    'schema_version = 4\n'
                    'schema_version = 4\n'
                    'project_name = "Puber"\n'
                    '[release]\n'
                    'topology_kind = "puber-release"\n'
                    'adoption_mode = "managed-in-place"\n'
                    f'spec_path = "{self.spec_path}"\n'
                    f'builder_path = "{self.builder_path}"\n'
                ).encode(),
            )
            commit = self.commit("Add duplicate profile key")
            self.assert_rejected(
                self.run_checker(commit),
                "valid UTF-8 TOML",
            )

        with self.subTest(kind="duplicate descriptor"):
            self.write(
                ".kent/workflow-profile.toml",
                (
                    'schema_version = 4\n'
                    'project_name = "Puber"\n'
                    '[release]\n'
                    'topology_kind = "puber-release"\n'
                    'adoption_mode = "managed-in-place"\n'
                    f'spec_path = "{self.spec_path}"\n'
                    f'builder_path = "{self.builder_path}"\n'
                    'snapshot_path = ".kent/workflows/puber-release.json"\n'
                ).encode(),
            )
            self.manifest["external_roots"].append(
                dict(self.manifest["external_roots"][0])
            )
            self.write_manifest()
            commit = self.commit("Add duplicate source descriptor")
            self.assert_rejected(
                self.run_checker(commit),
                "duplicates kind/key",
            )

    def test_unsupported_profile_and_release_spec_versions_fail(self) -> None:
        profile = (
            (self.root / ".kent/workflow-profile.toml")
            .read_text(encoding="utf-8")
            .replace("schema_version = 4", "schema_version = 3", 1)
        )
        self.write(".kent/workflow-profile.toml", profile.encode())
        profile_commit = self.commit("Use unsupported project profile schema")
        self.assert_rejected(
            self.run_checker(profile_commit),
            "profile must use schema_version 4",
        )

        spec = (
            (self.root / self.spec_path)
            .read_text(encoding="utf-8")
            .replace("schema_version = 2", "schema_version = 1", 1)
        )
        self.write(
            ".kent/workflow-profile.toml",
            (
                'schema_version = 4\n'
                'project_name = "Puber"\n'
                '[release]\n'
                'topology_kind = "puber-release"\n'
                'adoption_mode = "managed-in-place"\n'
                f'spec_path = "{self.spec_path}"\n'
                f'builder_path = "{self.builder_path}"\n'
                'snapshot_path = ".kent/workflows/puber-release.json"\n'
            ).encode(),
        )
        self.write(self.spec_path, spec.encode())
        spec_commit = self.commit("Use unsupported release spec schema")
        self.assert_rejected(
            self.run_checker(spec_commit),
            "release spec must use schema_version 2 or 3",
        )

    def test_unsupported_descriptor_fields_and_false_runtime_digest_fail(self) -> None:
        descriptor = self.manifest["external_roots"][0]
        descriptor["unexpected"] = "must not be ignored"
        self.write_manifest()
        extra_field_commit = self.commit("Add unsupported descriptor field")
        self.assert_rejected(
            self.run_checker(extra_field_commit),
            "unsupported or missing fields",
        )

        del descriptor["unexpected"]
        descriptor["runtime_digest_required"] = False
        self.write_manifest()
        false_runtime_commit = self.commit("Disable runtime digest requirement")
        self.assert_rejected(
            self.run_checker(false_runtime_commit),
            "runtime_digest_required must be true",
        )

    def test_unknown_custom_kind_malformed_digest_and_conflicting_alias_fail(self) -> None:
        self.manifest["external_roots"].append(
            {
                "kind": "principal-policy-sha256",
                "key": "0" * 64,
                "runtime_digest_required": True,
            }
        )
        self.write_manifest()
        unknown_kind_commit = self.commit("Add unmapped custom source kind")
        self.assert_rejected(
            self.run_checker(unknown_kind_commit),
            "unmapped kind",
        )

        self.manifest["external_roots"].pop()
        self.manifest["external_roots"][2]["key"] = (
            f"{self.readme_path}=" + ("A" * 64)
        )
        self.write_manifest()
        malformed_digest_commit = self.commit("Add uppercase digest")
        self.assert_rejected(
            self.run_checker(malformed_digest_commit),
            "lowercase hex64",
        )

        self.manifest["external_roots"][2]["key"] = (
            f"{self.readme_path}={self.readme_digest}"
        )
        self.manifest["external_roots"][1]["key"] = (
            f"{self.builder_path}=" + ("0" * 64)
        )
        self.write_manifest()
        conflict_commit = self.commit("Add conflicting source alias")
        self.assert_rejected(
            self.run_checker(conflict_commit),
            "conflicting expected digests",
        )

        self.manifest["external_roots"][1]["key"] = (
            f"{self.builder_path}={self.group_digest(self.builder_digest)}"
        )
        self.manifest["external_roots"][2]["key"] = (
            f"../escaped.md={self.readme_digest}"
        )
        self.write_manifest()
        escaped_path_commit = self.commit("Add an escaping repository path")
        self.assert_rejected(
            self.run_checker(escaped_path_commit),
            "not a normalized repository-relative path",
        )

    def test_missing_symlink_tree_and_gitlink_sources_are_rejected(self) -> None:
        with self.subTest(kind="missing blob"):
            self.manifest["external_roots"][2]["key"] = (
                f"missing/source.md={self.readme_digest}"
            )
            self.write_manifest()
            missing_commit = self.commit("Bind a missing source path")
            self.assert_rejected(
                self.run_checker(missing_commit),
                "selected Git tree is missing path",
            )

        with self.subTest(kind="symlink"):
            self.manifest["external_roots"][2]["key"] = (
                f"link-to-readme={self.readme_digest}"
            )
            self.write_manifest()
            (self.root / "link-to-readme").symlink_to(self.readme_path)
            symlink_commit = self.commit("Bind a symlink")
            self.assert_rejected(
                self.run_checker(symlink_commit),
                "mode=120000 type=blob",
            )

        with self.subTest(kind="tree"):
            self.manifest["external_roots"][2]["key"] = (
                f"tree-source={self.readme_digest}"
            )
            self.write_manifest()
            self.write("tree-source/child.txt", b"child\n")
            tree_commit = self.commit("Bind a tree instead of a file")
            self.assert_rejected(
                self.run_checker(tree_commit),
                "type=tree",
            )

        with self.subTest(kind="gitlink"):
            self.manifest["external_roots"][2]["key"] = (
                f"submodules/fake={self.readme_digest}"
            )
            self.write_manifest()
            self.git("add", "--all")
            self.git(
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{self.base_commit},submodules/fake",
            )
            self.git(
                "-c",
                "user.name=Source Binding Fixture",
                "-c",
                "user.email=source-binding-fixture@example.invalid",
                "commit",
                "-q",
                "-m",
                "Bind a Git submodule entry",
            )
            gitlink_commit = self.git("rev-parse", "HEAD")
            self.assert_rejected(
                self.run_checker(gitlink_commit),
                "mode=160000 type=commit",
            )

    def test_every_path_mode_is_checked_before_same_blob_cache_reuse(self) -> None:
        payload = b"target\n"
        digest = hashlib.sha256(payload).hexdigest()
        self.write("regular-target", payload)
        self.write_manifest()
        self.manifest["external_roots"].extend(
            [
                {
                    "kind": "source-sha256",
                    "key": f"regular-target={digest}",
                    "runtime_digest_required": True,
                },
                {
                    "kind": "source-sha256",
                    "key": f"symlink-alias={digest}",
                    "runtime_digest_required": True,
                },
            ]
        )
        self.write_manifest()
        (self.root / "symlink-alias").symlink_to("target")
        commit = self.commit("Add regular and symlink paths with identical blobs")
        result = self.run_checker(commit)
        self.assert_rejected(result, "mode=120000 type=blob")

    def test_conflicting_project_identity_fails_before_source_comparison(self) -> None:
        spec = (
            (self.root / self.spec_path)
            .read_text(encoding="utf-8")
            .replace('project_name = "Puber"', 'project_name = "Other"', 1)
        )
        self.write(self.spec_path, spec.encode())
        commit = self.commit("Change release spec identity")
        self.assert_rejected(
            self.run_checker(commit),
            "project_name differ",
        )

    def test_selected_commit_is_independent_of_dirty_files_index_and_runtime(self) -> None:
        marker = self.builder_marker
        executable_builder = (
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed again')\n"
        ).encode()
        executable_digest = hashlib.sha256(executable_builder).hexdigest()
        self.manifest["external_roots"][0]["key"] = executable_digest
        self.manifest["external_roots"][1]["key"] = (
            f"{self.builder_path}={self.group_digest(executable_digest)}"
        )
        self.write_manifest()
        self.write(self.builder_path, executable_builder, mode=0o755)
        selected_commit = self.commit("Add inert executable builder bytes")

        self.git("remote", "add", "origin", str(self.root / "absent-remote.git"))
        dirty_readme = b"uncommitted working-tree bytes\n"
        self.write(self.readme_path, dirty_readme)
        staged = self.write("staged-unrelated.txt", b"staged but unselected\n")
        self.git("add", "staged-unrelated.txt")
        runtime_file = self.write(
            ".kent/runtime/local-receipt.json",
            b'{"preserve":"unchanged"}\n',
        )
        index_file = self.root / ".git" / "index"
        index_before = hashlib.sha256(index_file.read_bytes()).digest()
        runtime_before = runtime_file.read_bytes()

        result = self.run_checker(
            selected_commit,
            extra_environment={
                "GIT_DIR": str(self.root / ".git"),
                "GIT_WORK_TREE": str(self.root / "wrong-worktree"),
                "GIT_INDEX_FILE": str(self.root / "wrong-index"),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual((self.root / self.readme_path).read_bytes(), dirty_readme)
        self.assertEqual(
            hashlib.sha256(index_file.read_bytes()).digest(),
            index_before,
        )
        self.assertEqual(runtime_file.read_bytes(), runtime_before)
        self.assertEqual(staged.read_bytes(), b"staged but unselected\n")
        self.assertFalse((self.root / ".git" / "FETCH_HEAD").exists())

        changed_readme = b"new selected integration-tree bytes\n"
        self.write(self.readme_path, changed_readme)
        integration_commit = self.commit("Create changed integration tree")
        mismatch = self.run_checker(integration_commit)
        self.assert_rejected(mismatch, "source binding mismatch")
        self.assertIn(
            hashlib.sha256(changed_readme).hexdigest(),
            mismatch.stderr,
        )
        self.assertFalse(marker.exists())

    def test_descriptor_count_and_blob_read_limits_fail_closed(self) -> None:
        self.manifest["external_roots"] = [
            {
                "kind": "source-sha256",
                "key": f"missing/{index}.txt={'0' * 64}",
                "runtime_digest_required": True,
            }
            for index in range(4097)
        ]
        self.write_manifest()
        too_many_commit = self.commit("Exceed source descriptor count")
        self.assert_rejected(
            self.run_checker(too_many_commit),
            "4096-descriptor limit",
        )

    def test_git_output_cap_kills_a_chatty_plumbing_process(self) -> None:
        fake_bin = self.root / "fake-bin"
        fake_git = fake_bin / "git"
        fake_bin.mkdir()
        fake_git.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "chunk = b'x' * 65536\n"
            "while True:\n"
            "    sys.stdout.buffer.write(chunk)\n"
            "    sys.stdout.buffer.flush()\n",
            encoding="utf-8",
        )
        fake_git.chmod(0o755)
        environment = self.git_environment()
        environment["PATH"] = (
            str(fake_bin) + os.pathsep + environment.get("PATH", "")
        )
        result = subprocess.run(
            [
                sys.executable,
                str(CHECKER),
                "--project",
                str(self.root),
                "--ref",
                self.base_commit,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=8,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Git command output exceeded the 8 MiB limit", result.stderr)

    def test_individual_and_cumulative_blob_read_limits_fail_closed(self) -> None:
        single_path = "large/oversized-source.bin"
        oversized = b"x" * (8 * 1024 * 1024)
        self.write(single_path, oversized)
        self.manifest["external_roots"].append(
            {
                "kind": "source-sha256",
                "key": f"{single_path}={hashlib.sha256(oversized).hexdigest()}",
                "runtime_digest_required": True,
            }
        )
        self.write_manifest()
        oversized_commit = self.commit("Add source above command output limit")
        self.assert_rejected(
            self.run_checker(oversized_commit),
            "exceeds the 8 MiB command-output limit",
        )

        self.manifest["external_roots"] = self.manifest["external_roots"][:3]
        self.write_manifest()
        for index in range(5):
            path = f"large/cumulative-{index}.bin"
            content = bytes([65 + index]) * (7 * 1024 * 1024)
            self.write(path, content)
            self.manifest["external_roots"].append(
                {
                    "kind": "source-sha256",
                    "key": f"{path}={hashlib.sha256(content).hexdigest()}",
                    "runtime_digest_required": True,
                }
            )
            del content
        self.write_manifest()
        cumulative_commit = self.commit("Exceed cumulative selected blob limit")
        self.assert_rejected(
            self.run_checker(cumulative_commit),
            "32 MiB total limit",
        )

    def test_consumer_ci_fixture_covers_existing_documentation_pr_jobs(self) -> None:
        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "source-bindings"
            / "consumer-ci.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(
            fixture["schema"],
            "release-source-binding-consumer-ci-v1",
        )
        self.assertTrue(fixture["existing_jobs_only"])
        checker_step = fixture["checker_step"]
        self.assertEqual(
            checker_step["name"],
            "Validate release source bindings",
        )
        self.assertEqual(
            checker_step["position"],
            "immediately-after-checkout",
        )
        projects = {
            item["project"]: item for item in fixture["consumers"]
        }
        self.assertEqual(
            set(projects),
            {
                "AppsomeAndroid",
                "Puber",
                "OsomeAPI-SDK-generator",
                "osome-slack-reader",
            },
        )
        expected_jobs = {
            "AppsomeAndroid": (
                "OsomePteLtd/AppsomeAndroid",
                ".github/workflows/build_stage_apk.yml",
                "detekt",
                "release/4.31.0",
            ),
            "Puber": (
                "rovkinmax/Puber",
                ".github/workflows/pr-checks.yml",
                "detekt",
                "master",
            ),
            "OsomeAPI-SDK-generator": (
                "OsomePteLtd/kmp-mobile-shared",
                ".github/workflows/check-pr.yml",
                "check",
                "main",
            ),
            "osome-slack-reader": (
                "OsomePteLtd/osome-slack-reader",
                ".github/workflows/ci.yml",
                "quality",
                "main",
            ),
        }
        expected_pull_requests = {
            "AppsomeAndroid": {
                "branches": ["master", "release/*"],
                "types": ["opened", "synchronize"],
            },
            "Puber": {"branches": ["master"], "types": None},
            "OsomeAPI-SDK-generator": {
                "branches": ["main"],
                "types": ["opened", "synchronize", "reopened"],
            },
            "osome-slack-reader": {"branches": None, "types": None},
        }
        expected_other_events = {
            "AppsomeAndroid": [
                {"event": "deployment"},
                {"event": "push", "branches": ["master", "release/*"]},
                {"event": "workflow_dispatch"},
            ],
            "Puber": [],
            "OsomeAPI-SDK-generator": [],
            "osome-slack-reader": [],
        }
        expected_checkout_actions = {
            "AppsomeAndroid": (
                "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"
            ),
            "Puber": "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
            "OsomeAPI-SDK-generator": (
                "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"
            ),
            "osome-slack-reader": (
                "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
            ),
        }
        expected_target_commits = {
            "AppsomeAndroid": "532797e49a6b37f0da295d06150f3217297bee16",
            "Puber": "d51d489222e63c9ff215864b48b5022be99692ee",
            "OsomeAPI-SDK-generator": (
                "3d9206a4c2d297f194fb99785eab65c821a3a0d6"
            ),
            "osome-slack-reader": "1a0bfd7c0d7bbfd54885515b27b8d286570b705a",
        }
        for project, item in projects.items():
            repository, workflow_path, job, target = expected_jobs[project]
            with self.subTest(project=project):
                self.assertEqual(item["repository"], repository)
                self.assertEqual(item["workflow_path"], workflow_path)
                self.assertEqual(item["job"], job)
                self.assertEqual(item["target_branch"], target)
                self.assertEqual(
                    item["target_commit"],
                    expected_target_commits[project],
                )
                self.assertEqual(item["path_filters"], False)
                self.assertEqual(
                    item["pull_request"],
                    expected_pull_requests[project],
                )
                self.assertEqual(
                    item["checkout_action"],
                    expected_checkout_actions[project],
                )
                self.assertEqual(
                    item["other_events"],
                    expected_other_events[project],
                )
                self.assertNotIn("paths", item["pull_request"])
                self.assertNotIn("paths_ignore", item["pull_request"])
                self.assertEqual(
                    item.get("run", checker_step["run"]),
                    checker_step["run"]
                    + (
                        " --kind-map .kent/workflows/source-binding-kinds.json"
                        if item["kind_map"]
                        else ""
                    ),
                )


if __name__ == "__main__":
    unittest.main()
