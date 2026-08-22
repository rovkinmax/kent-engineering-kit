from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from workflowkit.operations import (
    OperationJournal,
    activate_primary_checkout,
    canonical_bytes,
    load_plan,
)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


class PrimaryActivationTest(unittest.TestCase):
    def test_prepared_rollback_has_no_git_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git(root, "init", "-q")
            git(root, "branch", "-M", "main")
            git(root, "config", "user.name", "Kent Test")
            git(root, "config", "user.email", "kent@example.invalid")
            (root / "tracked").write_text("one\n")
            git(root, "add", ".")
            git(root, "commit", "-q", "-m", "one")
            baseline = git(root, "rev-parse", "HEAD")
            (root / "tracked").write_text("two\n")
            git(root, "commit", "-q", "-am", "two")
            target = git(root, "rev-parse", "HEAD")
            git(root, "reset", "--hard", "-q", baseline)
            kit_prompt = root.parent / "kit-release-decision.md"
            kit_prompt.write_text("release decision\n")
            config = root.parent / "config.toml"
            config.write_text("[roles.release_decision]\nprompt = true\n")
            value = {
                "schema": "kit-primary-activation-plan-v1",
                "state_dir": str(root.parent / "state"),
                "primary_root": str(root),
                "branch": "main",
                "baseline_commit": baseline,
                "target_commit": target,
                "role": {
                    "prompt_path": str(root.parent / "installed-release-decision.md"),
                    "config_path": str(config),
                    "kit_prompt_path": str(kit_prompt),
                    "expected_prompt_sha256": hashlib.sha256(
                        b"installed\n"
                    ).hexdigest(),
                },
                "git_config_allowlist": {},
                "tracking": None,
                "installed_links": [],
                "prompt_prestate": {"kind": "absent", "target": None, "sha256": None},
                "backups": {"path": str(root.parent / "installed-release-decision.md.release-decision.backup"), "kind": "absent", "sha256": None},
                "source_prompt_sha256": hashlib.sha256(b"release decision\n").hexdigest(),
            }
            path = root.parent / "plan.json"
            raw = canonical_bytes(value)
            path.write_bytes(raw)
            plan = load_plan(
                path,
                schema="kit-primary-activation-plan-v1",
                expected_sha256=hashlib.sha256(raw).hexdigest(),
            )
            with OperationJournal(root.parent / "state", "kit-primary-activation", plan) as journal:
                journal.persist({"phase": "prepared", "effects": {}})
            report = activate_primary_checkout(
                plan, mode="rollback", confirm=True
            )
            self.assertEqual(report["phase"], "rolled_back")
            self.assertEqual(git(root, "rev-parse", "HEAD"), baseline)


if __name__ == "__main__":
    unittest.main()
