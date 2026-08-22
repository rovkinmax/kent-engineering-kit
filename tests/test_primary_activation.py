from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import workflowkit.operations as operations
from workflowkit.operations import (
    EffectBlocked,
    JournalError,
    OperationError,
    OperationJournal,
    PlanValidationError,
    activate_primary_checkout,
    canonical_bytes,
    canonical_sha256,
    load_plan,
)


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


def load_activation_plan(
    root: Path,
    value: dict,
    name: str = "plan.json",
) -> operations.LoadedPlan:
    path = root / name
    raw = canonical_bytes(value)
    path.write_bytes(raw)
    return load_plan(
        path,
        schema="kit-primary-activation-plan-v1",
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )


def write_role_config(path: Path, prompt: Path) -> None:
    path.write_text(
        '[subagents.other]\n'
        'model = "gpt-test"\n'
        'tools = "read-only"\n'
        "\n"
        "[subagents.release-decision]\n"
        'description = "Release decision"\n'
        'model = "gpt-test"\n'
        f'system_prompt_file = "{prompt.name}"\n'
        "agent_callable = false\n"
        "workflow_subagent = false\n"
        "\n"
        "[subagents.release-decision.tools]\n"
        "shell = false\n"
        "patch = false\n"
        "edit = false\n"
    )


def activation_fixture(root: Path, *, duplicate_config: bool = False) -> dict:
    repository = root / "primary"
    repository.mkdir()
    git(repository, "init", "-q")
    git(repository, "branch", "-M", "main")
    git(repository, "config", "user.name", "Kent Test")
    git(repository, "config", "user.email", "kent@example.invalid")
    tracked = repository / "tracked"
    tracked.write_text("baseline\n")
    git(repository, "add", ".")
    git(repository, "commit", "-q", "-m", "baseline")
    baseline = git(repository, "rev-parse", "HEAD")
    tracked.write_text("target\n")
    git(repository, "commit", "-q", "-am", "target")
    target = git(repository, "rev-parse", "HEAD")
    git(repository, "reset", "--hard", "-q", baseline)
    git(repository, "config", "branch.main.remote", "origin")
    git(repository, "config", "branch.main.merge", "refs/heads/main")
    if duplicate_config:
        git(repository, "config", "extensions.worktreeConfig", "true")
        git(repository, "config", "--worktree", "--add", "test.duplicate", "alpha")
        git(repository, "config", "--worktree", "--add", "test.duplicate", "beta")

    prompt_bytes = b"release decision\n"
    source_prompt = root / "kit-release-decision.md"
    source_prompt.write_bytes(prompt_bytes)
    installed_prompt = root / "installed-release-decision.md"
    installed_prompt.write_bytes(prompt_bytes)
    backup = root / "installed-release-decision.md.release-decision.backup"
    role_config = root / "config.toml"
    write_role_config(role_config, installed_prompt)
    link_target = root / "kit-command"
    link_target.write_text("#!/bin/sh\nexit 0\n")
    installed_link = root / "installed-command"
    installed_link.symlink_to(link_target)

    prompt_sha256 = hashlib.sha256(prompt_bytes).hexdigest()
    value = {
        "schema": "kit-primary-activation-plan-v1",
        "state_dir": str(root / "state"),
        "primary_root": str(repository),
        "branch": "main",
        "baseline_commit": baseline,
        "target_commit": target,
        "role": {
            "prompt_path": str(installed_prompt),
            "config_path": str(role_config),
            "kit_prompt_path": str(source_prompt),
            "expected_prompt_sha256": prompt_sha256,
        },
        "git_config_allowlist": operations._git_config_inventory(repository),
        "tracking": {"remote": "origin", "merge": "refs/heads/main"},
        "installed_links": [
            {"path": str(installed_link), "target": str(link_target)}
        ],
        "prompt_prestate": {
            "kind": "file",
            "target": None,
            "sha256": prompt_sha256,
        },
        "backups": {
            "path": str(backup),
            "kind": "absent",
            "sha256": None,
        },
        "source_prompt_sha256": prompt_sha256,
    }
    return {
        "root": root,
        "repository": repository,
        "tracked": tracked,
        "baseline": baseline,
        "target": target,
        "source_prompt": source_prompt,
        "installed_prompt": installed_prompt,
        "backup": backup,
        "role_config": role_config,
        "link_target": link_target,
        "installed_link": installed_link,
        "prompt_bytes": prompt_bytes,
        "value": value,
        "plan": load_activation_plan(root, value),
    }


