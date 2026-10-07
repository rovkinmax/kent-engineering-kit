from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "mobile-login" / "recovery-cases.json"
CONTRACT_PATH = ROOT / "contracts" / "mobile-smoke-contract.md"
ROLE_PATH = ROOT / "agents" / "runtime-smoke-tester.md"

REQUIRED_CASES = {
    "already-authenticated",
    "prefixed-full-value-replacement",
    "separate-country-code",
    "failed-target",
    "unavailable-target",
    "unavailable-focus",
    "failed-clear-proof",
    "unavailable-normalization-proof",
    "unsafe-secret-input-channel",
    "normal-phone-then-code",
    "valid-authorization-reuse",
    "authorization-absent",
    "authorization-expired",
    "authorization-revoked",
    "authorization-out-of-scope",
    "authorization-source-unavailable",
    "phone-submission-unknown",
    "interrupted-observed-code-prompt",
    "attempt-budget-persists-across-sessions",
    "external-only-challenge",
    "explicit-unauthenticated-state",
    "unavailable-code-target",
    "unavailable-code-focus",
    "unsafe-code-input-channel",
    "failed-code-clear-proof",
    "unavailable-code-normalization-proof",
    "code-attempt-budget-exhausted",
    "semantic-assertion-failed",
    "unknown-destination-schema",
    "uncovered-destination-errors",
    "code-entry-only",
    "mcp-transport-failure",
    "mcp-processing-failure",
    "mcp-assertion-failure",
    "nonsemantic-success-signals",
    "attempt-budget-exhausted",
}

REQUIRED_CLAUSES = {
    "MR-01": (
        "reconcile the checkpoint",
        "reuse applicable authorization",
        "does not prove authentication",
    ),
    "MR-02": (
        "target",
        "focus",
        "secret-safe input",
        "before any secret entry",
    ),
    "MR-03": (
        "full-value replacement",
        "clear",
        "country-code",
        "normalization",
        "never append",
    ),
    "MR-04": (
        "secret-bearing",
        "low-entropy hashes",
        "sanitized",
    ),
    "MR-05": (
        "one phone submission",
        "one code submission",
        "unknown outcome",
        "never automatically replay",
    ),
    "MR-06": (
        "authenticated destination",
        "known provider/state schema",
        "interaction evidence",
    ),
    "MR-07": (
        "unknown destination",
        "failed assertion",
        "unauthenticated",
    ),
    "MR-08": (
        "external challenge",
        "do not bypass",
        "genuine missing consent",
    ),
}

CLAUSE_MARKER = re.compile(r"(?m)^### (MR-\d{2})\b[^\n]*")
# Pin complete obligations, not incidental words in headings or retention rules.
# Whitespace/case changes are harmless; normative rewrites require an intentional
# update to this binding and its negative mutation coverage.
MR03_OBLIGATIONS = (
    (
        "project-owned normalization",
        "Derive country-code and number normalization from the project-owned input contract.",
    ),
    (
        "entire-prefix clear before full-value entry",
        "Full-value replacement requires explicitly clearing the entire phone control, "
        "including any existing prefix, before entering the authorized full value.",
    ),
    (
        "no prefix append",
        "Never append a full value to a prefilled prefix.",
    ),
    (
        "separate-control combined normalization before submission",
        "If country-code entry uses a separate control, verify its identity and role, "
        "select the authorized country code, and prove the combined value has the "
        "expected normalization before submission.",
    ),
    (
        "equivalent code-entry prerequisites",
        "Apply the same target, focus, full-value replacement, and normalization proof "
        "to code entry.",
    ),
    (
        "failed or unavailable proof blocks submission",
        "Failed or unavailable clear or normalization proof stops submission.",
    ),
)
SECRET_KEYS = {
    "authorized_full_value",
    "expected_normalized_value",
    "existing_prefix",
    "otp_value",
}

