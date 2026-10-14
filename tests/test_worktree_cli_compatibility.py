"""Portable compatibility checks for Kent worktree CLI JSON.

Set KENT_WORKTREE_JANITOR_PATH to qualify another consumer's Janitor bytes:

    KENT_WORKTREE_JANITOR_PATH=/path/to/workflow-task-janitor \
      python3 -m unittest discover -s tests -p test_worktree_cli_compatibility.py

The Kent 2.9 fixtures below are source-derived schema examples, not live
Kent-runtime evidence. Their wire names and emitted fields come from the
immutable CLI projector at commit 8497311f451dee6a23039bc2c00311eadf387688,
cli/kent/worktree_projection.go (Git blob
4db3eb43807c03554765012d6175c1d23e84601f; SHA-256
7926ae7ff399665603f1a40eb2c06c534dd85090c315871f745d7e05a34694fe).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
JANITOR_ENV = "KENT_WORKTREE_JANITOR_PATH"
JANITOR_PATH = Path(os.environ.get(
    JANITOR_ENV,
    str(REPO_ROOT / "templates" / "project" / "workflow-task-janitor"),
))


def load_janitor(path: Path = JANITOR_PATH):
    if path.is_symlink() or not path.is_file():
        raise AssertionError(f"Janitor must be a regular file: {path}")
    loader = importlib.machinery.SourceFileLoader(
        "portable_worktree_janitor", str(path),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load Janitor: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cli29_target(primary: Path, effective: Path) -> dict[str, object]:
    primary = primary.resolve()
    effective = effective.resolve()
    target: dict[str, object] = {
        "workspace_id": "workspace-id",
        "workspace_name": "main",
        "workspace_root": str(primary),
        "workspace_availability": "available",
        "cwd_relpath": "",
        "effective_workdir": str(effective),
    }
    if effective != primary:
        target["worktree"] = {
            "id": "worktree-id",
            "name": "TASK-1",
            "root": str(effective),
            "availability": "available",
        }
    return target


def cli29_status(
    primary: Path,
    effective: Path,
    *,
    recorded: Path | None = None,
    observed: Path | None = None,
):
    effective = effective.resolve()
    worktree: dict[str, object] = {
        "recorded_root": str((recorded or effective).resolve()),
        "display_name": "TASK-1",
        "recorded_branch_ref": "refs/heads/TASK-1",
    }
    if observed is not None:
        worktree["observed_root"] = str(observed.resolve())
        worktree["observed_branch_ref"] = "refs/heads/TASK-1"
    return {
        "target": cli29_target(primary, effective),
        "worktree": worktree,
        "problems": [],
    }


def cli29_git_facts(
    root: Path,
    *,
    is_main: bool,
    branch: str | None = None,
) -> dict[str, object]:
    facts: dict[str, object] = {
        "canonical_root": str(root.resolve()),
        "head_object": "0123456789abcdef",
        "detached": False,
        "bare": False,
        "is_main": is_main,
        "path_available": True,
    }
    if branch is not None:
        facts["branch_ref"] = f"refs/heads/{branch}"
        facts["branch_name"] = branch
    return facts


def cli29_kent_facts(root: Path, *, worktree_id: str) -> dict[str, object]:
    return {
        "worktree_id": worktree_id,
        "canonical_root": str(root.resolve()),
        "display_name": "TASK-1",
        "managed": True,
        "created_branch": True,
        "origin_session_id": "origin-session",
    }


def cli29_list_entry(variant: str, body: dict[str, object]) -> dict[str, object]:
    return {
        "topology": {"variant": variant, variant: body},
        "projection": {"selector": "TASK-1", "is_current": False},
    }


def cli29_worktree_list(primary: Path, workspace: Path, external: Path):
    primary = primary.resolve()
    workspace = workspace.resolve()
    external = external.resolve()
    missing_root = (primary / ".kent" / "worktrees" / "missing").resolve()
    return {
        "target": cli29_target(primary, primary),
        "worktrees": [
            cli29_list_entry(
                "main_workspace",
                {"git": cli29_git_facts(primary, is_main=True, branch="main")},
            ),
            cli29_list_entry(
                "registered",
                {
                    "git": cli29_git_facts(
                        workspace, is_main=False, branch="TASK-1",
                    ),
                    "kent": cli29_kent_facts(
                        workspace, worktree_id="kent-record-task-1",
                    ),
                },
            ),
            cli29_list_entry(
                "external",
                {"git": cli29_git_facts(external, is_main=False, branch="other")},
            ),
            cli29_list_entry(
                "missing",
                {"kent": cli29_kent_facts(
                    missing_root, worktree_id="kent-record-missing",
                )},
            ),
        ],
    }


def run_json(module, function, payload: dict[str, object], *args):
    result = subprocess.CompletedProcess(
        ["kent-worktree-fixture"], 0, json.dumps(payload), "",
    )
    with mock.patch.object(module, "run", return_value=result):
        return function("kent-worktree-fixture", *args)


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise AssertionError(
            f"git {' '.join(args)} failed: {result.stderr or result.stdout}"
        )
    return result


class WorktreeCliCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.janitor = load_janitor()

    def test_source_derived_cli_29_status_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary = Path(temporary) / "main"
            workspace = primary / ".kent" / "worktrees" / "TASK-1"
            payload = cli29_status(primary, workspace)
            self.assertTrue(run_json(
                self.janitor,
                self.janitor.cleanup_session_uses_workspace,
                payload,
                primary,
                "cleanup-session",
                workspace,
            ))

            primary_status = cli29_status(primary, primary)
            self.assertFalse(run_json(
                self.janitor,
                self.janitor.cleanup_session_uses_workspace,
                primary_status,
                primary,
                "cleanup-session",
                workspace,
            ))

            nested = workspace / "subdirectory"
            self.assertTrue(run_json(
                self.janitor,
                self.janitor.cleanup_session_uses_workspace,
                cli29_status(
                    primary, nested, recorded=workspace, observed=workspace,
                ),
                primary,
                "cleanup-session",
                workspace,
            ))

    def test_cli_28_and_legacy_status_dialects_remain_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary = Path(temporary) / "main"
            workspace = primary / ".kent" / "worktrees" / "TASK-1"
            protobuf_status = {
                "target": {"effectiveWorkdir": str(workspace)},
                "worktree": {
                    "recordedRoot": str(workspace),
                    "observedRoot": str(workspace),
                },
            }
            self.assertTrue(run_json(
                self.janitor,
                self.janitor.cleanup_session_uses_workspace,
                protobuf_status,
                primary,
                "cleanup-session",
                workspace,
            ))
            legacy_status = {
                "target": {"EffectiveWorkdir": str(primary)},
                "worktree": {
                    "recorded_root": str(primary),
                    "observed_root": str(primary),
                },
            }
            self.assertFalse(run_json(
                self.janitor,
                self.janitor.cleanup_session_uses_workspace,
                legacy_status,
                primary,
                "cleanup-session",
                workspace,
            ))

    def test_status_rejects_mixed_dialects_and_contradictory_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary = Path(temporary) / "main"
            workspace = primary / ".kent" / "worktrees" / "TASK-1"
            invalid = [
                {
                    "target": {
                        "effective_workdir": str(workspace),
                        "effectiveWorkdir": str(primary),
                    },
                    "worktree": {"recorded_root": str(workspace)},
                },
                {
                    "target": {"effective_workdir": str(workspace)},
                    "worktree": {
                        "recorded_root": str(workspace),
                        "observedRoot": str(workspace),
                    },
                },
                cli29_status(primary, workspace, observed=primary),
                cli29_status(primary, primary, observed=workspace),
            ]
            for payload in invalid:
                with self.subTest(payload=payload):
                    with self.assertRaises(RuntimeError):
                        run_json(
                            self.janitor,
                            self.janitor.cleanup_session_uses_workspace,
                            payload,
                            primary,
                            "cleanup-session",
                            workspace,
                        )

    def test_source_derived_cli_29_topology_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary = Path(temporary) / "main"
            workspace = primary / ".kent" / "worktrees" / "TASK-1"
            external = Path(temporary) / "external"
            payload = cli29_worktree_list(primary, workspace, external)
            entries = run_json(
                self.janitor,
                self.janitor.kent_worktree_entries,
                payload,
                primary,
                "cleanup-session",
                workspace,
            )
            self.assertEqual(
                [entry["variant"] for entry in entries],
                ["mainWorkspace", "registered", "external", "missing"],
            )
            record = run_json(
                self.janitor,
                self.janitor.capture_kent_worktree_record,
                payload,
                primary,
                workspace.resolve(),
                "cleanup-session",
            )
            self.assertEqual(record, {
                "variant": "registered",
                "root": str(workspace.resolve()),
                "worktree_id": "kent-record-task-1",
            })

    def test_primary_identity_cannot_be_admitted_as_managed_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary = Path(temporary) / "main"
            workspace = primary / ".kent" / "worktrees" / "TASK-1"
            external = Path(temporary) / "external"
            payload = cli29_worktree_list(primary, workspace, external)
            registered = payload["worktrees"][1]["topology"]["registered"]
            registered["git"]["is_main"] = True
            with self.assertRaisesRegex(RuntimeError, "primary worktree"):
                run_json(
                    self.janitor,
                    self.janitor.capture_kent_worktree_record,
                    payload,
                    primary,
                    workspace.resolve(),
                    "cleanup-session",
                )

            main_only = {
                "target": payload["target"],
                "worktrees": [payload["worktrees"][0]],
            }
            with self.assertRaisesRegex(RuntimeError, "not registered"):
                run_json(
                    self.janitor,
                    self.janitor.capture_kent_worktree_record,
                    main_only,
                    primary,
                    primary.resolve(),
                    "cleanup-session",
                )

    def test_cli_29_topology_rejects_ambiguous_and_contradictory_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary = Path(temporary).resolve() / "main"
            workspace = primary / ".kent" / "worktrees" / "TASK-1"
            external = Path(temporary).resolve() / "external"
            valid = cli29_worktree_list(primary, workspace, external)
            malformed_entries = []

            duplicate_variant = json.loads(json.dumps(valid["worktrees"][1]))
            duplicate_variant["topology"]["missing"] = {
                "kent": cli29_kent_facts(
                    workspace, worktree_id="another-record",
                ),
            }
            malformed_entries.append(duplicate_variant)

            contradictory_roots = json.loads(json.dumps(valid["worktrees"][1]))
            contradictory_roots["topology"]["registered"]["kent"][
                "canonical_root"
            ] = str(external.resolve())
            malformed_entries.append(contradictory_roots)

            duplicate_primary_fields = json.loads(json.dumps(valid["worktrees"][1]))
            duplicate_primary_fields["topology"]["registered"]["git"][
                "is_main_worktree"
            ] = False
            malformed_entries.append(duplicate_primary_fields)

            for item in malformed_entries:
                with self.subTest(item=item):
                    with self.assertRaises(RuntimeError):
                        self.janitor._kent_record(
                            item, workspace=workspace, primary=primary,
                        )

            duplicate_roots = {
                "target": valid["target"],
                "worktrees": [
                    valid["worktrees"][1],
                    {
                        "topology": {
                            "variant": "external",
                            "external": {"git": cli29_git_facts(
                                workspace, is_main=False,
                            )},
                        },
                        "projection": {
                            "selector": "TASK-1",
                            "is_current": False,
                        },
                    },
                ],
            }
            result = subprocess.CompletedProcess(
                ["kent-worktree-fixture"], 0,
                json.dumps(duplicate_roots), "",
            )
            with mock.patch.object(self.janitor, "run", return_value=result):
                with self.assertRaisesRegex(RuntimeError, "duplicate roots"):
                    self.janitor.kent_worktree_entries(
                        "kent-worktree-fixture",
                        primary,
                        "cleanup-session",
                        workspace,
                    )

    def test_cli_28_protobuf_and_legacy_topology_dialects_remain_supported(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary = Path(temporary) / "main"
            workspace = primary / ".kent" / "worktrees" / "TASK-1"
            protobuf = {
                "topology": {
                    "registered": {
                        "git": {
                            "canonicalRoot": str(workspace),
                            "isMainWorktree": False,
                        },
                        "kent": {
                            "canonicalRoot": str(workspace),
                            "worktreeId": "kent-record-task-1",
                            "managed": True,
                        },
                    },
                },
            }
            self.assertEqual(self.janitor._kent_record(
                protobuf, workspace=workspace, primary=primary,
            ), {
                "variant": "registered",
                "root": str(workspace.resolve()),
                "worktree_id": "kent-record-task-1",
            })
            for is_main in (False, True):
                mixed_protobuf = {
                    "topology": {
                        "registered": {
                            "git": {
                                "canonicalRoot": str(workspace),
                                "isMainWorktree": False,
                                "is_main": is_main,
                            },
                            "kent": {
                                "canonicalRoot": str(workspace),
                                "worktreeId": "kent-record-task-1",
                                "managed": True,
                            },
                        },
                    },
                }
                with self.subTest(is_main=is_main):
                    with self.assertRaisesRegex(
                        RuntimeError, "mixes identity dialects",
                    ):
                        self.janitor._kent_record(
                            mixed_protobuf,
                            workspace=workspace,
                            primary=primary,
                        )
            legacy_variant = {
                "topology": {
                    "variant": "registered",
                    "registered": {
                        "git": {"canonical_root": str(workspace)},
                        "kent": {
                            "canonical_root": str(workspace),
                            "worktree_id": "kent-record-task-1",
                            "managed": True,
                        },
                    },
                },
            }
            self.assertEqual(self.janitor._kent_record(
                legacy_variant, workspace=workspace, primary=primary,
            )["variant"], "registered")

    def test_cli_29_delete_outcomes_and_older_delete_dialects(self) -> None:
        outcomes = (
            ("not_requested", None),
            ("not_applicable", None),
            ("deleted", "TASK-1"),
            ("retained", "TASK-1"),
        )
        for kind, branch in outcomes:
            with self.subTest(kind=kind):
                cleanup: dict[str, object] = {"kind": kind}
                if branch is not None:
                    cleanup["branch_name"] = branch
                if kind == "deleted":
                    cleanup["diagnostic"] = "branch deleted"
                self.assertEqual(
                    self.janitor.deletion_result_kind({"cleanup": cleanup}),
                    "completed",
                )

        protobuf_cleanup = {
            "kind": "WORKTREE_BRANCH_CLEANUP_OUTCOME_DELETED",
            "branchName": "TASK-1",
        }
        self.assertEqual(
            self.janitor.deletion_result_kind({"cleanup": protobuf_cleanup}),
            "completed",
        )
        self.assertEqual(
            self.janitor.deletion_result_kind({"kind": "completed"}),
            "completed",
        )
        self.assertEqual(self.janitor.deletion_result_kind({
            "kind": "scheduled",
            "scheduled": {"operation_id": "operation-1"},
        }), "scheduled")

    def test_delete_decoder_rejects_leftovers_mixed_and_ambiguous_results(self) -> None:
        invalid = [
            {},
            {"cleanup": {"kind": "unknown"}},
            {"cleanup": {"kind": "deleted"}},
            {"cleanup": {"kind": "deleted", "branch_name": ""}},
            {"cleanup": {"kind": "deleted", "branch_name": 7}},
            {"cleanup": {
                "kind": "deleted",
                "branch_name": "TASK-1",
                "branchName": "OTHER",
            }},
            {"cleanup": {"kind": "deleted", "branch_name": "TASK-1"},
             "leftover_root": None},
            {"cleanup": {"kind": "deleted", "branch_name": "TASK-1"},
             "leftover_root": "/retained"},
            {"cleanup": {"kind": "deleted", "branch_name": "TASK-1"},
             "leftoverRoot": None},
            {"cleanup": {"kind": "deleted", "branch_name": "TASK-1"},
             "unexpected": True},
            {"kind": "completed", "cleanup": {"kind": "deleted"}},
            {"cleanup": {"kind": "deleted", "branch_name": "TASK-1"},
             "kind": "completed"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises((TypeError, ValueError)):
                    self.janitor.deletion_result_kind(payload)

        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            self.janitor.json_object(
                '{"cleanup":{"kind":"deleted"},"cleanup":{"kind":"retained"}}',
                "Kent delete",
            )

    def test_cli_29_delete_runs_once_and_checks_real_git_postconditions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            primary = base / "main"
            primary.mkdir()
            git(primary, "init", "-q", "--initial-branch=main")
            git(primary, "config", "user.name", "Kent Test")
            git(primary, "config", "user.email", "kent@example.invalid")
            (primary / "tracked.txt").write_text("ready\n")
            git(primary, "add", "tracked.txt")
            git(primary, "commit", "-q", "-m", "Initial")
            workspace = primary / ".kent" / "worktrees" / "TASK-1"
            workspace.parent.mkdir(parents=True)
            git(primary, "worktree", "add", "-q", "-b", "TASK-1", str(workspace))
            workspace = workspace.resolve()
            branch_ref = "refs/heads/TASK-1"
            before_oid = git(primary, "rev-parse", branch_ref).stdout.strip()
            external = base / "external"
            delete_calls: list[tuple[str, ...]] = []

            source_copy = base / "workflow-task-janitor"
            shutil.copyfile(JANITOR_PATH, source_copy)
            janitor = load_janitor(source_copy)
            before_list = cli29_worktree_list(primary, workspace, external)
            after_list = {
                "target": cli29_target(primary, primary),
                "worktrees": [
                    cli29_list_entry(
                        "main_workspace",
                        {"git": cli29_git_facts(
                            primary, is_main=True, branch="main",
                        )},
                    ),
                ],
            }
            delete_output = {
                "cleanup": {"kind": "deleted", "branch_name": "TASK-1"},
            }
            real_run = janitor.run

            def fake_kent_run(args, **kwargs):
                if args[0] != "kent-worktree-fixture":
                    return real_run(args, **kwargs)
                if args[1] == "list":
                    listed = before_list if workspace.exists() else after_list
                    return subprocess.CompletedProcess(
                        args, 0, json.dumps(listed), "",
                    )
                if args[1] == "delete":
                    delete_calls.append(tuple(args))
                    git(primary, "worktree", "remove", "--force", str(workspace))
                    git(primary, "branch", "-D", "TASK-1")
                    return subprocess.CompletedProcess(
                        args, 0, json.dumps(delete_output), "",
                    )
                raise AssertionError(f"unexpected Kent command: {args}")

            with (
                mock.patch.object(janitor, "run", side_effect=fake_kent_run),
                mock.patch.object(janitor.os, "chdir"),
            ):
                expected = janitor.capture_kent_worktree_record(
                    "kent-worktree-fixture", primary, workspace, "cleanup-session",
                )
                deleted, report, blocker = janitor.delete_managed_worktree(
                    "kent-worktree-fixture",
                    primary,
                    workspace,
                    "cleanup-session",
                    "Legacy cleanup fixture.",
                    expected,
                )

            self.assertTrue(deleted, (report, blocker))
            self.assertEqual(blocker, "")
            self.assertEqual(len(delete_calls), 1)
            self.assertFalse(workspace.exists())
            self.assertNotIn(
                str(workspace),
                git(primary, "worktree", "list", "--porcelain").stdout,
            )
            self.assertNotEqual(
                subprocess.run(
                    ["git", "-C", str(primary), "show-ref", "--verify",
                     "--quiet", branch_ref],
                    check=False,
                ).returncode,
                0,
            )
            primary_head = git(primary, "rev-parse", "HEAD").stdout.strip()
            self.assertEqual(before_oid, primary_head)


if __name__ == "__main__":
    unittest.main()