def journal_path(fixture: dict) -> Path:
    state_dir = Path(fixture["value"]["state_dir"])
    return state_dir / "kit-primary-activation.journal.json"


def assert_apply_blocked(test: unittest.TestCase, fixture: dict) -> None:
    with test.assertRaises(OperationError):
        activate_primary_checkout(
            fixture["plan"],
            mode="apply",
            confirm=fixture["plan"].sha256,
        )
    test.assertEqual(
        git(fixture["repository"], "rev-parse", "HEAD"),
        fixture["baseline"],
    )
    test.assertFalse(journal_path(fixture).exists())


def set_prompt_phase(fixture: dict, phase: str) -> None:
    prompt = fixture["installed_prompt"]
    backup = fixture["backup"]
    prompt.unlink(missing_ok=True)
    backup.unlink(missing_ok=True)
    if phase in {"regular", "both"}:
        prompt.write_bytes(fixture["prompt_bytes"])
    if phase in {"both", "backup-only", "symlink", "final"}:
        backup.write_bytes(fixture["prompt_bytes"])
    if phase in {"symlink", "final"}:
        prompt.symlink_to(fixture["source_prompt"])


def seed_activation_phase(fixture: dict, phase: str, prompt_phase: str) -> None:
    data = operations._validate_activation_plan(fixture["plan"])
    preflight = operations._activation_preflight(
        data,
        fixture["baseline"],
        "baseline",
    )
    git(fixture["repository"], "reset", "--hard", "-q", fixture["target"])
    set_prompt_phase(fixture, prompt_phase)
    with OperationJournal(
        Path(fixture["value"]["state_dir"]),
        "kit-primary-activation",
        fixture["plan"],
    ) as journal:
        journal.persist(
            {
                "phase": phase,
                "preflight": preflight,
                "effects": {},
            }
        )