DEFAULTS: dict[str, object] = {
    "authorization": "valid",
    "app_state": "unauthenticated",
    "target": "verified",
    "focus": "verified",
    "safe_input_channel": "safe",
    "phone_clear": "proven",
    "phone_normalization": "proven",
    "phone_attempts_used": 0,
    "phone_submission": "accepted",
    "recovery_state": "not-interrupted",
    "external_challenge": False,
    "code_required": True,
    "code_target": "verified",
    "code_focus": "verified",
    "code_safe_input_channel": "safe",
    "code_clear": "proven",
    "code_normalization": "proven",
    "code_attempts_used": 0,
    "code_submission": "accepted",
    "destination": {
        "schema": "known",
        "errors_covered": True,
        "transport": "passed",
        "processing": "passed",
        "assertion": "true",
        "assertion_kind": "json_boolean",
        "interaction": True,
        "action": "unknown",
        "process_exit": "success",
        "digest_signal": "present",
        "literal_signal": "present",
    },
}


def load_fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def normalized_case(source: dict[str, object]) -> dict[str, object]:
    result = dict(DEFAULTS)
    result.update(source)
    result["destination"] = {
        **DEFAULTS["destination"],  # type: ignore[arg-type]
        **source.get("destination", {}),  # type: ignore[arg-type]
    }
    return result


def replay(source: dict[str, object]) -> dict[str, object]:
    case = normalized_case(source)
    trace = ["reconcile_checkpoint"]
    phone_submissions = 0
    code_submissions = 0
    authorization_questions = 0

    def result(outcome: str) -> dict[str, object]:
        return {
            "outcome": outcome,
            "trace": trace,
            "phone_submissions": phone_submissions,
            "code_submissions": code_submissions,
            "authorization_questions": authorization_questions,
            "secrets_retained": False,
        }

    if case["app_state"] == "authenticated":
        trace.append("observe_authenticated_destination")
        return result("authenticated")

    trace.append("resolve_existing_authorization")
    authorization = case["authorization"]
    if authorization in {"absent", "expired", "revoked", "out-of-scope"}:
        trace.append("request_genuine_consent")
        authorization_questions += 1
        return result("authorization_required")
    if authorization != "valid":
        return result("authorization_unknown")

    if case["phone_attempts_used"]:
        trace.append("inspect_prior_phone_submission")
        recovery_state = case["recovery_state"]
        if recovery_state == "code-prompt":
            trace.append("observe_code_prompt")
        elif recovery_state in {"unknown", "unavailable"}:
            return result("submission_unknown")
        else:
            trace.append("stop_at_phone_attempt_budget")
            return result("attempt_budget_exhausted")
    else:
        trace.append("verify_phone_target")
        if case["target"] != "verified":
            return result("input_prerequisite_failed")
        trace.append("verify_phone_focus")
        if case["focus"] != "verified":
            return result("input_prerequisite_failed")
        trace.append("verify_secret_safe_input_channel")
        if case["safe_input_channel"] != "safe":
            return result("input_prerequisite_failed")
        trace.append("clear_phone_control")
        if case["phone_clear"] != "proven":
            return result("input_prerequisite_failed")
        trace.append("replace_phone_full_value")
        if case.get("country_control") == "separate":
            trace.append("select_country_code")
        trace.append("verify_phone_normalization")
        if case["phone_normalization"] != "proven":
            return result("input_prerequisite_failed")
        trace.append("persist_phone_submission_intent")
        trace.append("submit_phone_once")
        phone_submissions += 1
        trace.append("observe_phone_submission")
        if case["phone_submission"] == "unknown":
            trace.append("inspect_after_unknown_submission")
            if case["recovery_state"] != "code-prompt":
                return result("submission_unknown")
            trace.append("observe_code_prompt")
        elif case["phone_submission"] != "accepted":
            return result("failed_assertion")

    if case["external_challenge"]:
        trace.append("stop_for_external_challenge")
        return result("external_challenge")

    if case["code_attempts_used"]:
        trace.append("stop_at_code_attempt_budget")
        return result("attempt_budget_exhausted")

    trace.append("verify_code_target")
    if case["code_target"] != "verified":
        return result("input_prerequisite_failed")
    trace.append("verify_code_focus")
    if case["code_focus"] != "verified":
        return result("input_prerequisite_failed")
    trace.append("verify_secret_safe_code_input_channel")
    if case["code_safe_input_channel"] != "safe":
        return result("input_prerequisite_failed")
    trace.append("clear_code_control")
    if case["code_clear"] != "proven":
        return result("input_prerequisite_failed")
    trace.append("replace_code_full_value")
    trace.append("verify_code_normalization")
    if case["code_normalization"] != "proven":
        return result("input_prerequisite_failed")
    trace.append("persist_code_submission_intent")
    trace.append("submit_code_once")
    code_submissions += 1
    trace.append("observe_code_submission")
    if case["code_submission"] == "unknown":
        trace.append("inspect_after_unknown_submission")
        return result("submission_unknown")
    if case["code_submission"] != "accepted":
        return result("failed_assertion")

    trace.append("verify_authenticated_destination")
    destination = case["destination"]
    if (
        destination["transport"] != "passed"
        or destination["processing"] != "passed"
        or destination["schema"] != "known"
        or not destination["errors_covered"]
        or not destination["interaction"]
    ):
        return result("unknown_destination")
    if destination.get("semantic_state") == "unauthenticated":
        return result("unauthenticated")
    if destination["assertion"] == "false":
        return result("failed_assertion")
    if (
        destination["assertion"] != "true"
        or destination["assertion_kind"] != "json_boolean"
    ):
        return result("unknown_destination")
    return result("authenticated")


