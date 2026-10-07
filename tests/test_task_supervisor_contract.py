"""Static contract checks; these do not execute an LLM or native Kent effects."""

import json
from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


def normalized(text: str) -> str:
    return " ".join(text.split())


def section(text: str, heading: str) -> str:
    level = len(heading.split(" ", 1)[0])
    match = re.search(
        rf"(?m)^{re.escape(heading)}\n(?P<body>.*?)(?=^#{{1,{level}}} |\Z)",
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
        cls.runtime = (REPO_ROOT / "agents/runtime-smoke-tester.md").read_text()
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
                "known-futile-resume", "validation-only-script", "provider-effect",
                "late-work-unknown", "alternate-also-noop", "reusable-qualification",
                "running-is-not-noop", "qualified-waiting-reentry",
                "waiting-human-gate", "diagnosis-not-ready", "diagnostic-helper",
                "incompatible-helper", "waiting-resource-owner",
                "helper-persistence", "helper-cleanup-open", "helper-shared-budget",
                "report-deadline", "token-parser-incident", "native-cli-grammar",
                "task-head-not-source-pin", "semantic-outcome",
                "stage-provider-independent", "billing-and-run-limit",
                "owner-git-capability", "stage-alternative-evidence",
                "native-question-and-stop",
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
        self.assertIn("Other Git writes and generic operational children remain prohibited", self.text)
        self.assertIn("Do not edit product source, configuration, role prompts or Workflow graphs", self.text)
        self.assertIn("arbitrarily move or complete Tasks", self.text)
        self.assertIn("Do not create other children", self.text)
        for heading in (
            "# Operating cycle", "# Grill consultation", "# Authorized PR merge",
            "## Qualified native recovery",
            "# Standalone runtime diagnosis", "# Diagnostic qualification",
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

    def test_noop_budget_requires_attempt_settlement_and_bounds_invocations(self) -> None:
        recovery = section(self.role, "## Qualified native recovery")
        for guard in (
            "completed validation-only Script", "exact executed code",
            "relevant dependencies/setup", "actual attempt-specific failure path",
            "no queued, active or deferred late work",
            "settled enqueue/status/audit bookkeeping alone",
            "Provider requests or actual effects spend it",
            "unknown settlement reserves it until reconciled",
            "Failure or cleanup does not erase an effect",
            "A brief actual execution returning to its blocker is not a no-op",
            "at most one equivalent qualified native alternative",
            "Never a second Resume or a third automatic route",
            "even if the alternative also proves no-op",
            "Reuse a matching existing project qualification",
            "prove current settlement separately",
            "Do not falsify a cursor or input provenance",
        ):
            with self.subTest(guard=guard):
                self.assertIn(guard, recovery)
        retained = section(self.role, "## Retained execution")
        self.assertIn("one automatic resume attempt per incident", retained)
        self.assertIn("Diagnose a known same-input failure before Resume", retained)
        self.assertIn("An equivalent failure does not permit another Resume", retained)
        self.assertIn("do not escalate solely because a qualified validation-only", retained)
        self.assertNotIn("blocks further automatic retries", retained)
        self.assertNotIn("The only waiting-state exception", retained)

    def test_waiting_reentry_is_not_gate_or_ownership_bypass(self) -> None:
        recovery = section(self.role, "## Qualified native recovery")
        for guard in (
            "The only waiting-state exception", "`waiting_question`",
            "`waiting_approval`", "exact technical pending condition",
            "retained route is proved incompatible",
            "Prefer ordinary answer/approve and owner/replan routes",
            "Waiting is ownership",
            "settle or supersede the exact old pending object/execution",
            "exclude late answers or competing continuation",
            "before replacement work runs", "required human judgment",
            "independent approval/review", "Any changed route selector",
            "exact provenance and equivalent authorized meaning",
            "unqualified pending-object semantics block reentry",
            "not permission to move around an approval",
            "shares the recovery budget",
        ):
            with self.subTest(guard=guard):
                self.assertIn(guard, recovery)

    def test_supervisor_and_runtime_diagnosis_modes_are_coupled(self) -> None:
        helper = section(self.role, "# Standalone runtime diagnosis")
        for guard in (
            "Outside Workflow", "kent run --agent runtime-smoke-tester",
            "per unchanged incident", "when child depth permits",
            "effective installed role's diagnostic mode",
            "compatible project standalone procedure",
            "own durable minimal state/evidence", "reliable token capture",
            "without Task checkpoint/lifecycle writes",
            "A waiting owner still owns its resources",
            "Do not borrow its Task ID", "default-role fallback",
            "unavailable permitted persistence is a preflight blocker",
            "No product/config/Git writes", "official Smoke PASS",
            "Keep inspecting the whole selection while it runs",
            "A report without cleanup proof leaves an incident open",
            "do not abandon a live child", "restart an equivalent helper",
            "technical recovery uses the same incident budget",
            "remaining run/recovery allowance, prior attempts and unfinished intents",
            "Reserve any delegated recovery attempt",
            "do not recover the same incident concurrently",
            "Source rollout alone does not prove project compatibility",
        ):
            with self.subTest(guard=guard):
                self.assertIn(guard, helper)
        diagnosis = section(self.runtime, "# Standalone diagnosis")
        for guard in (
            "genuine Session identity", "exclusive resource ownership",
            "own durable minimal diagnostic state/evidence",
            "Do not borrow a Task ID", "touch an owner's checkpoint",
            "Do not edit product source/configuration",
            "act on Task lifecycle", "write Task checkpoints",
            "grant waivers or create children",
            "unavailable required persistence does not permit shell file edits",
            "Never use a lost-token recovery path",
            "Reconcile retained intent and effects",
            "shares the supervisor incident's budget",
            "One effective or unsettled recovery attempt",
            "Missing allowance or settlement blocks recovery",
            "continuing permitted read-only observations",
            "recovery actions and their settlement",
            "serialized lease operation",
            "persisted token before runtime actions",
            "processes/kept-open shells before resource release",
            "Never return official Smoke PASS or a Workflow transition",
            "Unknown cleanup leaves an open incident",
        ):
            with self.subTest(guard=guard):
                self.assertIn(guard, diagnosis)
        modes = section(self.runtime, "# Execution modes")
        self.assertIn("defaults apply only to Workflow Smoke", modes)
        self.assertIn("incompatible standalone procedure blocks diagnosis", modes)
        smoke = section(self.runtime, "# Workflow Smoke")
        self.assertIn("Exercise only the runtime scope selected by the workflow gate", smoke)
        self.assertIn("smoke-checkpoint.json", smoke)
        self.assertIn("first-class patch tool", smoke)
        self.assertNotRegex(self.runtime, r"(?m)^\s*(model|tools)\s*:")
        self.assertNotIn("start an operational child", self.text)
        self.assertNotIn("No other children, operational fallback", self.contract)
        self.assertNotIn("Before acquisition establish", diagnosis)

    def test_reporting_and_diagnostics_are_bound_to_their_sections(self) -> None:
        observation = section(self.role, "# Observation and escalation")
        for guard in (
            "report interval and next deadline", "Bound waits by that deadline",
            "report while alive even when progress is unchanged",
            "exception to change-only notifications",
            "without inventing missed reports", "Use the native Question UI",
            "Preserve real human stops and the headless return contract",
        ):
            self.assertIn(guard, observation)
        qualification = section(self.role, "# Diagnostic qualification")
        for guard in (
            "A prerequisite-ready edge", "it is not a diagnostic route",
            "Do not invent a mandatory billing-access gate",
            "Respect accepted spend/run limits", "preflight exact-source readiness",
            "Manual Stage checks do not require provider sandbox",
            "Check the owner's actual capabilities",
            "Alternative test evidence is not a mandatory Smoke PASS",
            "Verify semantic outcome evidence", "Verify CLI grammar",
            "Task-owned commits do not themselves invalidate the initial source pin",
        ):
            self.assertIn(guard, qualification)
        resource = section(self.role, "# Resource recovery")
        self.assertIn("one root-cause incident across new lease, Session and approval IDs", resource)
        self.assertIn("fixture check of the exact owner's command and output grammar", resource)
        evidence = section(self.role, "# Evidence and reporting")
        self.assertIn("plus explicitly scheduled reports", evidence)


if __name__ == "__main__":
    unittest.main()
