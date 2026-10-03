from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from adapters.gradle.story_change_evidence import (
    Capability,
    ExpectedContext,
    Status,
    StoryEvidenceError,
    _safe_json_input,
    evaluate_evidence,
)


BASELINE = "a" * 40
REVISION = "b" * 40
DIFF_SHA256 = "c" * 64
IDENTITY = {
    "group": "Settings",
    "component": "EmptyState",
    "style": "Default",
}


class StoryChangeEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self._write("src/SettingsEmptyStory.kt", "Preview: empty settings\n")
        self._write("build/story-change.patch", "added SettingsEmptyStory\n")
        self._write(
            "build/showkase-metadata.json",
            json.dumps({"actual_registry_fixture": [IDENTITY]}),
        )
        self._write(
            "build/paparazzi-results.xml",
            "<testsuite tests='1' failures='0' />\n",
        )
        self._write(
            "build/keyboard-interaction.xml",
            "<testsuite tests='1' failures='0' />\n",
        )
        self.adapter_script = Path(
            sys.modules["adapters.gradle.story_change_evidence"].__file__
        ).resolve()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _sha256(self, relative: str) -> str:
        return hashlib.sha256(
            (self.root / relative).read_bytes()
        ).hexdigest()

    def expected(
        self,
        *,
        project_id: str = "appsome",
        capability: Capability = Capability.CONNECTED,
        permit_deferred: bool = False,
        baseline: str = BASELINE,
        revision: str = REVISION,
        diff_sha256: str = DIFF_SHA256,
    ) -> ExpectedContext:
        return ExpectedContext(
            workspace_root=self.root,
            project_id=project_id,
            capability=capability,
            permit_deferred_not_connected=permit_deferred,
            baseline=baseline,
            revision=revision,
            diff_sha256=diff_sha256,
        )

    def _check(
        self,
        *,
        check_id: str,
        kind: str,
        artifact: str,
        story_ids: list[str] | None = None,
        outcome: str = "passed",
        baseline: str = BASELINE,
        revision: str = REVISION,
        diff_sha256: str = DIFF_SHA256,
    ) -> dict[str, object]:
        command = (
            "./tools/agentw :feature:storybook:verifyPaparazziDebug"
        )
        if kind == "registry":
            command = "Showkase.getMetadata().componentList assertion"
        return {
            "id": check_id,
            "kind": kind,
            "command": command,
            "outcome": outcome,
            "artifact": artifact,
            "baseline": baseline,
            "revision": revision,
            "diff_sha256": diff_sha256,
            "story_ids": story_ids or [],
        }

    def packet(self) -> dict[str, object]:
        story_id = "settings-empty"
        registry_artifact = "build/showkase-metadata.json"
        story_artifact = "build/paparazzi-results.xml"
        return {
            "schema": "story-change-evidence-v1",
            "project_id": "appsome",
            "capability": "connected",
            "applicability": "ui",
            "baseline": BASELINE,
            "revision": REVISION,
            "diff_sha256": DIFF_SHA256,
            "reason": None,
            "states": [
                {
                    "id": "settings-empty",
                    "support": "supported",
                    "reason": None,
                    "story_ids": [story_id],
                    "alternative_evidence": [],
                }
            ],
            "registry": {
                "artifact": registry_artifact,
                "check_id": "showkase",
                "identities": [IDENTITY.copy()],
            },
            "stories": [
                {
                    "id": story_id,
                    "change": "added",
                    "source": "src/SettingsEmptyStory.kt",
                    "source_sha256": self._sha256(
                        "src/SettingsEmptyStory.kt"
                    ),
                    "change_evidence": "build/story-change.patch",
                    "change_evidence_sha256": self._sha256(
                        "build/story-change.patch"
                    ),
                    "identity": IDENTITY.copy(),
                    "check_ids": ["paparazzi"],
                }
            ],
            "checks": [
                self._check(
                    check_id="showkase",
                    kind="registry",
                    artifact=registry_artifact,
                ),
                self._check(
                    check_id="paparazzi",
                    kind="story",
                    artifact=story_artifact,
                    story_ids=[story_id],
                ),
            ],
        }

    def _set_non_ui(self, packet: dict[str, object]) -> None:
        packet.update(
            {
                "applicability": "non_ui",
                "reason": "The verified task diff contains no UI source changes.",
                "states": [],
                "registry": None,
                "stories": [],
                "checks": [],
            }
        )

    def _set_deferred(self, packet: dict[str, object]) -> None:
        packet.update(
            {
                "project_id": "puber",
                "capability": "not_connected",
                "applicability": "ui",
                "reason": (
                    "Story registration is deferred until Puber connects an "
                    "approved catalog capability."
                ),
                "states": [
                    {
                        "id": "profile-empty",
                        "support": "unknown",
                        "reason": None,
                        "story_ids": [],
                        "alternative_evidence": [],
                    }
                ],
                "registry": None,
                "stories": [],
                "checks": [],
            }
        )

    def _cli_context(
        self,
        *,
        project_id: str = "appsome",
        capability: str = "connected",
        permit_deferred: bool = False,
    ) -> dict[str, object]:
        return {
            "schema": "story-change-expected-context-v1",
            "project_id": project_id,
            "capability": capability,
            "permit_deferred_not_connected": permit_deferred,
            "baseline": BASELINE,
            "revision": REVISION,
            "diff_sha256": DIFF_SHA256,
        }

    def _run_cli(
        self,
        packet: dict[str, object],
        *,
        context: dict[str, object] | None = None,
        context_path: str = "context.json",
        evidence_path: str = "evidence.json",
    ) -> subprocess.CompletedProcess[str]:
        self._write(
            "context.json",
            json.dumps(context or self._cli_context(), ensure_ascii=False),
        )
        self._write(
            "evidence.json",
            json.dumps(packet, ensure_ascii=False),
        )
        return subprocess.run(
            [
                sys.executable,
                str(self.adapter_script),
                "--context",
                context_path,
                "--evidence",
                evidence_path,
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )

    def _cli_result(
        self,
        result: subprocess.CompletedProcess[str],
    ) -> dict[str, object]:
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(
            set(payload),
            {"schema", "status", "reasons", "artifacts"},
        )
        self.assertEqual(payload["schema"], "story-change-evaluation-v1")
        self.assertNotIn("story_pass", payload)
        return payload

    def test_added_registered_story_with_fresh_check_is_evidence_ready(self) -> None:
        result = evaluate_evidence(self.expected(), self.packet())
        self.assertEqual(result.status, Status.EVIDENCE_READY)
        self.assertIn("build/showkase-metadata.json", result.artifacts)
        self.assertIn("build/paparazzi-results.xml", result.artifacts)
        self.assertNotIn("story_pass", result.to_dict())

    def test_meaningful_update_to_existing_registered_story_is_allowed(self) -> None:
        packet = self.packet()
        packet["stories"][0]["change"] = "updated"
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.EVIDENCE_READY)

    def test_unchanged_existing_story_execution_does_not_satisfy_change(self) -> None:
        packet = self.packet()
        packet["stories"][0]["change"] = "unchanged"
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.FAILED)

    def test_ordinary_preview_without_registry_membership_fails(self) -> None:
        packet = self.packet()
        packet["registry"]["identities"] = []
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.FAILED)

    def test_supported_state_without_story_mapping_fails(self) -> None:
        packet = self.packet()
        packet["states"][0]["story_ids"] = []
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.FAILED)

    def test_missing_story_check_fails(self) -> None:
        packet = self.packet()
        packet["checks"] = packet["checks"][:1]
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.FAILED)

    def test_failed_story_check_fails(self) -> None:
        packet = self.packet()
        packet["checks"][1]["outcome"] = "failed"
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.FAILED)

    def test_stale_packet_identity_fields_fail(self) -> None:
        for field in ("baseline", "revision", "diff_sha256"):
            with self.subTest(field=field):
                packet = self.packet()
                packet[field] = "d" * len(packet[field])
                result = evaluate_evidence(self.expected(), packet)
                self.assertEqual(result.status, Status.FAILED)

    def test_stale_story_check_fails(self) -> None:
        stale_values = {
            "baseline": "d" * 40,
            "revision": "e" * 40,
            "diff_sha256": "f" * 64,
        }
        for field, value in stale_values.items():
            with self.subTest(field=field):
                packet = self.packet()
                packet["checks"][1][field] = value
                result = evaluate_evidence(self.expected(), packet)
                self.assertEqual(result.status, Status.FAILED)

    def test_stale_registry_check_fails(self) -> None:
        stale_values = {
            "baseline": "d" * 40,
            "revision": "e" * 40,
            "diff_sha256": "f" * 64,
        }
        for field, value in stale_values.items():
            with self.subTest(field=field):
                packet = self.packet()
                packet["checks"][0][field] = value
                result = evaluate_evidence(self.expected(), packet)
                self.assertEqual(result.status, Status.FAILED)

        packet = self.packet()
        packet["checks"][0]["artifact"] = "build/paparazzi-results.xml"
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.FAILED)

    def test_non_ui_change_requires_reason_and_is_not_applicable(self) -> None:
        packet = self.packet()
        self._set_non_ui(packet)
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.NOT_APPLICABLE)

        packet["reason"] = None
        self.assertEqual(
            evaluate_evidence(self.expected(), packet).status,
            Status.FAILED,
        )

    def test_genuinely_unsupported_state_requires_alternative_evidence(self) -> None:
        packet = self.packet()
        packet.update(
            {
                "reason": None,
                "states": [
                    {
                        "id": "system-keyboard",
                        "support": "unsupported",
                        "reason": "Runtime keyboard behavior is outside the catalog.",
                        "story_ids": [],
                        "alternative_evidence": [
                            "build/keyboard-interaction.xml"
                        ],
                    }
                ],
                "registry": None,
                "stories": [],
                "checks": [],
            }
        )
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.UNSUPPORTED)
        self.assertIn("build/keyboard-interaction.xml", result.artifacts)

    def test_unsupported_state_without_reason_or_alternative_fails(self) -> None:
        packet = self.packet()
        packet.update(
            {
                "states": [
                    {
                        "id": "system-keyboard",
                        "support": "unsupported",
                        "reason": None,
                        "story_ids": [],
                        "alternative_evidence": [],
                    }
                ],
                "registry": None,
                "stories": [],
                "checks": [],
            }
        )
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.FAILED)

    def test_unsupported_state_with_missing_alternative_artifact_fails(self) -> None:
        packet = self.packet()
        packet.update(
            {
                "states": [
                    {
                        "id": "system-keyboard",
                        "support": "unsupported",
                        "reason": "Runtime keyboard behavior is outside the catalog.",
                        "story_ids": [],
                        "alternative_evidence": ["build/missing-interaction.xml"],
                    }
                ],
                "registry": None,
                "stories": [],
                "checks": [],
            }
        )
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.FAILED)

    def test_mixed_supported_and_unsupported_states_preserve_both(self) -> None:
        packet = self.packet()
        packet["states"].append(
            {
                "id": "system-keyboard",
                "support": "unsupported",
                "reason": "Runtime keyboard behavior is outside the catalog.",
                "story_ids": [],
                "alternative_evidence": ["build/keyboard-interaction.xml"],
            }
        )
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.EVIDENCE_READY)
        self.assertIn("build/keyboard-interaction.xml", result.artifacts)

    def test_two_states_may_reference_the_same_story(self) -> None:
        packet = self.packet()
        packet["states"].append(
            {
                "id": "settings-empty-accessibility",
                "support": "supported",
                "reason": None,
                "story_ids": ["settings-empty"],
                "alternative_evidence": [],
            }
        )
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.EVIDENCE_READY)

    def test_duplicate_story_definitions_are_ambiguous_and_fail(self) -> None:
        packet = self.packet()
        duplicate = dict(packet["stories"][0])
        duplicate["id"] = "duplicate-definition"
        packet["stories"].append(duplicate)
        packet["states"][0]["story_ids"].append("duplicate-definition")
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.FAILED)

    def test_deferred_puber_is_visible_and_never_story_pass(self) -> None:
        packet = self.packet()
        self._set_deferred(packet)
        result = evaluate_evidence(
            self.expected(
                project_id="puber",
                capability=Capability.NOT_CONNECTED,
                permit_deferred=True,
            ),
            packet,
        )
        self.assertEqual(result.status, Status.NOT_CONNECTED)
        self.assertIn("deferred", " ".join(result.reasons).lower())
        self.assertNotIn("story_pass", result.to_dict())

    def test_deferred_puber_is_not_connected_not_applicable_or_pass(self) -> None:
        packet = self.packet()
        self._set_deferred(packet)
        result = self._run_cli(
            packet,
            context=self._cli_context(
                project_id="puber",
                capability="not_connected",
                permit_deferred=True,
            ),
        )
        payload = self._cli_result(result)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["status"], "not_connected")

    def test_connected_puber_uses_the_same_strict_contract(self) -> None:
        packet = self.packet()
        packet["project_id"] = "puber"
        result = evaluate_evidence(
            self.expected(project_id="puber", capability=Capability.CONNECTED),
            packet,
        )
        self.assertEqual(result.status, Status.EVIDENCE_READY)

    def test_puber_deferral_fails_without_expected_policy_permission(self) -> None:
        packet = self.packet()
        self._set_deferred(packet)
        result = evaluate_evidence(
            self.expected(
                project_id="puber",
                capability=Capability.NOT_CONNECTED,
                permit_deferred=False,
            ),
            packet,
        )
        self.assertEqual(result.status, Status.FAILED)

    def test_connected_context_rejects_packet_claiming_not_connected(self) -> None:
        packet = self.packet()
        self._set_deferred(packet)
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.FAILED)

    def test_expected_context_identity_rejects_coherent_wrong_packet(self) -> None:
        packet = self.packet()
        packet["project_id"] = "other-project"
        packet["baseline"] = "e" * 40
        packet["revision"] = "f" * 40
        packet["diff_sha256"] = "1" * 64
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.FAILED)

    def test_wrong_project_expected_context_cannot_be_packet_selected(self) -> None:
        packet = self.packet()
        result = evaluate_evidence(
            self.expected(project_id="puber"),
            packet,
        )
        self.assertEqual(result.status, Status.FAILED)

    def test_escaped_artifact_path_is_rejected(self) -> None:
        packet = self.packet()
        packet["stories"][0]["source"] = "../outside/Story.kt"
        with self.assertRaises(StoryEvidenceError):
            evaluate_evidence(self.expected(), packet)

    def test_symlink_artifact_escaping_workspace_is_rejected(self) -> None:
        outside = Path(self.temporary_directory.name).parent / (
            Path(self.temporary_directory.name).name + "-outside"
        )
        outside.write_text("outside\n", encoding="utf-8")
        try:
            (self.root / "outside-link").symlink_to(outside)
            packet = self.packet()
            packet["stories"][0]["source"] = "outside-link"
            with self.assertRaises(StoryEvidenceError):
                evaluate_evidence(self.expected(), packet)
        finally:
            outside.unlink(missing_ok=True)

    def test_story_source_and_change_artifact_hashes_are_verified(self) -> None:
        packet = self.packet()
        packet["stories"][0]["source_sha256"] = "9" * 64
        self.assertEqual(
            evaluate_evidence(self.expected(), packet).status,
            Status.FAILED,
        )

        packet = self.packet()
        packet["stories"][0]["change_evidence_sha256"] = "8" * 64
        self.assertEqual(
            evaluate_evidence(self.expected(), packet).status,
            Status.FAILED,
        )

    def test_unknown_fields_and_unbounded_state_lists_are_rejected(self) -> None:
        packet = self.packet()
        packet["story_pass"] = True
        with self.assertRaises(StoryEvidenceError):
            evaluate_evidence(self.expected(), packet)

    def test_enum_values_must_be_strings_and_known(self) -> None:
        malformed_packets = []
        packet = self.packet()
        packet["capability"] = []
        malformed_packets.append(packet)

        packet = self.packet()
        packet["applicability"] = {}
        malformed_packets.append(packet)

        packet = self.packet()
        packet["states"][0]["support"] = []
        malformed_packets.append(packet)

        packet = self.packet()
        packet["stories"][0]["change"] = {}
        malformed_packets.append(packet)

        packet = self.packet()
        packet["checks"][0]["kind"] = []
        malformed_packets.append(packet)

        packet = self.packet()
        packet["checks"][0]["outcome"] = {}
        malformed_packets.append(packet)

        for packet in malformed_packets:
            with self.subTest(packet=packet):
                with self.assertRaises(StoryEvidenceError):
                    evaluate_evidence(self.expected(), packet)

        packet = self.packet()
        packet["capability"] = "future-capability"
        with self.assertRaises(StoryEvidenceError):
            evaluate_evidence(self.expected(), packet)

    def test_repeated_mapping_references_are_allowed(self) -> None:
        packet = self.packet()
        packet["states"][0]["story_ids"] = [
            "settings-empty",
            "settings-empty",
        ]
        packet["checks"][1]["story_ids"] = [
            "settings-empty",
            "settings-empty",
        ]
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.EVIDENCE_READY)

    def test_missing_retained_check_artifact_fails_required_evidence(self) -> None:
        packet = self.packet()
        packet["checks"][1]["artifact"] = "build/missing-paparazzi.xml"
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.FAILED)

        packet = self.packet()
        packet["states"] = [
            {
                "id": f"state-{index}",
                "support": "supported",
                "reason": None,
                "story_ids": ["settings-empty"],
                "alternative_evidence": [],
            }
            for index in range(129)
        ]
        with self.assertRaises(StoryEvidenceError):
            evaluate_evidence(self.expected(), packet)

    def test_unknown_story_check_reference_returns_failed(self) -> None:
        for story_ids in (
            ["missing-story"],
            ["missing-story", "settings-empty"],
        ):
            with self.subTest(story_ids=story_ids):
                packet = self.packet()
                packet["checks"][1]["story_ids"] = story_ids
                result = evaluate_evidence(self.expected(), packet)
                self.assertEqual(result.status, Status.FAILED)
                self.assertIn(
                    "story check references an unknown story", result.reasons
                )

    def test_unknown_story_reference_preserves_other_check_diagnostics(self) -> None:
        packet = self.packet()
        packet["checks"][1]["story_ids"] = ["missing-story", "settings-empty"]
        packet["stories"][0]["check_ids"] = []
        result = evaluate_evidence(self.expected(), packet)
        self.assertEqual(result.status, Status.FAILED)
        self.assertIn("story check references an unknown story", result.reasons)
        self.assertIn("story check mapping is not bidirectional", result.reasons)

    def test_unknown_story_check_reference_cli_returns_closed_failed_json(self) -> None:
        packet = self.packet()
        packet["checks"][1]["story_ids"].append("missing-story")
        result = self._run_cli(packet)
        payload = self._cli_result(result)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertIn(
            "story check references an unknown story", payload["reasons"]
        )

    def test_connected_evidence_ready_cli_exit_is_not_story_pass(self) -> None:
        result = self._run_cli(self.packet())
        payload = self._cli_result(result)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["status"], "evidence_ready")

    def test_missing_required_evidence_cli_exit_is_one(self) -> None:
        packet = self.packet()
        packet["states"][0]["story_ids"] = []
        result = self._run_cli(packet)
        payload = self._cli_result(result)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["status"], "failed")

    def test_malformed_or_escaped_cli_input_exits_two(self) -> None:
        result = self._run_cli(
            self.packet(),
            context_path="../context.json",
        )
        payload = self._cli_result(result)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "failed")

        result = self._run_cli(
            self.packet(),
            context={
                **self._cli_context(),
                "unexpected": "field",
            },
        )
        payload = self._cli_result(result)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "failed")

    def test_cli_rejects_duplicate_json_object_keys(self) -> None:
        self._write("context.json", json.dumps(self._cli_context()))
        self._write(
            "evidence.json",
            '{"schema":"story-change-evidence-v1",'
            '"schema":"story-change-evidence-v1"}',
        )
        result = subprocess.run(
            [
                sys.executable,
                str(self.adapter_script),
                "--context",
                "context.json",
                "--evidence",
                "evidence.json",
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        payload = self._cli_result(result)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "failed")

    def test_api_rejects_oversized_integer_as_typed_input_error(self) -> None:
        packet = self.packet()
        packet["reason"] = 10 ** 4999
        with self.assertRaisesRegex(StoryEvidenceError, "integer"):
            evaluate_evidence(self.expected(), packet)

    def test_json_input_api_normalizes_oversized_integer_errors(self) -> None:
        self._write("evidence.json", '{"x":' + "9" * 5000 + "}")
        with self.assertRaises(StoryEvidenceError):
            _safe_json_input(self.root, "evidence.json", "evidence packet")

    def test_api_rejects_lone_surrogates_as_typed_input_errors(self) -> None:
        for surrogate in ("\ud800", "\udfff"):
            for location in ("value", "key"):
                with self.subTest(surrogate=repr(surrogate), location=location):
                    packet = self.packet()
                    self._set_non_ui(packet)
                    if location == "value":
                        packet["reason"] = surrogate
                    else:
                        packet[surrogate] = None
                    with self.assertRaisesRegex(StoryEvidenceError, "Unicode"):
                        evaluate_evidence(self.expected(), packet)

    def test_malformed_json_scalars_cli_return_closed_input_errors(self) -> None:
        packet = self.packet()
        self._set_non_ui(packet)
        malformed_json = ['{"x":' + "9" * 5000 + "}"]
        for surrogate in ("\ud800", "\udfff"):
            malformed_json.append(json.dumps({**packet, "reason": surrogate}))
            malformed_json.append(json.dumps({**packet, surrogate: None}))
        for raw in malformed_json:
            for input_path in ("context.json", "evidence.json"):
                with self.subTest(raw_prefix=raw[:30], input_path=input_path):
                    self._write("context.json", json.dumps(self._cli_context()))
                    self._write("evidence.json", json.dumps(packet))
                    self._write(input_path, raw)
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(self.adapter_script),
                            "--context", "context.json",
                            "--evidence", "evidence.json",
                        ],
                        cwd=self.root,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    payload = self._cli_result(result)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(payload["status"], "failed")

    def test_valid_unicode_scalars_remain_accepted(self) -> None:
        packet = self.packet()
        self._set_non_ui(packet)
        packet["reason"] = "No UI changes: café, настройки, \U0001f600."
        self.assertEqual(
            evaluate_evidence(self.expected(), packet).status,
            Status.NOT_APPLICABLE,
        )
        result = self._run_cli(packet)
        payload = self._cli_result(result)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["status"], "not_applicable")

    def test_cli_bounds_json_input_before_parsing(self) -> None:
        self._write("context.json", " " * (1_048_576 + 1))
        self._write("evidence.json", json.dumps(self.packet()))
        result = subprocess.run(
            [
                sys.executable,
                str(self.adapter_script),
                "--context",
                "context.json",
                "--evidence",
                "evidence.json",
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        payload = self._cli_result(result)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "failed")


if __name__ == "__main__":
    unittest.main()