def contract_findings(contents: str) -> list[str]:
    headings = list(CLAUSE_MARKER.finditer(contents))
    clause_text: dict[str, str] = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(contents)
        clause_text[heading.group(1)] = " ".join(
            contents[heading.end() : end].lower().split()
        )

    findings: list[str] = []
    for clause_id, requirements in REQUIRED_CLAUSES.items():
        body = clause_text.get(clause_id)
        if body is None:
            findings.append(f"missing normative login clause {clause_id}")
            continue
        missing = [requirement for requirement in requirements if requirement not in body]
        if missing:
            findings.append(
                f"incomplete normative login clause {clause_id}: "
                + ", ".join(missing)
            )
    if "MR-03" in clause_text:
        sentences = [
            " ".join(sentence.split())
            for sentence in re.split(r"(?<=\.)\s+", clause_text["MR-03"].strip())
        ]
        positions = []
        for obligation, text in MR03_OBLIGATIONS:
            expected = text.lower()
            if sentences.count(expected) != 1:
                findings.append(
                    f"incomplete normative login clause MR-03: {obligation}"
                )
            else:
                positions.append(sentences.index(expected))
        if positions != sorted(positions):
            findings.append(
                "incomplete normative login clause MR-03: prerequisite order"
            )
    return findings


def mutate_clause(
    contents: str,
    clause_id: str,
    old: str,
    new: str,
) -> str:
    headings = list(CLAUSE_MARKER.finditer(contents))
    for index, heading in enumerate(headings):
        if heading.group(1) != clause_id:
            continue
        start = heading.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(contents)
        body = contents[start:end]
        changed, substitutions = re.subn(
            re.escape(old),
            new,
            body,
            flags=re.IGNORECASE,
        )
        if substitutions == 0:
            raise AssertionError(f"{clause_id} does not contain mutation target {old!r}")
        return contents[:start] + changed + contents[end:]
    raise AssertionError(f"missing normative login clause {clause_id}")


class MobileLoginRecoveryReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_fixture()
        self.cases = {
            case["id"]: case
            for case in self.fixture["cases"]  # type: ignore[index]
        }

    def test_fixture_has_closed_synthetic_recovery_matrix(self) -> None:
        self.assertEqual(self.fixture["schema"], "mobile-login-recovery-v1")
        self.assertTrue(self.fixture["synthetic_inputs_only"])
        self.assertEqual(len(self.cases), len(self.fixture["cases"]))  # type: ignore[arg-type]
        self.assertEqual(REQUIRED_CASES - set(self.cases), set())
        self.assertEqual(set(self.cases) - REQUIRED_CASES, set())
        for case in self.cases.values():
            for value in case.get("synthetic_inputs", {}).values():
                self.assertIsInstance(value, str)
                self.assertTrue(value.startswith("opaque-"))
                self.assertNotRegex(value, r"\d{4,}")

    def test_critical_fixture_traces_bind_to_normative_clauses(self) -> None:
        contents = CONTRACT_PATH.read_text(encoding="utf-8")
        bindings = self.fixture["coverage_bindings"]
        self.assertEqual(
            {binding["clause_id"] for binding in bindings},
            set(REQUIRED_CLAUSES),
        )
        trace_profiles = self.fixture["trace_profiles"]
        for binding in bindings:
            with self.subTest(
                clause=binding["clause_id"],
                fixture=binding["fixture_id"],
            ):
                case = self.cases[binding["fixture_id"]]
                observed = replay(case)
                self.assertEqual(
                    observed["trace"],
                    trace_profiles[binding["trace_profile"]],
                )
                prohibited = binding["prohibited_action"]
                self.assertIn(prohibited, case["expected"]["must_not_include"])
                self.assertNotIn(prohibited, observed["trace"])
                clause_id = binding["clause_id"]
                relevant_findings = [
                    finding
                    for finding in contract_findings(contents)
                    if finding.startswith(
                        f"missing normative login clause {clause_id}"
                    )
                    or finding.startswith(
                        f"incomplete normative login clause {clause_id}"
                    )
                ]
                self.assertEqual(relevant_findings, [])

    def test_replay_obeys_expected_outcomes_and_forbidden_actions(self) -> None:
        for case_id, case in self.cases.items():
            with self.subTest(case=case_id):
                observed = replay(case)
                expected = case["expected"]
                self.assertEqual(observed["outcome"], expected["outcome"])
                for action in expected.get("must_include", []):
                    self.assertIn(action, observed["trace"])
                for action in expected.get("must_not_include", []):
                    self.assertNotIn(action, observed["trace"])
                self.assertEqual(
                    observed["phone_submissions"],
                    expected.get("phone_submissions", 0),
                )
                self.assertEqual(
                    observed["code_submissions"],
                    expected.get("code_submissions", 0),
                )
                self.assertEqual(
                    observed["authorization_questions"],
                    expected.get("authorization_questions", 0),
                )
                self.assertFalse(observed["secrets_retained"])
                encoded = json.dumps(observed, sort_keys=True)
                for key in SECRET_KEYS:
                    token = case.get("synthetic_inputs", {}).get(key)
                    if token is not None:
                        self.assertNotIn(token, encoded)

    def test_prefixed_input_is_cleared_and_never_appended(self) -> None:
        observed = replay(self.cases["prefixed-full-value-replacement"])
        trace = observed["trace"]
        self.assertLess(trace.index("clear_phone_control"), trace.index("replace_phone_full_value"))
        self.assertLess(
            trace.index("replace_phone_full_value"),
            trace.index("verify_phone_normalization"),
        )
        self.assertLess(
            trace.index("verify_phone_normalization"),
            trace.index("submit_phone_once"),
        )
        self.assertNotIn("append_phone_to_prefix", trace)

    def test_unknown_or_interrupted_submission_is_never_replayed(self) -> None:
        unknown = replay(self.cases["phone-submission-unknown"])
        self.assertEqual(unknown["outcome"], "submission_unknown")
        self.assertEqual(unknown["phone_submissions"], 1)
        self.assertNotIn("submit_phone_once", unknown["trace"][unknown["trace"].index("observe_phone_submission") + 1 :])

        resumed = replay(self.cases["interrupted-observed-code-prompt"])
        self.assertEqual(resumed["outcome"], "authenticated")
        self.assertEqual(resumed["phone_submissions"], 0)
        self.assertEqual(resumed["code_submissions"], 1)
        self.assertEqual(resumed["trace"].count("inspect_prior_phone_submission"), 1)

    def test_nonsemantic_success_signals_never_prove_authentication(self) -> None:
        observed = replay(self.cases["nonsemantic-success-signals"])
        self.assertEqual(observed["outcome"], "unknown_destination")
        self.assertEqual(observed["phone_submissions"], 1)
        self.assertEqual(observed["code_submissions"], 1)


