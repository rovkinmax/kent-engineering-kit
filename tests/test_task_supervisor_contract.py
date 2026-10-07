"""Static contract checks; these do not execute an LLM or native Kent effects."""

import json
from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


def normalized(text: str) -> str:
    return " ".join(text.split())


def section(text: str, heading: str) -> str:
    match = re.search(
        rf"(?m)^{re.escape(heading)}\n(?P<body>.*?)(?=^# |\Z)",
        text,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"Missing role section: {heading}")
    return normalized(match["body"])


class TaskSupervisorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.role = (REPO_ROOT / "agents/task-supervisor.md").read_text()
        cls.text = normalized(cls.role)
        cls.contract = normalized(
            (REPO_ROOT / "contracts/role-contract.md").read_text()
        )
        cls.cases = json.loads(
            (REPO_ROOT / "tests/fixtures/supervisor-operational-cases.json").read_text()
        )

    def test_acceptance_cases_bind_to_shipped_policy(self) -> None:
        self.assertEqual(set(self.cases), {"description", "cases"})
        self.assertIn("not execution", self.cases["description"])
        identifiers = set()
        for case in self.cases["cases"]:
            with self.subTest(case=case["id"]):
                self.assertEqual(set(case), {"id", "input", "expected", "guards"})
                self.assertNotIn(case["id"], identifiers)
                identifiers.add(case["id"])
                self.assertTrue(case["input"].strip())
                self.assertTrue(case["expected"].strip())
                self.assertGreaterEqual(len(case["guards"]), 2)
                self.assertEqual(len(case["guards"]), len(set(case["guards"])))
                for guard in case["guards"]:
                    self.assertIn(normalized(guard), self.text)
        self.assertEqual(
            identifiers,
            {
                "whole-selection", "failed-read", "existing-owner-gate",
                "existing-authority", "delegated-choice", "mandatory-judgment",
                "backlog-limit", "material-grill", "unavailable-grill",
                "operator-capability", "authorized-merge", "changed-head",
                "project-ci-waiver", "merge-queue", "qualified-recovery",
                "unqualified-edge", "deliberate-stop", "ambiguous-effect",
                "proved-noop", "fanout-invariants", "installation-unproved",
                "compaction-catchup",
            },
        )

    def test_operating_cycle_orders_scan_action_settlement_and_observation(self) -> None:
        cycle = section(self.role, "# Operating cycle")
        steps = [
            "1. Re-read all selected Tasks",
            "2. Classify each actionable incident",
            "3. Fairly process",
            "4. Keep unresolved incidents",
            "5. Notify new actionable blockers",
        ]
        positions = [cycle.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        for guard in (
            "a focused follow-up never replaces the whole-selection scan",
            "unknown in the selected set",
            "Successful message delivery alone is not decision closure",
            "A routine healthy scan is not a reason to finish",
            "After an answer, restart or compaction",
        ):
            self.assertIn(guard, cycle)

    def test_narrow_exceptions_have_no_contradictory_blanket_prohibitions(self) -> None:
        for old_prohibition in (
            "Do not create child agents",
            "perform Git delivery, make new human decisions",
            "outside this role's automatic recovery",
            "continue until all selected Tasks are terminal",
        ):
            self.assertNotIn(old_prohibition, self.text)
        self.assertIn("Other Git writes and operational child agents remain prohibited", self.text)
        self.assertIn("Do not edit product source, configuration, role prompts or Workflow graphs", self.text)
        self.assertIn("arbitrarily move or complete Tasks", self.text)
        self.assertIn("Do not create other children", self.text)
        for heading in (
            "# Operating cycle", "# Grill consultation", "# Authorized PR merge",
            "## Qualified native recovery",
        ):
            self.assertEqual(self.role.count(heading), 1)

    def test_merge_and_recovery_sections_keep_effect_guards_together(self) -> None:
        merge = section(self.role, "# Authorized PR merge")
        for guard in (
            "accepted PR result", "exact repository, PR, base and head",
            "--match-head-commit", "Do not use `--admin`",
            "Head binding does not atomically freeze the base",
            "Queued or auto-merge-enabled is not merged",
            "On a lost response, reconcile the exact PR",
            "not authority to edit source, commit, push",
        ):
            self.assertIn(guard, merge)
        recovery = section(self.role, "## Qualified native recovery")
        for guard in (
            "selected other Task", "no active executor or competing recovery",
            "no pending human gate", "provenance of every required input",
            "A supported edge alone does not qualify a route",
            "project procedure must establish native effect semantics",
            "approval/review and fan-out/Join invariants",
            "Do not supply fabricated success values",
            "one automatic recovery-effect budget",
            "A proved no-op does not consume an effect",
            "Do not enumerate transitions or retry an unknown effect",
        ):
            self.assertIn(guard, recovery)

    def test_consumer_role_and_maintainer_contract_agree_on_boundaries(self) -> None:
        for guard in (
            "whole-selection", "operational", "Grill", "outside Workflow",
            "--match-head-commit", "new_session", "fan-out/Join",
            "one automatic recovery-effect budget",
        ):
            with self.subTest(guard=guard):
                self.assertIn(guard.lower(), self.text.lower())
                self.assertIn(guard.lower(), self.contract.lower())
        self.assertNotIn("contracts/role-contract.md", self.role)
        self.assertNotRegex(self.role, r"(?m)^\s*(model|tools)\s*:")
        self.assertNotIn("task-supervisor-v2", self.role)


if __name__ == "__main__":
    unittest.main()