class PrimaryActivationTest(unittest.TestCase):
    def test_matching_regular_prompt_reaches_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = activation_fixture(Path(temporary))
            report = activate_primary_checkout(
                fixture["plan"],
                mode="apply",
                confirm=fixture["plan"].sha256,
            )
            self.assertEqual(report["phase"], "verified")
            self.assertEqual(report["effects_released"], 1)
            self.assertEqual(
                git(fixture["repository"], "rev-parse", "HEAD"),
                fixture["target"],
            )
            self.assertTrue(fixture["installed_prompt"].is_symlink())
            self.assertEqual(
                fixture["installed_prompt"].resolve(),
                fixture["source_prompt"].resolve(),
            )
            self.assertEqual(
                fixture["backup"].read_bytes(),
                fixture["prompt_bytes"],
            )

    def test_plan_requires_equal_source_expected_digest_and_tracking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = activation_fixture(Path(temporary))
            cases = (
                (
                    "source-digest",
                    lambda value: value.update(source_prompt_sha256="f" * 64),
                ),
                (
                    "expected-digest",
                    lambda value: value["role"].update(
                        expected_prompt_sha256="f" * 64
                    ),
                ),
                ("tracking", lambda value: value.update(tracking=None)),
            )
            for label, mutate in cases:
                with self.subTest(label=label):
                    value = json.loads(json.dumps(fixture["value"]))
                    value["state_dir"] = str(fixture["root"] / f"state-{label}")
                    mutate(value)
                    plan = load_activation_plan(
                        fixture["root"],
                        value,
                        f"{label}.json",
                    )
                    with self.assertRaises(PlanValidationError):
                        activate_primary_checkout(plan, mode="preview")

    def test_prompt_source_symlink_and_backup_drift_block_before_commit(self) -> None:
        mutations = {
            "mismatching prompt": lambda fixture: fixture[
                "installed_prompt"
            ].write_text("different\n"),
            "missing source": lambda fixture: fixture["source_prompt"].unlink(),
            "wrong source": lambda fixture: fixture["source_prompt"].write_text(
                "different\n"
            ),
            "foreign prompt symlink": self._foreign_prompt_symlink,
            "dangling prompt symlink": self._dangling_prompt_symlink,
            "existing backup": lambda fixture: fixture["backup"].write_text(
                "occupied\n"
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                fixture = activation_fixture(Path(temporary))
                mutate(fixture)
                assert_apply_blocked(self, fixture)

    @staticmethod
    def _foreign_prompt_symlink(fixture: dict) -> None:
        foreign = fixture["root"] / "foreign-prompt"
        foreign.write_bytes(fixture["prompt_bytes"])
        fixture["installed_prompt"].unlink()
        fixture["installed_prompt"].symlink_to(foreign)

    @staticmethod
    def _dangling_prompt_symlink(fixture: dict) -> None:
        fixture["installed_prompt"].unlink()
        fixture["installed_prompt"].symlink_to(
            fixture["root"] / "missing-prompt"
        )

    def test_role_config_authority_drift_blocks_before_commit(self) -> None:
        replacements = {
            "shell enabled": ("shell = false", "shell = true"),
            "agent callable": ("agent_callable = false", "agent_callable = true"),
            "workflow subagent": (
                "workflow_subagent = false",
                "workflow_subagent = true",
            ),
            "foreign prompt": (
                'system_prompt_file = "installed-release-decision.md"',
                'system_prompt_file = "foreign.md"',
            ),
        }
        for label, (old, new) in replacements.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                fixture = activation_fixture(Path(temporary))
                contents = fixture["role_config"].read_text()
                fixture["role_config"].write_text(contents.replace(old, new))
                assert_apply_blocked(self, fixture)

    def test_installed_link_drift_blocks_before_commit(self) -> None:
        for label in ("absent", "foreign", "dangling"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                fixture = activation_fixture(Path(temporary))
                fixture["installed_link"].unlink()
                if label == "foreign":
                    foreign = fixture["root"] / "foreign-command"
                    foreign.write_text("foreign\n")
                    fixture["installed_link"].symlink_to(foreign)
                elif label == "dangling":
                    fixture["installed_link"].symlink_to(
                        fixture["root"] / "missing-command"
                    )
                assert_apply_blocked(self, fixture)

    def test_tracking_and_complete_git_config_drift_block_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = activation_fixture(Path(temporary))
            git(fixture["repository"], "config", "branch.main.remote", "upstream")
            assert_apply_blocked(self, fixture)
        with tempfile.TemporaryDirectory() as temporary:
            fixture = activation_fixture(Path(temporary), duplicate_config=True)
            preview = activate_primary_checkout(fixture["plan"], mode="preview")
            self.assertEqual(preview["phase"], "preview")
            git(
                fixture["repository"],
                "config",
                "--worktree",
                "--add",
                "test.duplicate",
                "gamma",
            )
            assert_apply_blocked(self, fixture)
        dangerous = {
            "core.hooksPath": "/tmp/hooks",
            "filter.release.clean": "cat",
            "pager.log": "cat",
            "credential.helper": "store",
            "maintenance.auto": "true",
            "core.fsmonitor": "true",
            "core.attributesFile": "/tmp/attributes",
        }
        for key, value in dangerous.items():
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                fixture = activation_fixture(Path(temporary))
                git(fixture["repository"], "config", key, value)
                assert_apply_blocked(self, fixture)

    def test_dirty_branch_baseline_and_non_fast_forward_drift_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = activation_fixture(Path(temporary))
            fixture["tracked"].write_text("dirty\n")
            assert_apply_blocked(self, fixture)
        with tempfile.TemporaryDirectory() as temporary:
            fixture = activation_fixture(Path(temporary))
            git(fixture["repository"], "switch", "-q", "-c", "other")
            assert_apply_blocked(self, fixture)
        with tempfile.TemporaryDirectory() as temporary:
            fixture = activation_fixture(Path(temporary))
            git(fixture["repository"], "reset", "--hard", "-q", fixture["target"])
            with self.assertRaises(OperationError):
                activate_primary_checkout(fixture["plan"], mode="apply", confirm=True)
            self.assertFalse(journal_path(fixture).exists())
        with tempfile.TemporaryDirectory() as temporary:
            fixture = activation_fixture(Path(temporary))
            git(fixture["repository"], "reset", "--hard", "-q", fixture["target"])
            value = json.loads(json.dumps(fixture["value"]))
            value["state_dir"] = str(fixture["root"] / "state-non-fast-forward")
            value["baseline_commit"] = fixture["target"]
            value["target_commit"] = fixture["baseline"]
            fixture["plan"] = load_activation_plan(
                fixture["root"],
                value,
                "non-fast-forward.json",
            )
            fixture["value"] = value
            with self.assertRaises(OperationError):
                activate_primary_checkout(fixture["plan"], mode="apply", confirm=True)
            self.assertFalse(journal_path(fixture).exists())

    def test_exact_git_invocation_environment_and_post_git_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = activation_fixture(Path(temporary))
            captured: dict = {}
            real_adopt = operations._adopt_release_prompt

            def settle(_journal: OperationJournal, **kwargs: object) -> str:
                captured.update(kwargs)
                result = subprocess.run(
                    kwargs["command"],
                    cwd=kwargs["cwd"],
                    env=operations._safe_env(kwargs["extra_env"]),
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                return "postimage"

            def adopt(data: dict) -> None:
                self.assertEqual(
                    git(fixture["repository"], "rev-parse", "HEAD"),
                    fixture["target"],
                )
                self.assertTrue(fixture["installed_prompt"].is_file())
                self.assertFalse(fixture["installed_prompt"].is_symlink())
                real_adopt(data)

            with mock.patch.object(operations, "_settle_or_run", side_effect=settle):
                with mock.patch.object(
                    operations,
                    "_adopt_release_prompt",
                    side_effect=adopt,
                ):
                    report = activate_primary_checkout(
                        fixture["plan"],
                        mode="apply",
                        confirm=fixture["plan"].sha256,
                    )
            self.assertEqual(report["phase"], "verified")
            self.assertEqual(
                captured["command"],
                [
                    "/usr/bin/git",
                    "-C",
                    str(fixture["repository"]),
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "credential.helper=",
                    "-c",
                    "maintenance.auto=false",
                    "merge",
                    "--ff-only",
                    fixture["target"],
                ],
            )
            self.assertEqual(captured["extra_env"], operations._GIT_EFFECT_ENV)
            safe_environment = operations._safe_env(captured["extra_env"])
            self.assertEqual(safe_environment["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(safe_environment["GIT_PAGER"], "cat")
            self.assertEqual(safe_environment["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertEqual(safe_environment["GIT_CONFIG_GLOBAL"], os.devnull)

    def test_exact_target_recovery_does_not_replay_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = activation_fixture(Path(temporary))
            data = operations._validate_activation_plan(fixture["plan"])
            preflight = operations._activation_preflight(
                data,
                fixture["baseline"],
                "baseline",
            )
            pre_git = preflight["git"]
            post_git = {
                **pre_git,
                "head": fixture["target"],
                "main_ref": fixture["target"],
            }
            command = operations._activation_merge_command(data)
            identity = operations._effect_inputs(
                command,
                fixture["repository"],
                operations._GIT_EFFECT_ENV,
                None,
                canonical_sha256(pre_git),
                canonical_sha256(post_git),
            )[4]
            with OperationJournal(
                Path(fixture["value"]["state_dir"]),
                "kit-primary-activation",
                fixture["plan"],
            ) as journal:
                journal.persist(
                    {
                        "phase": "activation_committed",
                        "preflight": preflight,
                        "effects": {
                            "primary-merge": {
                                **identity,
                                "status": "unresolved",
                                "attempt": 1,
                                "child": {
                                    "guardian_pid": 999999,
                                    "child_pid": 999998,
                                },
                            }
                        },
                    }
                )
            git(fixture["repository"], "reset", "--hard", "-q", fixture["target"])
            with mock.patch.object(
                operations,
                "run_effect",
                side_effect=AssertionError("merge replayed"),
            ):
                report = activate_primary_checkout(
                    fixture["plan"],
                    mode="apply",
                    confirm=fixture["plan"].sha256,
                )
            self.assertEqual(report["phase"], "verified")
            with OperationJournal(
                Path(fixture["value"]["state_dir"]),
                "kit-primary-activation",
                fixture["plan"],
            ) as journal:
                self.assertEqual(
                    journal.state["effects"]["primary-merge"]["attempt"],
                    1,
                )

    def test_later_cycle_retries_settled_preimage_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = activation_fixture(Path(temporary))
            counter = fixture["root"] / "merge-count"
            executable = fixture["root"] / "controlled-merge"
            executable.write_text(
                f"#!{Path(sys.executable).resolve()}\n"
                + textwrap.dedent(
                    f"""
                    from pathlib import Path
                    import subprocess

                    counter = Path({str(counter)!r})
                    count = int(counter.read_text()) + 1 if counter.exists() else 1
                    counter.write_text(str(count))
                    if count == 2:
                        subprocess.run(
                            [
                                "/usr/bin/git",
                                "-C",
                                {str(fixture["repository"])!r},
                                "merge",
                                "--ff-only",
                                {fixture["target"]!r},
                            ],
                            check=True,
                        )
                    """
                )
            )
            executable.chmod(0o755)
            with mock.patch.object(
                operations,
                "_activation_merge_command",
                return_value=[str(executable)],
            ):
                first = activate_primary_checkout(
                    fixture["plan"],
                    mode="apply",
                    confirm=fixture["plan"].sha256,
                )
                self.assertEqual(first["phase"], "activation_committed")
                self.assertEqual(first["settled"], "preimage")
                self.assertEqual(counter.read_text(), "1")
                second = activate_primary_checkout(
                    fixture["plan"],
                    mode="apply",
                    confirm=fixture["plan"].sha256,
                )
            self.assertEqual(second["phase"], "verified")
            self.assertEqual(counter.read_text(), "2")
            with OperationJournal(
                Path(fixture["value"]["state_dir"]),
                "kit-primary-activation",
                fixture["plan"],
            ) as journal:
                self.assertEqual(
                    journal.state["effects"]["primary-merge"]["attempt"],
                    2,
                )

    def test_prompt_and_journal_phase_recovery_converge(self) -> None:
        scenarios = {
            "regular-only": ("primary_promoted", "regular"),
            "prompt-and-backup": ("primary_promoted", "both"),
            "backup-only": ("primary_promoted", "backup-only"),
            "symlink-created": ("primary_promoted", "symlink"),
            "role-adopted": ("role_adopted", "final"),
            "verified": ("verified", "final"),
        }
        for label, (journal_phase, prompt_phase) in scenarios.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                fixture = activation_fixture(Path(temporary))
                seed_activation_phase(fixture, journal_phase, prompt_phase)
                report = activate_primary_checkout(
                    fixture["plan"],
                    mode="apply",
                    confirm=fixture["plan"].sha256,
                )
                self.assertEqual(report["phase"], "verified")
                self.assertTrue(fixture["installed_prompt"].is_symlink())
                self.assertEqual(
                    fixture["installed_prompt"].resolve(),
                    fixture["source_prompt"].resolve(),
                )
                self.assertEqual(
                    fixture["backup"].read_bytes(),
                    fixture["prompt_bytes"],
                )

    def test_final_readback_rejects_every_bound_authority_drift(self) -> None:
        mutations = {
            "target head": lambda fixture: git(
                fixture["repository"],
                "reset",
                "--hard",
                "-q",
                fixture["baseline"],
            ),
            "Git config": lambda fixture: git(
                fixture["repository"],
                "config",
                "test.extra",
                "value",
            ),
            "installed link": self._drift_installed_link,
            "installed target bytes": lambda fixture: fixture[
                "link_target"
            ].write_text("changed\n"),
            "role config bytes": lambda fixture: fixture[
                "role_config"
            ].write_text(fixture["role_config"].read_text() + "# drift\n"),
            "prompt": self._drift_final_prompt,
            "backup": lambda fixture: fixture["backup"].write_text("changed\n"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                fixture = activation_fixture(Path(temporary))
                seed_activation_phase(fixture, "verified", "final")
                mutate(fixture)
                with self.assertRaises((OperationError, EffectBlocked)):
                    activate_primary_checkout(
                        fixture["plan"],
                        mode="apply",
                        confirm=fixture["plan"].sha256,
                    )

    @staticmethod
    def _drift_installed_link(fixture: dict) -> None:
        foreign = fixture["root"] / "foreign-command"
        foreign.write_text("foreign\n")
        fixture["installed_link"].unlink()
        fixture["installed_link"].symlink_to(foreign)

    @staticmethod
    def _drift_final_prompt(fixture: dict) -> None:
        fixture["installed_prompt"].unlink()
        fixture["installed_prompt"].write_bytes(fixture["prompt_bytes"])

    def test_prepared_rollback_has_no_git_or_file_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = activation_fixture(Path(temporary))
            data = operations._validate_activation_plan(fixture["plan"])
            preflight = operations._activation_preflight(
                data,
                fixture["baseline"],
                "baseline",
            )
            with OperationJournal(
                Path(fixture["value"]["state_dir"]),
                "kit-primary-activation",
                fixture["plan"],
            ) as journal:
                journal.persist(
                    {
                        "phase": "prepared",
                        "preflight": preflight,
                        "effects": {},
                    }
                )
            report = activate_primary_checkout(
                fixture["plan"],
                mode="rollback",
                confirm=fixture["plan"].sha256,
            )
            self.assertEqual(report["phase"], "rolled_back")
            self.assertEqual(
                git(fixture["repository"], "rev-parse", "HEAD"),
                fixture["baseline"],
            )
            self.assertTrue(fixture["installed_prompt"].is_file())
            self.assertFalse(fixture["installed_prompt"].is_symlink())
            self.assertFalse(fixture["backup"].exists())

    def test_rollback_rejects_every_committed_phase(self) -> None:
        phases = (
            "activation_committed",
            "primary_promoted",
            "role_adopted",
            "verified",
        )
        for phase in phases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                fixture = activation_fixture(Path(temporary))
                data = operations._validate_activation_plan(fixture["plan"])
                preflight = operations._activation_preflight(
                    data,
                    fixture["baseline"],
                    "baseline",
                )
                with OperationJournal(
                    Path(fixture["value"]["state_dir"]),
                    "kit-primary-activation",
                    fixture["plan"],
                ) as journal:
                    journal.persist(
                        {
                            "phase": phase,
                            "preflight": preflight,
                            "effects": {},
                        }
                    )
                with self.assertRaises(JournalError):
                    activate_primary_checkout(
                        fixture["plan"],
                        mode="rollback",
                        confirm=fixture["plan"].sha256,
                    )
                self.assertEqual(
                    git(fixture["repository"], "rev-parse", "HEAD"),
                    fixture["baseline"],
                )


if __name__ == "__main__":
    unittest.main()