class MobileLoginNormativeContractTest(unittest.TestCase):
    def test_role_resolves_contract_from_supported_consumer_installation(self) -> None:
        # Reproduce scripts/install's role symlink, not an actual installation.
        # The consumer has no contracts directory and runs outside the Kit.
        role = ROLE_PATH.read_text(encoding="utf-8")
        checks = re.findall(r"```python\n(.*?)\n```", role, re.DOTALL)
        self.assertEqual(len(checks), 1, "role needs an executable resolution check")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer = root / "consumer"
            consumer.mkdir()
            home = root / "home"
            for name, explicit_root in (("default", False), ("custom root", True)):
                with self.subTest(persistence=name):
                    persistence = (
                        root / name if explicit_root else home / ".kent"
                    )
                    agents = persistence / "agents"
                    agents.mkdir(parents=True)
                    installed_role = agents / ROLE_PATH.name
                    installed_role.symlink_to(ROLE_PATH)
                    self.assertEqual(installed_role.resolve(), ROLE_PATH)
                    self.assertFalse((consumer / "contracts").exists())
                    self.assertFalse((persistence / "contracts").exists())
                    environment = {
                        key: value for key, value in os.environ.items()
                        if key not in {"KENT_PERSISTENCE_ROOT", "HOME"}
                    }
                    environment["HOME"] = str(home)
                    if explicit_root:
                        environment["KENT_PERSISTENCE_ROOT"] = str(persistence)

                    def resolve(check: str = checks[0]) -> subprocess.CompletedProcess[str]:
                        return subprocess.run(
                            [sys.executable, "-c", check],
                            cwd=consumer,
                            env=environment,
                            capture_output=True,
                            text=True,
                            timeout=10,
                            check=False,
                        )

                    result = resolve()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), str(CONTRACT_PATH))
                    resolved = Path(result.stdout.strip())
                    self.assertEqual(contract_findings(resolved.read_text()), [])

                    # Reject the original consumer-relative dependency, even
                    # though its spelling names the canonical contract.
                    relative = checks[0].replace(
                        "role.resolve(strict=True).parents[1]",
                        "Path.cwd()",
                    )
                    self.assertNotEqual(relative, checks[0])
                    result = resolve(relative)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")

                    # A missing source fails closed rather than resolving a
                    # coincidental contract relative to the consumer.
                    installed_role.unlink()
                    installed_role.symlink_to(root / "missing-kit" / "agents" / ROLE_PATH.name)
                    result = resolve()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")

                    # A role source alone is not proof of an available contract.
                    kit = root / name / "incomplete kit"
                    source_role = kit / "agents" / ROLE_PATH.name
                    source_role.parent.mkdir(parents=True)
                    source_role.write_text(role, encoding="utf-8")
                    installed_role.unlink()
                    installed_role.symlink_to(source_role)
                    result = resolve()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    procedure = kit / "contracts" / CONTRACT_PATH.name
                    procedure.parent.mkdir()
                    procedure.touch()
                    result = resolve()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")

    def test_mr03_binding_tolerates_layout_not_obligation_order_changes(self) -> None:
        complete = CONTRACT_PATH.read_text(encoding="utf-8")
        self.assertEqual(contract_findings(complete.upper()), [])
        reflowed = complete
        for _, text in MR03_OBLIGATIONS:
            # Reflow each pinned sentence without changing its obligations.
            pattern = re.escape(text).replace(r"\ ", r"\s+")
            wrapped = re.sub(
                pattern,
                text.replace(" ", "\n"),
                complete,
                flags=re.IGNORECASE,
            )
            self.assertEqual(contract_findings(wrapped), [])
            reflowed = re.sub(pattern, text, reflowed, flags=re.IGNORECASE)
            self.assertEqual(contract_findings(reflowed), [])

        first = MR03_OBLIGATIONS[1][1]
        second = MR03_OBLIGATIONS[3][1]
        swapped = mutate_clause(reflowed, "MR-03", first, "OBLIGATION_PLACEHOLDER.")
        swapped = mutate_clause(swapped, "MR-03", second, first)
        swapped = mutate_clause(swapped, "MR-03", "OBLIGATION_PLACEHOLDER.", second)
        self.assertIn(
            "incomplete normative login clause MR-03: prerequisite order",
            contract_findings(swapped),
        )

    def test_mr03_rejects_keyword_preserving_obligation_mutations(self) -> None:
        complete = CONTRACT_PATH.read_text(encoding="utf-8")
        clear = (
            "Full-value replacement requires explicitly clearing the entire\n"
            "phone control, including any existing prefix, before entering the authorized\n"
            "full value."
        )
        combined = (
            "If country-code\n"
            "entry uses a separate control, verify its identity and role, select the\n"
            "authorized country code, and prove the combined value has the expected\n"
            "normalization before submission."
        )
        stop = "Failed or unavailable\nclear or normalization proof stops submission."
        mutations = {
            "clear-removed-with-keyword-decoy": (clear, "Clear is a historical keyword."),
            "prefix-retained": (
                clear,
                "Full-value replacement may leave the existing prefix in the phone "
                "control; clear is optional before entering the authorized full value.",
            ),
            "clear-negated": (clear, clear.replace("requires", "does not require")),
            "clear-after-entry": (
                clear,
                clear.replace("before entering", "after entering"),
            ),
            "combined-proof-removed": (combined, ""),
            "combined-proof-negated": (
                combined,
                combined.replace("and prove", "and do not prove"),
            ),
            "combined-proof-after-submission": (
                combined,
                combined.replace("before submission", "after submission"),
            ),
            "failed-proof-permits-submission": (
                stop,
                stop.replace("stops submission", "permits submission"),
            ),
        }
        for name, (old, new) in mutations.items():
            with self.subTest(mutation=name):
                changed = mutate_clause(complete, "MR-03", old, new)
                self.assertTrue(
                    any(
                        finding.startswith("incomplete normative login clause MR-03")
                        for finding in contract_findings(changed)
                    )
                )

    def test_canonical_contract_contains_every_recovery_clause(self) -> None:
        contents = CONTRACT_PATH.read_text(encoding="utf-8")
        self.assertEqual(contract_findings(contents), [])

    def test_contract_assertions_detect_critical_rule_removals(self) -> None:
        complete = CONTRACT_PATH.read_text(encoding="utf-8")
        if contract_findings(complete):
            self.skipTest("canonical clauses are absent in the expected pre-edit baseline")
        mutations = {
            "clear": mutate_clause(complete, "MR-03", "clear", "reset"),
            "normalization": mutate_clause(
                complete,
                "MR-03",
                "normalization",
                "formatting",
            ),
            "no-replay": mutate_clause(
                complete,
                "MR-05",
                "never automatically replay",
                "automatically retry",
            ),
            "destination-proof": mutate_clause(
                complete,
                "MR-06",
                "authenticated destination",
                "destination",
            ),
        }
        expected_clauses = {
            "clear": "MR-03",
            "normalization": "MR-03",
            "no-replay": "MR-05",
            "destination-proof": "MR-06",
        }
        for mutation, changed in mutations.items():
            with self.subTest(rule=mutation):
                self.assertTrue(
                    any(
                        finding.startswith(f"incomplete normative login clause {expected_clauses[mutation]}")
                        for finding in contract_findings(changed)
                    )
                )

    def test_runtime_role_delegates_to_canonical_login_procedure(self) -> None:
        role = ROLE_PATH.read_text(encoding="utf-8").lower()
        self.assertIn("canonical mobile login recovery procedure", role)
        self.assertIn("contracts/mobile-smoke-contract.md", role)


if __name__ == "__main__":
    unittest.main()
