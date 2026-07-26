from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from workflowkit.merge_strategy import (
    resolve_github_merge_strategy,
    resolve_merge_strategy,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class MergeStrategyTest(unittest.TestCase):
    def test_merge_policy_resolves_when_merge_is_allowed(self) -> None:
        result = resolve_merge_strategy("merge", ("merge", "rebase"))
        self.assertEqual(result.outcome, "resolved")
        self.assertEqual(result.strategy, "merge")

    def test_squash_policy_resolves_with_linear_history(self) -> None:
        result = resolve_merge_strategy(
            "squash",
            ("merge", "squash", "rebase"),
            required_linear_history=True,
        )
        self.assertEqual(result.strategy, "squash")

    def test_rebase_policy_resolves(self) -> None:
        result = resolve_merge_strategy("rebase", ("rebase",))
        self.assertEqual(result.strategy, "rebase")

    def test_auto_is_ambiguous_when_two_linear_methods_remain(self) -> None:
        result = resolve_merge_strategy(
            "auto",
            ("merge", "squash", "rebase"),
            required_linear_history=True,
        )
        self.assertEqual(result.outcome, "needs_user_action")
        self.assertEqual(result.code, "ambiguous_strategy")
        self.assertEqual(result.candidates, ("squash", "rebase"))

    def test_explicit_merge_is_incompatible_with_linear_history(self) -> None:
        result = resolve_merge_strategy(
            "merge",
            ("merge", "rebase"),
            required_linear_history=True,
        )
        self.assertEqual(result.code, "explicit_strategy_incompatible")
        self.assertIsNone(result.strategy)

    def test_merge_queue_method_narrows_auto(self) -> None:
        result = resolve_merge_strategy(
            "auto",
            ("merge", "squash", "rebase"),
            merge_queue_method="squash",
        )
        self.assertEqual(result.strategy, "squash")

    def test_puber_github_payload_resolves_auto_to_rebase(self) -> None:
        result = resolve_github_merge_strategy(
            "auto",
            {
                "repository": {
                    "allow_merge_commit": True,
                    "allow_squash_merge": False,
                    "allow_rebase_merge": True,
                },
                "branch_protection": {
                    "required_linear_history": {"enabled": True},
                },
                "rulesets": [],
            },
        )
        self.assertEqual(result.strategy, "rebase")

    def test_active_ruleset_and_queue_are_consumed(self) -> None:
        result = resolve_github_merge_strategy(
            "auto",
            {
                "repository": {
                    "mergeCommitAllowed": True,
                    "squashMergeAllowed": True,
                    "rebaseMergeAllowed": True,
                },
                "rulesets": [
                    {
                        "enforcement": "active",
                        "rules": [
                            {"type": "required_linear_history"},
                            {
                                "type": "merge_queue",
                                "parameters": {"merge_method": "SQUASH"},
                            },
                        ],
                    }
                ],
            },
        )
        self.assertEqual(result.strategy, "squash")

    def test_cli_returns_structured_ambiguity(self) -> None:
        result = subprocess.run(
            [
                str(REPO_ROOT / "scripts" / "resolve-github-merge-strategy"),
                "--policy",
                "auto",
            ],
            input=json.dumps(
                {
                    "repository": {
                        "allow_merge_commit": False,
                        "allow_squash_merge": True,
                        "allow_rebase_merge": True,
                    }
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KENT_ENGINEERING_KIT_PYTHON": sys.executable,
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["outcome"], "needs_user_action")
        self.assertEqual(payload["code"], "ambiguous_strategy")
        self.assertEqual(payload["candidates"], ["squash", "rebase"])


if __name__ == "__main__":
    unittest.main()
