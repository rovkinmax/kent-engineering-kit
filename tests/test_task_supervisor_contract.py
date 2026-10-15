"""Static invariants and walkthrough fixture integrity, not model execution."""

import json
from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE = "references/task-supervision-operations.md"


def normalized(text: str) -> str:
    return " ".join(text.split())


def section(text: str, heading: str) -> str:
    match = re.search(
        rf"(?m)^# {re.escape(heading)}\n(?P<body>.*?)(?=^# |\Z)",
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
        cls.skill_root = REPO_ROOT / "skills/kent-engineering-kit"
        cls.reference = (cls.skill_root / REFERENCE).read_text()

    def test_mechanics_reference_is_packaged_and_discoverable(self) -> None:
        self.assertIn(REFERENCE, self.role)
        self.assertIn(REFERENCE, (self.skill_root / "SKILL.md").read_text())
        self.assertTrue(self.reference.strip())
        self.assertNotIn("contracts/role-contract.md", self.role)
        self.assertNotRegex(self.role, r"(?m)^\s*(model|tools)\s*:")

    def test_assignment_retains_authority_and_product_ownership(self) -> None:
        assignment = section(self.role, "Assignment and judgment")
        for invariant in (
            "original human decisions",
            "authority, capability, factual readiness and ownership",
            "mandatory independent human judgments",
            "Do not edit product source",
            "directly mutate Kent's database",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, assignment)

    def test_delegation_permits_ordinary_operators_without_workflow_bypass(self) -> None:
        execution = section(self.role, "Organize execution")
        self.assertIn("explicitly selected ordinary idle operator", execution)
        self.assertIn("launch an appropriate configured helper", execution)
        self.assertIn("Never directly continue a Workflow-owned Session", execution)
        self.assertIn("Delegation adds no authority", execution)
        self.assertIn("Serialize writers", execution)
        self.assertIn("Separate observer wait time from executor deadlines", execution)
        self.assertNotIn("generic operational children remain prohibited", self.role)

    def test_recovery_preserves_settlement_and_real_prerequisites(self) -> None:
        recovery = section(self.role, "Recover with evidence")
        for invariant in (
            "Honor deliberate human stops",
            "supported new-session entry",
            "pending decision settlement",
            "fan-out/Join",
            "A readiness edge requires the blocker resolved",
            "Do not invent PASS or a product defect",
            "Unknown effects block replay",
            "within existing run/spend/risk limits",
            "Never borrow Task/Session identity",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, recovery)
        self.assertNotIn("one automatic recovery-effect budget", self.role)
        self.assertNotIn("Never a second Resume or a third automatic route", self.role)

    def test_observation_distinguishes_followup_from_pause_and_completion(self) -> None:
        observation = section(self.role, "Escalation and observation")
        for invariant in (
            "Preserve the supervision assignment through focused follow-ups",
            "Respect explicit pauses",
            "Preserve requested report cadence",
            "not indefinite frequent polling",
            "Respect an explicit request to keep monitoring",
            "No automatic wakeup",
            "published, merged and installed",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, observation)

    def test_writer_roles_share_the_bounded_human_checkpoint_exception(self) -> None:
        exception = (
            "A bounded local preservation/checkpoint commit may also be authorized by "
            "an explicit original human decision."
        )
        for name in ("implementation-worker", "fix-worker"):
            with self.subTest(role=name):
                text = normalized((REPO_ROOT / f"agents/{name}.md").read_text())
                self.assertIn(exception, text)
                self.assertIn("file set, accepted plan and single-writer ownership", text)
                self.assertIn("preserve project/node prohibitions", text)
                self.assertIn(
                    "This exception grants no rebase, push, merge, publication", text
                )

    def test_runtime_helper_preserves_budget_ownership_and_cleanup(self) -> None:
        runtime = (REPO_ROOT / "agents/runtime-smoke-tester.md").read_text()
        diagnosis = section(runtime, "Standalone diagnosis")
        for invariant in (
            "compatible project procedure",
            "Do not borrow a Task ID",
            "Do not edit product source/configuration",
            "Unknown effects block replay",
            "caller's existing limits",
            "Task lifecycle recovery remains with the coordinator",
            "Unknown cleanup leaves an open incident",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, diagnosis)
        self.assertNotIn("One effective or unsettled recovery attempt", diagnosis)

    def test_native_reference_does_not_claim_atomic_targeting(self) -> None:
        text = normalized(self.reference)
        self.assertIn("not an atomic compare-and-set", text)
        self.assertIn("not a distributed lock", text)
        self.assertIn("--match-head-commit", text)
        self.assertIn("Do not test uncertain movement semantics on a live Task", text)
        self.assertIn(
            "serialized acquisition, then verifies exclusive ownership", text
        )

    def test_requested_endpoint_survives_blockers_and_status_questions(self) -> None:
        assignment = section(self.role, "Assignment and judgment")
        for invariant in (
            "without adding an alternative",
            "audit-only, pause or handoff assignments",
            "does not replace an ongoing delivery assignment",
            "existing applicable PR",
            "draft/open-PR",
            "does not authorize merge or release",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, assignment)
        observation = section(self.role, "Escalation and observation")
        for invariant in (
            "Do not call `kent goal complete`",
            "requested endpoint is unmet",
            "Explicit human cancellation or scope reduction",
            "leave the goal unfinished",
            "Do not manufacture a user decision",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, observation)

    def test_technical_blocker_requires_owned_bounded_action(self) -> None:
        recovery = section(self.role, "Recover with evidence")
        for invariant in (
            "named capable executor",
            "verify its actual start",
            "follow through to settlement",
            "is not an execution handoff",
            "Reuse qualified capabilities",
            "bounded, decision-ready proposal",
            "ask only for the missing decision",
            "does not waive review, budgets or human-only endpoints",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, recovery)

    def test_walkthrough_fixtures_are_valid_not_an_executable_policy_oracle(self) -> None:
        all_ids = set()
        for name in ("supervisor-operational-cases", "supervisor-decision-closure"):
            data = json.loads((REPO_ROOT / f"tests/fixtures/{name}.json").read_text())
            with self.subTest(fixture=name):
                self.assertEqual(set(data), {"description", "cases"})
                self.assertIn("not execution", data["description"])
                identifiers = set()
                categories = set()
                for case in data["cases"]:
                    self.assertEqual(set(case), {"id", "input", "expected", "category"})
                    self.assertRegex(case["id"], r"^[a-z][a-z0-9-]*$")
                    self.assertNotIn(case["id"], identifiers)
                    identifiers.add(case["id"])
                    self.assertTrue(case["input"].strip())
                    self.assertTrue(case["expected"].strip())
                    self.assertIn(case["category"], {"allow", "protect", "capability"})
                    categories.add(case["category"])
                self.assertEqual(categories, {"allow", "protect", "capability"})
                all_ids.update(identifiers)
        self.assertTrue(
            {
                "authorized-helper", "selected-idle-operator", "human-checkpoint",
                "workflow-session", "ambiguous-effect", "renamed-hypothesis",
                "deliberate-stop", "fanout-invariants", "diagnosis-not-ready",
                "helper-shared-budget", "checkpoint-prohibition",
                "executor-deadline", "no-progress-producer",
                "routine-intervention", "default-role-cohort",
                "available-resource-helper",
                "pr-endpoint-blocked", "delivery-status-question",
                "technical-repair-owner", "exhausted-retained-owner",
                "explicit-audit-endpoint", "existing-consent-after-cancel",
                "human-only-wait", "capability-exhausted-goal",
                "healthy-delivery-writer",
            }.issubset(all_ids)
        )


if __name__ == "__main__":
    unittest.main()
