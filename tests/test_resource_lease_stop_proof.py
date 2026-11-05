from __future__ import annotations

import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import base64
import sqlite3
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from contextlib import closing
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "templates/project/resource_lease_stop_proof.py"
loader = importlib.machinery.SourceFileLoader("stop_proof_tested", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
proof = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = proof
loader.exec_module(proof)

PROJECT = "project-11111111-1111-4111-8111-111111111111"
TASK = "task-22222222-2222-4222-8222-222222222222"
OWNER_SESSION = "33333333-3333-4333-8333-333333333333"
REVIEWERS = (
    "44444444-4444-4444-8444-444444444444",
    "55555555-5555-4555-8555-555555555555",
)
ANSWERER_UNSET = object()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


class ProofFixture:
    def __init__(self, root: Path, *, unresolved=False, question_version=None,
                 external_approval=False, recovery_review=False, terminal_null_generation=False,
                 recommended_option_index=ANSWERER_UNSET, short_assignment=False,
                 coverage_dispositions=None):
        self.root = root.resolve()
        self.question_version = question_version or proof.EFFECT_KENT_QUESTION_VERSION
        self.external_approval = external_approval
        self.recovery_review = recovery_review
        self.recommended_option_index = recommended_option_index
        self.short_assignment = short_assignment
        self.coverage_dispositions = coverage_dispositions or (
            "administrative_source_reviewed", "stage_input_stop_confirmed")
        self.persistence = self.root / "kent"
        self.project = self.persistence / "projects" / PROJECT
        (self.project / "sessions").mkdir(mode=0o700, parents=True)
        (self.persistence / "db").mkdir(mode=0o700, parents=True)
        self.db_path = self.persistence / "db" / "main.sqlite3"
        self.evidence = self.root / "evidence"
        self.evidence.mkdir(mode=0o700)
        self.run = self.root / "runs" / "legacy-run"
        self.run.mkdir(mode=0o700, parents=True)
        self.operation_id = "77777777-7777-4777-8777-777777777777"
        self.binding = {
            "schema": 1, "owner": "smoke:test", "capability": "clean",
            "profile": "smoke.clean",
            "binding": {"project": PROJECT, "task": TASK, "session": OWNER_SESSION},
            "config": {"executable": "/qualified/resource-lease", "pool": "/private/pool",
                       "runs_directory": str(self.run.parent)},
        }
        self._write(self.run / "binding.json", canonical(self.binding))
        self._write(self.run / "receipt.json", canonical({"state": "completed"}))
        self.status = {
            "phase": "failed", "updated_at": 12.0, "run_directory": str(self.run),
            "lease": {"id": "lease-1", "slot": "slot-a", "generation": 9,
                      "state": "completed", "endpoint": None},
            "error": "command stop is uncertain; preserve unknown effects",
            "command_stop": "unconfirmed", "unknown_effects": ["application effect remains unknown"],
        }
        if terminal_null_generation:
            self.status["lease"].update(generation=None, cleanup_pending=False, quarantined=False)
        self.status_path = self.run / "status.json"
        self.status_bytes = canonical(self.status) + b"\n"
        self._write(self.status_path, self.status_bytes)
        self._write(self.run / "helper.lock", b"")
        self.subject = {
            "project_id": PROJECT, "task_id": TASK, "session_id": OWNER_SESSION,
            "run_directory": str(self.run),
            "binding_sha256": digest((self.run / "binding.json").read_bytes()),
            "receipt_sha256": digest((self.run / "receipt.json").read_bytes()),
            "lease_identity": {"id": "lease-1", "slot": "slot-a", "generation": 9,
                               "serial": "emulator-5574"},
        }
        self.step = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.coordinator = "66666666-6666-4666-8666-666666666666"
        self.answerer = "77777777-7777-4777-8777-777777777777"
        self._install_native_metadata()
        self.owner_events = []
        self._build_owner()
        if recovery_review:
            review, review_id = self._native_call(
                7, "ask_question", {"question": "Which reviewed same-run recovery route applies?"},
                {"ToolName": "ask_question", "Question": "Which reviewed same-run recovery route applies?"},
            )
            self.owner_events.extend([
                review,
                self._completion(8, review_id, "ask_question", "Use the separately approved route.",
                                 question_answer={"freeform": "Use the separately approved route."},
                                 answered_by_session_id=self.coordinator),
                self._tool_message(9, review_id, "ask_question"),
            ])
        self._write_owner()
        self._make_packet(unresolved=unresolved)

    def _write(self, path, data):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o600)

    @staticmethod
    def _event(seq, kind, payload, *, step="test-step"):
        return {"seq": seq, "kind": kind, "step_id": step, "payload": payload}

    def _native_call(self, seq, name, arguments, presentation, *, step="test-step"):
        call_id = f"call-{seq}"
        item = {"id": f"fc-{seq}", "type": "function_call", "status": "completed",
                "arguments": json.dumps(arguments, separators=(",", ":")),
                "call_id": call_id, "name": name}
        payload = {"role": "assistant", "native_tool_presentation": {call_id: presentation},
                   "native_response": {"format": "openai-responses",
                                       "items": [base64.b64encode(canonical(item)).decode()]}}
        return self._event(seq, "message", payload, step=step), call_id

    def _completion(self, seq, call_id, name, output, *, question_answer=None,
                    answered_by_session_id=ANSWERER_UNSET, step="test-step"):
        result = {"type": "function_call_output", "name": name, "call_id": call_id,
                  "raw": {"type": "function_call_output", "call_id": call_id, "output": output}}
        payload = {"call_id": call_id, "name": name, "output_kind": "function",
                   "is_error": False, "output": output,
                   "presentation": {"ToolName": name}, "provider_items": [result]}
        if question_answer is not None:
            payload["question_answer"] = question_answer
        if answered_by_session_id is not ANSWERER_UNSET:
            payload["answered_by_session_id"] = answered_by_session_id
        return self._event(seq, "tool_completed", payload, step=step)

    def _tool_message(self, seq, call_id, name):
        return self._event(seq, "message", {"role": "tool", "name": name,
                                             "tool_call_id": call_id, "content": "ok"})

    def _assistant_message(self, seq, text, session_step="test-step"):
        item = {"id": f"msg-{seq}", "type": "message", "status": "completed",
                "role": "assistant", "content": [{"type": "output_text", "text": text}]}
        return self._event(seq, "message", {
            "role": "assistant", "native_response": {
                "format": "openai-responses",
                "items": [base64.b64encode(canonical(item)).decode()],
            },
        }, step=session_step)

    def _write_session(self, session_id, events):
        path = self.project / "sessions" / session_id / "events.jsonl"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = b'{"contract":"kent.session.events","version":2}\n'
        data += b"".join(canonical(event) + b"\n" for event in events)
        self._write(path, data)
        return path

    def _write_owner(self):
        self.owner_path = self._write_session(OWNER_SESSION, self.owner_events)

    def _locator(self, session_id, sequence):
        path = self.project / "sessions" / session_id / "events.jsonl"
        return {"project_id": PROJECT, "session_id": session_id,
                "event_sequence": sequence,
                "event_sha256": digest(path.read_bytes().splitlines()[sequence])}

    def _ref(self, name, data):
        path = self.evidence / name
        self._write(path, data)
        return {"path": str(path), "object_sha256": digest(data), "byte_size": len(data)}

    def _install_native_metadata(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
                           "parent_agent_session_id TEXT, task_id TEXT, locked_json TEXT, "
                           "continuation_json TEXT)")
        connection.execute("CREATE TABLE session_workflow_node_associations "
                           "(session_id TEXT, node_id TEXT, transition_branch_key TEXT, "
                           "associated_at_unix_ms INTEGER)")
        rows = [
            (OWNER_SESSION, None, TASK, {"system_prompt": "owner", "enabled_tools": []},
             {"agent_role": "default"}),
            (self.coordinator, None, None, {"system_prompt": "coordinator", "enabled_tools": []},
             {"agent_role": "default"}),
            (self.answerer, None, None, {"system_prompt": "human", "enabled_tools": []}, {}),
        ]
        for session_id, parent, task_id, locked, continuation in rows:
            connection.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                               (session_id, PROJECT, parent, task_id,
                                json.dumps(locked), json.dumps(continuation)))
        for session_id, role in zip(REVIEWERS, ("grill", "architecture-designer")):
            prompt = f"You are an approved read-only {role} reviewer."
            connection.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                               (session_id, PROJECT, self.coordinator, None,
                                json.dumps({"system_prompt": prompt,
                                            "enabled_tools": ["exec_command", "view_image"],
                                            "has_system_prompt": True,
                                            "has_enabled_tools": True}),
                                json.dumps({"agent_role": role})))
        connection.commit()
        connection.close()
        self.db_path.chmod(0o600)

    def _build_owner(self):
        command = (f"./resource-lease-client start --config /private/config.json --profile smoke.clean "
                   f"--run-dir {self.run} --project {PROJECT} --task {TASK} --session {OWNER_SESSION}")
        first, cid = self._native_call(1, "exec_command", {"cmd": command},
                                       {"ToolName": "exec_command", "Command": command})
        output = canonical({
            "run_directory": str(self.run), "phase": "active", "error": None,
            "lease": {"id": "lease-1", "slot": "slot-a", "generation": 9,
                      "owner": self.binding["owner"], "capability": "clean", "state": "active",
                      "cleanup_pending": False, "quarantined": False,
                      "endpoint": {"adb_serial": "emulator-5574"}},
        }).decode()
        done = self._completion(2, cid, "exec_command", output)
        tool = self._tool_message(3, cid, "exec_command")
        stage = (f'run_dir="$(.kent/scripts/workflow-checkpoint read --stage smoke --task OSM-156 '
                 f'--workspace "$PWD" | jq -er \'.stage_data.resource_lease_run_directory\')"; '
                 f'RESOURCE_LEASE_RUN_DIRECTORY="$run_dir" ./tools/stage-input --serial emulator-5574 '
                 f'--config "$HOME/.config/kent/android-tools/test.json" --source '
                 f'"$PWD/.kent/runtime/OSM-156/stage-input-private/input.md" --account Invoices '
                 f'--field phone; .kent/scripts/workflow-checkpoint read --stage smoke --task OSM-156 '
                 f'--workspace "$PWD" | jq \'.stage_data.result="done"\' | '
                 f'.kent/scripts/workflow-checkpoint write --stage smoke --task OSM-156 --workspace "$PWD"')
        second, cid2 = self._native_call(4, "exec_command", {"cmd": stage},
                                         {"ToolName": "exec_command", "Command": stage})
        result = '{"status":"action_accepted","readback":"different","reason":null}'
        checkpoint = canonical({"checkpoint_path": "/tmp/smoke-checkpoint.json",
                                "updated_at": "2026-11-05T00:00:00Z"}).decode()
        done2 = self._completion(5, cid2, "exec_command", result + "\n" + checkpoint)
        tool2 = self._tool_message(6, cid2, "exec_command")
        self.owner_events = [first, done, tool, second, done2, tool2]
        self._write_owner()
        start = self._locator(OWNER_SESSION, 1)
        cutoff = self._locator(OWNER_SESSION, 6)
        self.history = {"start": start, "async_completion": None,
                        "audited_start_sequence": 1, "cutoff": cutoff}

    def _make_packet(self, *, unresolved=False):
        source = b"""import json
class Helper:
    def confirm_remote_stopped(self): pass
    def handoff_uncertain(self, reason): pass

def execute(args, custody):
    result = {"status": "action_accepted", "readback": "different", "reason": None}
    try:
        pass
    finally:
        try:
            command(["shell", "am", "force-stop"])
            command(["shell", "pidof"])
            command(["shell", "dumpsys"])
            command(["shell", "run-as", "rm"])
            custody.confirm_remote_stopped()
        except Exception:
            result = outcome("unknown", "cleanup")
            custody.handoff_uncertain("remote_stop_unconfirmed")
    return result

def main():
    result = execute(None, Helper())
    print(json.dumps(result))
"""
        producer = self._ref("stage-input", source)
        delegate_refs = [
            self._ref("delegates/" + relative, f"test delegate {relative}".encode())
            for relative in proof.LEGACY_PROJECT_DELEGATE_SHA256
        ]
        self.delegate_hashes = {
            relative: reference["object_sha256"]
            for relative, reference in zip(proof.LEGACY_PROJECT_DELEGATE_SHA256, delegate_refs)
        }
        candidate = self._ref("candidate.json", canonical({
            "files": [{"path": "tools/stage-input", "sha256": digest(source), "mode": "100755"}]
            + [{"path": relative, "sha256": sha, "mode": "100755"}
               for relative, sha in self.delegate_hashes.items()],
        }))
        legacy_refs = [self._ref(name, f"reviewed test {name} producer".encode())
                       for name in proof.LEGACY_PRODUCER_SHA256 if name != "stage-input"]
        self.producer_hashes = {"stage-input": digest(source)}
        self.producer_hashes.update({Path(ref["path"]).name: ref["object_sha256"]
                                     for ref in legacy_refs})
        source_contents = {
            "ask_question_resolution.go": (
                b"func questionAnswererLabel\nreturn \"User\"\nreturn \"Agent \"\n"
            ),
            "ask_question_tool.go": (
                b"if answer.Answerer != nil\nAnsweredBySessionID\n"
            ),
            "question_history_projection.go": (
                b"completion.AnsweredBySessionID\nAnsweredBySessionID: answerer\n"
            ),
        }
        source_refs = [
            self._ref(name, data) for name, data in source_contents.items()
        ]
        self.question_source_refs = source_refs
        self.question_source_hashes = {
            Path(ref["path"]).name: ref["object_sha256"] for ref in source_refs
        }
        self.runtime_ref = self._ref("runtime-schema.json", canonical({
            "schema": proof.KENT_QUESTION_PROTOCOL_SCHEMA,
            "kent_version": self.question_version,
            "source_refs": source_refs,
        }))
        evidence = self._ref("coverage.bin", b"immutable invocation evidence " * 8)
        core = {
            "schema": proof.CORE_SCHEMA, "producer_contract": proof.PRODUCER_CONTRACT,
            "subject": self.subject,
            "source_closure": {"candidate_ref": candidate, "source_refs": [producer],
                                "runtime_schema_ref": self.runtime_ref,
                                "auxiliary_refs": legacy_refs + delegate_refs},
            "history": self.history,
            "coverage": [
                {"event_locator": self._locator(OWNER_SESSION, 1),
                 "disposition": self.coverage_dispositions[0], "evidence_refs": [evidence]},
                {"event_locator": self._locator(OWNER_SESSION, 4),
                 "disposition": self.coverage_dispositions[1], "evidence_refs": [evidence]},
            ],
            "manifest_inventory": [],
        }
        if self.recovery_review:
            core["source_closure"]["auxiliary_refs"].append(self._ref("review-tail.json", canonical({
                "schema": "native-stop-reviewed-tail-v1",
                "subject": {key: self.subject[key] for key in ("project_id", "task_id", "session_id")},
                "records": [{"invocation_locator": self._locator(OWNER_SESSION, 7),
                             "purpose": "same_run_recovery_information",
                             "expected_answerer_session_id": self.coordinator}],
            })))
        core_hash = digest(canonical(core))
        core_ref = self._ref("core.json", canonical(core))
        intent = {
            "schema": proof.INTENT_SCHEMA, "operation_id": self.operation_id,
            "core_sha256": core_hash,
            "expected_preimage_sha256": digest(self.status_bytes),
            "application_timestamp_policy": "monotonic_at_application",
            "permitted_mutation": {"kind": "terminal_status_only", "phase": "finished",
                                   "command_stop": "confirmed"},
            "target_status": {"phase": "finished", "command_stop": "confirmed",
                              "preserve_error": True, "preserve_unknown_effects": True,
                              "operation_id": self.operation_id},
        }
        intent_hash = digest(canonical(intent))
        intent_ref = self._ref("intent.json", canonical(intent))
        interval = {"project_id": PROJECT, "session_id": OWNER_SESSION,
                    "start_sequence": 1, "cutoff_sequence": 6}
        audit_refs = []
        coordinator_events = []
        if self.short_assignment:
            with closing(sqlite3.connect(self.db_path)) as connection, connection:
                connection.execute("ALTER TABLE sessions ADD COLUMN short_id INTEGER")
                for short_id, reviewer in enumerate(REVIEWERS, 901):
                    connection.execute("UPDATE sessions SET short_id=? WHERE id=?",
                                       (short_id, reviewer))
            handoff, handoff_id = self._native_call(
                1, "trigger_handoff", {}, {"ToolName": "trigger_handoff"})
            handoff_done = self._completion(2, handoff_id, "trigger_handoff", {"status": "ok"})
            handoff_done["payload"]["provider_items"][0]["raw"]["output"] = '{"status":"ok"}'
            coordinator_events.extend(
                (handoff, handoff_done, self._tool_message(3, handoff_id, "trigger_handoff")))
        for reviewer, role in zip(REVIEWERS, ("grill", "architecture-designer")):
            seq = len(coordinator_events) + 1
            command = f"kent run --agent={role} --task {TASK} --review {core_hash} {intent_hash}"
            dispatch, cid = self._native_call(seq, "exec_command", {"cmd": command},
                                              {"ToolName": "exec_command", "Command": command})
            if self.short_assignment:
                short_id = 901 + REVIEWERS.index(reviewer)
                output = (f"Process moved to background with ID {70 + short_id}. Output:\n"
                          f'Started a new session, `kent run steer {short_id} "prompt"` '
                          "to send messages while it runs")
            else:
                output = f"started session {reviewer}"
            completion = self._completion(seq + 1, cid, "exec_command", output)
            if self.short_assignment:
                completion["payload"]["presentation"].update(
                    IsShell=True, MovedToBackground=True, ShellExitCode=None)
            tool = self._tool_message(seq + 2, cid, "exec_command")
            coordinator_events.extend((dispatch, completion, tool))
            self._write_session(self.coordinator, coordinator_events)
            assignment = self._locator(self.coordinator, seq)
            report = {
                "schema": proof.AUDIT_SCHEMA, "reviewer_session_id": reviewer,
                "assignment_locator": assignment, "core_sha256": core_hash,
                "intent_sha256": intent_hash, "interval": interval,
                "coverage_complete": True,
                "facts": [{"claim": claim, "conclusion": "pass",
                           "evidence_refs": [{"kind": "native_event",
                                              "value": self._locator(OWNER_SESSION, 4)}]}
                          for claim in sorted(proof.REQUIRED_AUDIT_CLAIMS)],
                "findings": ["unresolved proof gap"] if unresolved else [],
            }
            report_event = self._assistant_message(1, json.dumps(report, sort_keys=True))
            self._write_session(reviewer, [report_event])
            audit_refs.append(self._locator(reviewer, 1))
        self._write_session(self.coordinator, coordinator_events)

        qualification = {"schema": proof.QUALIFICATION_SCHEMA, "core_sha256": core_hash,
                         "intent_sha256": intent_hash, "core_ref": core_ref,
                         "intent_ref": intent_ref, "audit_refs": audit_refs}
        q_bytes = canonical(qualification)
        self.qualification_path = self.evidence / "qualification.json"
        self._write(self.qualification_path, q_bytes)
        self.request_path = self.evidence / "request.json"
        question = (f"Approve terminal_status_reconciliation for Q={digest(q_bytes)} "
                    f"and I={intent_hash}; terminal status only, preserve unknown effects.")
        question_args = {"question": question, "suggestions": ["Approve", "Decline"]}
        if self.recommended_option_index is not ANSWERER_UNSET:
            question_args["recommended_option_index"] = self.recommended_option_index
        qevent, qid = self._native_call(
            7, "ask_question", question_args, {"ToolName": "ask_question", "Question": question})
        answer = {"selected_option_number": 1}
        qdone = self._completion(
            8, qid, "ask_question", "Approved", question_answer=answer,
            answered_by_session_id=None,
        )
        qtool = self._tool_message(9, qid, "ask_question")
        reconcile = (f"/qualified/resource-lease-client reconcile --request {self.request_path} "
                     f"--qualification {self.qualification_path}")
        current, _rid = self._native_call(10, "exec_command", {"cmd": reconcile},
                                          {"ToolName": "exec_command", "Command": reconcile},
                                          step=self.step)
        if self.external_approval:
            coordinator_events.extend((qevent, qdone, qtool))
            self._write_session(self.coordinator, coordinator_events)
            current["seq"] = len(self.owner_events) + 1
            self.owner_events.append(current)
            approval_session = self.coordinator
        else:
            self.owner_events.extend((qevent, qdone, qtool, current))
            approval_session = OWNER_SESSION
        self._write_owner()
        request = {"schema": proof.REQUEST_SCHEMA, "run_directory": str(self.run),
                   "expected_preimage_sha256": digest(self.status_bytes),
                   "qualification_ref": {"object_sha256": digest(q_bytes),
                                         "byte_size": len(q_bytes)},
                   "effect_approval_ref": {
                       "native_question_locator": self._locator(approval_session, 7),
                       "approved_intent_sha256": intent_hash}}
        self.request = request
        self._write(self.request_path, canonical(request))

    def verify(self):
        with patch.object(proof, "KENT_QUESTION_SOURCE_SHA256", self.question_source_hashes), \
                patch.object(proof, "LEGACY_PRODUCER_SHA256", self.producer_hashes), \
                patch.object(proof, "LEGACY_PROJECT_DELEGATE_SHA256", self.delegate_hashes), \
                patch.dict(os.environ, {"KENT_PERSISTENCE_ROOT": str(self.persistence),
                                        "KENT_SESSION_ID": OWNER_SESSION,
                                        "KENT_STEP_ID": self.step}):
            return proof.verify_bundle(self.request_path, self.qualification_path)

    def next_reconciliation_call(self, output):
        previous = self.owner_events[-1]
        call_id = next(iter(previous["payload"]["native_tool_presentation"]))
        sequence = len(self.owner_events) + 1
        self.owner_events += [
            self._completion(sequence, call_id, "exec_command", output, step=self.step),
            self._tool_message(sequence + 1, call_id, "exec_command"),
        ]
        command = previous["payload"]["native_tool_presentation"][call_id]["Command"]
        self.step = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        current, _ = self._native_call(
            sequence + 2, "exec_command", {"cmd": command},
            {"ToolName": "exec_command", "Command": command}, step=self.step,
        )
        self.owner_events.append(current)
        self._write_owner()


class StopProofTests(unittest.TestCase):
    def test_native_recommendation_is_optional_display_metadata_not_effect_consent(self):
        for external in (False, True):
            for index in (1, 2):
                with self.subTest(external=external, recommended=index), tempfile.TemporaryDirectory() as directory:
                    fixture = ProofFixture(Path(directory), external_approval=external,
                                           recommended_option_index=index)
                    self.assertEqual(fixture.verify().intent["operation_id"], fixture.operation_id)
            for index in (True, 0, -1, 3, "1", None):
                with self.subTest(external=external, invalid=index), tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(proof.ProofError):
                        ProofFixture(Path(directory), external_approval=external,
                                     recommended_option_index=index).verify()

    def test_legacy_terminal_generation_is_not_a_fabricated_historical_observation(self):
        identity = {"id": "lease-1", "slot": "slot-a", "generation": 9}
        terminal = {"id": "lease-1", "slot": "slot-a", "generation": None,
                    "state": "completed", "cleanup_pending": False,
                    "quarantined": False, "endpoint": None}
        proof._validate_legacy_status_identity(terminal, identity)
        self.assertIsNone(terminal["generation"])
        for changes in ({"state": "active"}, {"cleanup_pending": True}, {"generation": 10},
                        {"id": "different"}, {"generation": True}):
            with self.subTest(changes=changes), self.assertRaises(proof.ProofError):
                proof._validate_legacy_status_identity({**terminal, **changes}, identity)

    def test_actual_native_store_read_modes_preserve_owner_only_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            fixture.persistence.chmod(0o755)
            fixture.db_path.chmod(0o644)
            self.assertEqual(fixture.verify().intent["operation_id"], fixture.operation_id)
            fixture.db_path.chmod(0o664)
            with self.assertRaisesRegex(proof.ProofError, "owner-controlled"):
                fixture.verify()
            fixture.db_path.chmod(0o644)
            fixture.project.chmod(0o775)
            with self.assertRaisesRegex(proof.ProofError, "association is unsafe"):
                fixture.verify()

    def test_native_short_assignment_joins_real_reviewer_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory), short_assignment=True)
            self.assertEqual(fixture.verify().intent["operation_id"], fixture.operation_id)
            with closing(sqlite3.connect(fixture.db_path)) as connection, connection:
                connection.execute("UPDATE sessions SET short_id=999 WHERE id=?",
                                   (REVIEWERS[0],))
            with self.assertRaisesRegex(proof.ProofError, "assignment"):
                fixture.verify()

    def test_native_short_assignment_rejects_unqualified_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory), short_assignment=True)
            output = ('Process moved to background with ID 971. Output:\n'
                      'Started a new session, `kent run steer 901 "prompt"` '
                      'to send messages while it runs')
            presentation = {"IsShell": True, "MovedToBackground": True, "ShellExitCode": None}
            for text, metadata in (
                (output.replace("steer 901", "steer 902"), presentation),
                (output + "\n" + REVIEWERS[0], presentation),
                ("echo " + output, presentation),
                (output, {**presentation, "IsShell": False}),
                (output, {**presentation, "ShellExitCode": 1}),
                (output, {**presentation, "MovedToBackground": False}),
                (f"unrelated output mentions {REVIEWERS[0]}", presentation),
            ):
                with self.subTest(text=text, metadata=metadata):
                    completion = {"payload": {
                        "output": text, "presentation": metadata, "is_error": False}}
                    self.assertFalse(proof._assignment_output_matches(
                        completion, REVIEWERS[0], PROJECT, fixture.persistence))
            synchronous = {"payload": {
                "is_error": False,
                "output": output.split("\n", 1)[1],
                "presentation": {"IsShell": True, "MovedToBackground": False, "ShellExitCode": 0},
            }}
            self.assertTrue(proof._assignment_output_matches(
                synchronous, REVIEWERS[0], PROJECT, fixture.persistence))
            with closing(sqlite3.connect(fixture.db_path)) as connection, connection:
                connection.execute("UPDATE sessions SET parent_agent_session_id=NULL WHERE id=?",
                                   (REVIEWERS[0],))
            with self.assertRaisesRegex(proof.ProofError, "parent"):
                fixture.verify()

    def test_native_assignment_requires_unique_same_step_completion_and_tool_message(self):
        for mutation in ("wrong_step", "duplicate_completion", "missing_tool_message"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                fixture = ProofFixture(Path(directory), short_assignment=True)
                path = fixture.project / "sessions" / fixture.coordinator / "events.jsonl"
                events = [json.loads(line) for line in path.read_text().splitlines()[1:]]
                # The first three events are a genuine but unrelated handoff.
                completion, message = events[4:6]
                if mutation == "wrong_step":
                    completion["step_id"] = "another-step"
                elif mutation == "duplicate_completion":
                    duplicate = json.loads(json.dumps(completion))
                    duplicate["seq"] = len(events) + 1
                    events.append(duplicate)
                else:
                    message["payload"]["tool_call_id"] = "unrelated-call"
                fixture._write_session(fixture.coordinator, events)
                with self.assertRaises(proof.ProofError):
                    fixture.verify()

    def test_neutral_mixed_disposition_does_not_replace_stage_stop_proof(self):
        neutral = "mixed_administrative_execution_reviewed"
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory), coverage_dispositions=(
                neutral, "stage_input_stop_confirmed"))
            result = fixture.verify()
            self.assertEqual(result.core["coverage"][0]["disposition"], neutral)
            self.assertEqual(result.intent["target_status"]["preserve_unknown_effects"], True)
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory), coverage_dispositions=(neutral, neutral))
            with self.assertRaisesRegex(proof.ProofError, "wrong proof disposition"):
                fixture.verify()

    def test_background_process_can_settle_through_genuine_empty_native_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            fixture.owner_events[1] = fixture._completion(
                2, "call-1", "exec_command", "Process moved to background with ID 77. Output:\n")
            fixture.owner_events[1]["payload"]["presentation"]["MovedToBackground"] = True
            poll, call_id = fixture._native_call(
                4, "write_stdin", {"session_id": 77, "chars": ""},
                {"ToolName": "write_stdin"})
            done = fixture._completion(
                5, call_id, "write_stdin",
                "Wall time: 1.0 seconds\nOutput file: /tmp/77.log\nExit code 0, output:\nsettled")
            done["payload"]["presentation"].update(
                IsShell=True, ShellExitCode=0, MovedToBackground=False)
            fixture.owner_events[3:5] = [poll, done]
            fixture._write_owner()
            history = {"async_completion": fixture._locator(OWNER_SESSION, 5)}
            events = fixture.owner_events[:6]
            calls, completions = proof._native_trace(events)
            proof._validate_background_settlement(
                events, calls, completions, history, fixture.subject, fixture.persistence)
            done["payload"]["presentation"]["ShellExitCode"] = None
            with self.assertRaisesRegex(proof.ProofError, "completion sets differ"):
                proof._validate_background_settlement(
                    events, calls, completions, history, fixture.subject, fixture.persistence)
            done["payload"]["presentation"]["ShellExitCode"] = 0
            calls[call_id]["arguments"]["chars"] = "interactive input"
            with self.assertRaisesRegex(proof.ProofError, "read-only observation"):
                proof._validate_background_settlement(
                    events, calls, completions, history, fixture.subject, fixture.persistence)

    def test_actual_custom_patch_protocol_joins_structured_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            event, call_id = fixture._native_call(
                1, "patch", {"unused": True}, {"ToolName": "patch"},
            )
            item = {"type": "custom_tool_call", "name": "patch", "call_id": call_id,
                    "input": "*** Begin Patch\n*** End Patch"}
            event["payload"]["native_response"]["items"] = [
                base64.b64encode(canonical(item)).decode()]
            call = proof._response_calls(event)[call_id]
            completion = fixture._completion(2, call_id, "patch", '{"ok":true,"operations":1}')
            completion["payload"]["output_kind"] = "custom"
            completion["payload"]["output"] = {"ok": True, "operations": 1}
            provider = completion["payload"]["provider_items"][0]
            provider["type"] = provider["raw"]["type"] = "custom_tool_call_output"
            proof._completion_output(completion, call)
            provider["raw"]["output"] = '{"ok":true,"operations":2}'
            with self.assertRaisesRegex(proof.ProofError, "provider output"):
                proof._completion_output(completion, call)

    def test_native_owner_reads_persisted_association(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            connection = sqlite3.connect(fixture.db_path)
            connection.execute("INSERT INTO session_workflow_node_associations VALUES (?,?,?,?)",
                               (OWNER_SESSION, "node-one", "", 0))
            connection.commit()
            connection.close()
            detail = {
                "summary": {"id": TASK, "project_id": PROJECT, "done": False},
                "status": {"kind": "running", "native_state": "running",
                           "node_ids": ["node-one"]},
                "current_nodes": [{"node_id": "node-one", "session_id": OWNER_SESSION}],
            }
            sessions = {"task_id": TASK, "items": [
                {"session_id": OWNER_SESSION, "status": "running"}]}
            with patch.dict(os.environ, {"KENT_SESSION_ID": OWNER_SESSION,
                                         "KENT_STEP_ID": fixture.step,
                                         "KENT_PERSISTENCE_ROOT": str(fixture.persistence)}), \
                    patch.object(proof.shutil, "which", return_value="/qualified/kent"), \
                    patch.object(proof, "_native_cli_version"), \
                    patch.object(proof, "_command_json", side_effect=[detail, sessions]):
                proof._native_owner_active(fixture.subject)

    def test_native_retry_tail_reaches_same_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            previous = fixture.owner_events[-1]
            call_id = next(iter(previous["payload"]["native_tool_presentation"]))
            fixture.owner_events += [
                fixture._completion(11, call_id, "exec_command", '{"phase":"finished"}',
                                    step=fixture.step),
                fixture._tool_message(12, call_id, "exec_command"),
            ]
            command = previous["payload"]["native_tool_presentation"][call_id]["Command"]
            current, _ = fixture._native_call(
                13, "exec_command", {"cmd": command},
                {"ToolName": "exec_command", "Command": command}, step=fixture.step,
            )
            fixture.owner_events.append(current)
            fixture._write_owner()
            fixture.verify()

    def test_json_parser_rejects_duplicate_and_non_finite_values(self):
        with self.assertRaisesRegex(proof.ProofError, "duplicate"):
            json.loads(b'{"a":1,"a":2}', object_pairs_hook=proof._unique_object)
        with self.assertRaisesRegex(proof.ProofError, "non-finite"):
            json.loads(b'{"a":NaN}', parse_constant=proof._reject_constant)

    def test_verifies_acyclic_native_bound_packet_and_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            verified = fixture.verify()
            self.assertEqual(verified.intent["operation_id"], fixture.operation_id)
            self.assertEqual(verified.hashes["qualification"],
                             fixture.request["qualification_ref"]["object_sha256"])

    def test_rejects_unresolved_audit_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(proof.ProofError, "findings"):
                ProofFixture(Path(directory), unresolved=True).verify()

    def test_rejects_unknown_native_event_schema_and_unreviewed_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            fixture.owner_events.append(fixture._event(11, "future_native_event", {}))
            fixture._write_owner()
            with self.assertRaisesRegex(proof.ProofError, "event schema"):
                fixture.verify()
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            fixture.owner_events.append(fixture._event(11, "tool_completed", {
                "call_id": "unrelated-call", "name": "exec_command",
                "is_error": False, "output": "", "presentation": {"ToolName": "exec_command"},
                "provider_items": [],
            }))
            fixture._write_owner()
            with self.assertRaisesRegex(proof.ProofError, "native completion has no unique original invocation"):
                fixture.verify()

    def test_accepts_only_exact_reconciliation_tool_call_in_native_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            fixture._write_owner()
            self.assertEqual(fixture.verify().intent["operation_id"], fixture.operation_id)

    def test_rejects_changed_approval_and_changed_qualification(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            call_event = fixture.owner_events[6]
            payload = call_event["payload"]
            item = json.loads(base64.b64decode(payload["native_response"]["items"][0]))
            args = json.loads(item["arguments"])
            args["question"] += " changed"
            item["arguments"] = json.dumps(args, separators=(",", ":"))
            payload["native_response"]["items"][0] = base64.b64encode(canonical(item)).decode()
            payload["native_tool_presentation"][item["call_id"]]["Question"] = args["question"]
            fixture._write_owner()
            with self.assertRaisesRegex(proof.ProofError, "locator digest"):
                fixture.verify()
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            fixture.qualification_path.write_bytes(b"{}\n")
            with self.assertRaisesRegex(proof.ProofError, "qualification reference changed"):
                fixture.verify()

    def test_explicit_null_native_answerer_is_human_consent_when_source_qualified(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            completion = fixture.owner_events[7]
            completion["payload"]["question_answer"] = {"selected_option_number": 1}
            completion["payload"]["answered_by_session_id"] = None
            fixture._write_owner()
            fixture.request["effect_approval_ref"]["native_question_locator"] = fixture._locator(
                OWNER_SESSION, 7,
            )
            fixture._write(fixture.request_path, canonical(fixture.request))
            self.assertEqual(fixture.verify().intent["operation_id"], fixture.operation_id)

    def test_missing_question_answerer_field_is_not_attributed_to_user(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            fixture.owner_events[7]["payload"].pop("answered_by_session_id")
            fixture._write_owner()
            fixture._write(fixture.request_path, canonical(fixture.request))
            with patch.object(proof, "_command_json", return_value={"questions": []}), \
                    self.assertRaisesRegex(proof.ProofError, "actor join"):
                fixture.verify()

    def test_omitted_native_user_actor_requires_exact_history_projection_join(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            event = fixture.owner_events[6]
            call = next(iter(proof._response_calls(event).values()))
            completion = fixture.owner_events[7]
            completion["payload"].pop("answered_by_session_id")
            completion["committed_at_unix_ms"] = 1793836800000
            row = {
                "question": call["arguments"]["question"], "answer": "Approve",
                "selected_option_number": 1, "commentary": None,
                "answered_by_session_id": None, "at": "2026-11-05T00:00:00Z",
            }
            with patch.object(proof, "_command_json", return_value={"questions": [row]}), \
                    patch.object(proof.shutil, "which", return_value="/qualified/kent"):
                self.assertIsNone(proof._question_answerer(
                    call, completion, OWNER_SESSION, "2.12.0"))
                row["answered_by_session_id"] = REVIEWERS[0]
                self.assertEqual(proof._question_answerer(
                    call, completion, OWNER_SESSION, "2.12.0"), REVIEWERS[0])
                row["at"] = "2026-11-05T00:00:01Z"
                with self.assertRaisesRegex(proof.ProofError, "one genuine history"):
                    proof._question_answerer(call, completion, OWNER_SESSION, "2.12.0")

    def test_external_human_approval_and_reviewed_agent_answer_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory), external_approval=True, recovery_review=True)
            self.assertEqual(fixture.verify().intent["operation_id"], fixture.operation_id)
            fixture.owner_events[7]["payload"]["answered_by_session_id"] = REVIEWERS[0]
            fixture._write_owner()
            with self.assertRaisesRegex(proof.ProofError, "genuine native answerer"):
                fixture.verify()

    def test_pending_review_question_never_counts_as_completed_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory), external_approval=True, recovery_review=True)
            fixture.owner_events = fixture.owner_events[:7] + [fixture.owner_events[-1]]
            fixture.owner_events[-1]["seq"] = 8
            fixture._write_owner()
            with self.assertRaisesRegex(proof.ProofError, "ambiguous pending"):
                fixture.verify()

    def test_source_causality_is_exact_revision_not_word_presence(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            core = fixture.verify().core
            source_ref = core["source_closure"]["source_refs"][0]
            path = Path(source_ref["path"])
            raw = path.read_bytes()
            raw = raw.replace(
                b'            command(["shell", "am", "force-stop"])',
                b'            custody.confirm_remote_stopped()\n'
                b'            command(["shell", "am", "force-stop"])',
            )
            fixture._write(path, raw)
            source_ref.update(object_sha256=digest(raw), byte_size=len(raw))
            with patch.object(proof, "LEGACY_PRODUCER_SHA256", fixture.producer_hashes), \
                    patch.object(proof, "LEGACY_PROJECT_DELEGATE_SHA256", fixture.delegate_hashes), \
                    self.assertRaisesRegex(proof.ProofError, "exact reviewed closure"):
                proof._validate_core(core, fixture.run, proof.ReferenceBudget())

    def test_stage_recipe_forms_and_output_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            call = next(iter(proof._response_calls(fixture.owner_events[3]).values()))
            parts = proof._statements(call["presentation"]["Command"])
            result = '{"status":"action_accepted","readback":"different","reason":null}'
            checkpoint = '{"checkpoint_path":"/tmp/smoke-checkpoint.json","updated_at":"now"}'
            admin, resolve, stage = parts[2], parts[0], parts[1]
            guard = ('.kent/adapters/mobile/resource-lease-client check --run-dir "$run_dir" '
                     '>/dev/null && "$HOME/Library/Android/sdk/platform-tools/adb" '
                     '-s emulator-5574 shell pidof com.example.helper')
            variants = [
                ("; ".join((admin, resolve, stage)), checkpoint + "\n" + result),
                ("; ".join((resolve, guard, admin, stage)), "123\n" + checkpoint + "\n" + result),
                ("; ".join((resolve, stage, admin)), result + "\n" + checkpoint),
            ]
            for command, output in variants:
                with self.subTest(command=command):
                    changed = {**call, "presentation": {"Command": command}}
                    done = fixture._completion(5, "call-4", "exec_command", output)
                    self.assertEqual(proof._stage_input_call(changed, done), "emulator-5574")
            for command in (
                    "; ".join((resolve, 'echo \'{"status":"action_accepted"}\'', stage)),
                    "; ".join((resolve, stage, stage)),
                    "; ".join((resolve, 'run_dir="/foreign/run"', stage))):
                with self.subTest(command=command), self.assertRaises(proof.ProofError):
                    proof._stage_recipe(command)

    def test_forged_manifest_output_has_no_native_facade_producer(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            event, call_id = fixture._native_call(
                1, "exec_command", {"cmd": "echo forged"}, {"ToolName": "exec_command", "Command": "echo forged"})
            done = fixture._completion(2, call_id, "exec_command",
                                       '{"local_manifest":"/private/manifest.json"}')
            calls = proof._response_calls(event)
            with self.assertRaisesRegex(proof.ProofError, "result count"):
                proof._native_facade_inventory(calls, {call_id: done}, fixture.subject, [])

    def test_original_full_spool_requires_exact_native_provenance_and_uniqueness(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            source = 'r=JSON.parse(STDIN.read);puts JSON.generate({error:r["error"],manifest:r["local_manifest"]})'
            resolve = proof._statements(
                fixture.owner_events[3]["payload"]["native_tool_presentation"]["call-4"]["Command"])[0]
            command = (resolve + '; RESOURCE_LEASE_RUN_DIRECTORY="$run_dir" '
                       '.kent/adapters/android/android-tools --config '
                       '"$HOME/.config/kent/android-tools/test.json" device tree --arguments '
                       '\'{"device_serial":"emulator-5574"}\' | ruby -rjson -e \'' + source + "'")
            event, call_id = fixture._native_call(
                4, "exec_command", {"cmd": command}, {"ToolName": "exec_command", "Command": command})
            full = b'{"error":null,"manifest":"/private/manifest.json","items":[1,2,3]}\n'
            prefix = full.decode().strip()[:-8]
            native = prefix + "… [8 characters omitted]"
            completion = fixture._completion(5, call_id, "exec_command", native)
            fixture.owner_events[3:5] = [event, completion]
            fixture._write_owner()
            connection = sqlite3.connect(fixture.db_path)
            connection.execute("ALTER TABLE sessions ADD COLUMN short_id INTEGER")
            connection.execute("UPDATE sessions SET short_id=810 WHERE id=?", (OWNER_SESSION,))
            connection.commit()
            connection.close()
            steps = (
                f"2026-11-05T00:00:00Z runtime.event kind=tool_call_started step_id=test-step "
                f"call_id={call_id} name=exec_command\n"
                f"2026-11-05T00:00:02Z runtime.event kind=tool_call_completed step_id=test-step "
                f"call_id={call_id} name=exec_command is_error=false\n"
            ).encode()
            fixture._write(fixture.owner_path.parent / "steps.log", steps)
            spool_root = fixture.root / "spool"
            original = spool_root / "810" / "1.log"
            fixture._write(original, full)
            timestamp = 1793836801
            os.utime(original, (timestamp, timestamp))
            full_ref = fixture._ref("complete-stdout.log", full)
            declaration = {
                "schema": "native-shell-full-output-v1",
                "invocation_locator": fixture._locator(OWNER_SESSION, 4),
                "completion_locator": fixture._locator(OWNER_SESSION, 5),
                "full_output_ref": full_ref, "original_spool_path": str(original),
            }
            declaration_ref = fixture._ref("full-output.json", canonical(declaration))
            core = {"subject": fixture.subject, "coverage": [{
                "event_locator": fixture._locator(OWNER_SESSION, 4),
                "evidence_refs": [declaration_ref]}]}
            calls = proof._response_calls(event)

            def recover():
                return proof._qualified_shell_outputs(
                    core, fixture.persistence, calls, {call_id: completion}, proof.ReferenceBudget())

            with patch.object(proof, "NATIVE_SPOOL_ROOT", spool_root), patch.object(
                    proof, "LEGACY_FACADE_FILTER_SHA256", {digest(source.encode())}):
                self.assertEqual(recover()[call_id], full.decode().strip())
                duplicate = original.with_name("2.log")
                fixture._write(duplicate, full)
                os.utime(duplicate, (timestamp, timestamp))
                with self.assertRaisesRegex(proof.ProofError, "ambiguous"):
                    recover()
                duplicate.unlink()
                os.utime(original, (timestamp + 5, timestamp + 5))
                with self.assertRaisesRegex(proof.ProofError, "lifecycle interval"):
                    recover()
                os.utime(original, (timestamp, timestamp))
                fixture._write(original, full + b"x")
                with self.assertRaisesRegex(proof.ProofError, "changed from its preserved"):
                    recover()
                fixture._write(original, full)
                os.utime(original, (timestamp, timestamp))
                changed = full + b"unprinted"
                declaration["full_output_ref"] = fixture._ref("changed-stdout.log", changed)
                core["coverage"][0]["evidence_refs"] = [
                    fixture._ref("full-output.json", canonical(declaration))]
                with self.assertRaisesRegex(proof.ProofError, "exactly reproduce native clipping"):
                    recover()

    def test_filtered_facade_header_requires_actual_manifest_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            invocation_id = "88888888-8888-4888-8888-888888888888"
            manifest = {"operation": "device.tree", "outcome": {
                "transport": "completed", "effect": "unknown", "error": None, "data": {}},
                "artifacts": []}
            reference = fixture._ref(invocation_id + "/manifest.json", canonical(manifest))
            inventory = [{"invocation_id": invocation_id, "object_ref": reference}]
            source = "reviewed output filter"
            resolve = proof._statements(
                fixture.owner_events[3]["payload"]["native_tool_presentation"]["call-4"]["Command"])[0]
            command = (resolve + '; RESOURCE_LEASE_RUN_DIRECTORY="$run_dir" '
                       '.kent/adapters/android/android-tools --config '
                       '"$HOME/.config/kent/android-tools/test.json" device tree --arguments '
                       '\'{"device_serial":"emulator-5574"}\' | ruby -rjson -e \'' + source + "'")
            event, call_id = fixture._native_call(
                1, "exec_command", {"cmd": command}, {"ToolName": "exec_command", "Command": command})
            calls = proof._response_calls(event)

            def check(value):
                done = fixture._completion(2, call_id, "exec_command", json.dumps(value))
                return proof._native_facade_inventory(calls, {call_id: done}, fixture.subject, inventory)

            with patch.object(proof, "LEGACY_FACADE_FILTER_SHA256", {digest(source.encode())}):
                value = {"error": None, "manifest": reference["path"], "safe_controls": []}
                self.assertEqual(check(value), set())
                # Never infer receipt fields that the actual projection did not emit.
                self.assertNotIn("transport", value)
                self.assertEqual(manifest["outcome"]["effect"], "unknown")
                with self.assertRaisesRegex(proof.ProofError, "not bound"):
                    check({**value, "manifest": "/foreign/manifest.json"})
                with self.assertRaisesRegex(proof.ProofError, "result count"):
                    check({"error": None, "safe_controls": []})
                with self.assertRaisesRegex(proof.ProofError, "differs from the original"):
                    check({"manifest": reference["path"], "safe_controls": []})

    def test_agent_session_cannot_answer_native_approval_question(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            fixture.owner_events[7]["payload"]["answered_by_session_id"] = REVIEWERS[0]
            fixture._write_owner()
            fixture._write(fixture.request_path, canonical(fixture.request))
            with self.assertRaisesRegex(proof.ProofError, "attributed to an Agent"):
                fixture.verify()

    def test_native_question_runtime_source_version_hash_and_contents_are_pinned(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            with patch.object(proof, "KENT_QUESTION_SOURCE_SHA256",
                              {**fixture.question_source_hashes,
                               "ask_question_tool.go": "0" * 64}):
                with self.assertRaisesRegex(proof.ProofError, "reviewed version pin"):
                    proof._validate_kent_question_runtime(
                        fixture.runtime_ref, proof.ReferenceBudget(),
                    )
            with patch.object(proof, "KENT_QUESTION_SOURCE_SHA256",
                              fixture.question_source_hashes):
                altered = dict(fixture.runtime_ref)
                altered.update(fixture._ref("runtime-schema-unsupported.json", canonical({
                    "schema": proof.KENT_QUESTION_PROTOCOL_SCHEMA,
                    "kent_version": "2.14.0",
                    "source_refs": fixture.question_source_refs,
                })))
                with self.assertRaisesRegex(proof.ProofError, "version is unsupported"):
                    proof._validate_kent_question_runtime(
                        altered, proof.ReferenceBudget(),
                    )
                fixture._write(
                    Path(fixture.question_source_refs[0]["path"]),
                    b"mutated source that no longer matches its immutable reference\n",
                )
                with self.assertRaisesRegex(proof.ProofError, "immutable reference changed"):
                    proof._validate_kent_question_runtime(
                        fixture.runtime_ref, proof.ReferenceBudget(),
                    )

    def test_historical_2_12_question_source_remains_qualified(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory), question_version="2.12.0")
            self.assertEqual(fixture.verify().native_question_version, "2.12.0")

    def test_effect_cli_requires_exact_requested_2_13_version(self):
        with patch.object(proof.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stderr="", stdout="2.13.0\n")):
            self.assertEqual(proof._native_cli_version(Path("/qualified/kent")), "2.13.0")
        with patch.object(proof.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stderr="", stdout="2.12.0\n")):
            with self.assertRaisesRegex(proof.ProofError, "requested 2.13"):
                proof._native_cli_version(Path("/qualified/kent"))

    def test_apply_preserves_unknown_effects_and_exact_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory), terminal_null_generation=True)
            verified = fixture.verify()
            terminal = {
                "state": "completed", "cleanup_pending": False,
                "quarantined": False, "endpoint": None, "id": "lease-1",
                "slot": "slot-a", "generation": None, "observed_at": 123.0,
            }
            with patch.dict(os.environ, {"KENT_PERSISTENCE_ROOT": str(fixture.persistence),
                                         "KENT_SESSION_ID": OWNER_SESSION,
                                         "KENT_STEP_ID": fixture.step}), patch.object(
                    proof, "KENT_QUESTION_SOURCE_SHA256", fixture.question_source_hashes), patch.object(
                    proof, "LEGACY_PRODUCER_SHA256", fixture.producer_hashes), patch.object(
                    proof, "LEGACY_PROJECT_DELEGATE_SHA256", fixture.delegate_hashes), patch.object(
                    proof, "_native_owner_active"), patch.object(
                    proof, "_inspect_terminal_lease", return_value=terminal):
                first = proof.apply_terminal_reconciliation(verified)
            fixture.next_reconciliation_call('{"phase":"finished"}')
            verified = fixture.verify()
            with patch.dict(os.environ, {"KENT_PERSISTENCE_ROOT": str(fixture.persistence),
                                         "KENT_SESSION_ID": OWNER_SESSION,
                                         "KENT_STEP_ID": fixture.step}), patch.object(
                    proof, "KENT_QUESTION_SOURCE_SHA256", fixture.question_source_hashes), patch.object(
                    proof, "LEGACY_PRODUCER_SHA256", fixture.producer_hashes), patch.object(
                    proof, "LEGACY_PROJECT_DELEGATE_SHA256", fixture.delegate_hashes):
                retry = proof.apply_terminal_reconciliation(verified)
            self.assertFalse(first.already_applied)
            self.assertTrue(retry.already_applied)
            self.assertEqual(first.status["unknown_effects"], fixture.status["unknown_effects"])
            self.assertEqual(first.status["error"], fixture.status["error"])
            self.assertEqual(first.status, retry.status)

    def test_existing_finish_barrier_is_preserved_not_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            fixture._write(fixture.run / "finish.json", canonical({"cancel": False}))
            verified = fixture.verify()
            terminal = {
                "state": "completed", "cleanup_pending": False,
                "quarantined": False, "endpoint": None, "id": "lease-1",
                "slot": "slot-a", "generation": None, "observed_at": 123.0,
            }
            with patch.dict(os.environ, {"KENT_PERSISTENCE_ROOT": str(fixture.persistence),
                                         "KENT_SESSION_ID": OWNER_SESSION,
                                         "KENT_STEP_ID": fixture.step}), patch.object(
                    proof, "KENT_QUESTION_SOURCE_SHA256", fixture.question_source_hashes), patch.object(
                    proof, "LEGACY_PRODUCER_SHA256", fixture.producer_hashes), patch.object(
                    proof, "LEGACY_PROJECT_DELEGATE_SHA256", fixture.delegate_hashes), patch.object(
                    proof, "_native_owner_active"), patch.object(
                    proof, "_inspect_terminal_lease", return_value=terminal):
                self.assertEqual(proof.apply_terminal_reconciliation(verified).status["phase"], "finished")

    def test_persisted_intent_retries_after_status_write_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            verified = fixture.verify()
            terminal = {
                "state": "completed", "cleanup_pending": False,
                "quarantined": False, "endpoint": None, "id": "lease-1",
                "slot": "slot-a", "generation": None, "observed_at": 123.0,
            }
            original_write = proof._atomic_write
            with patch.dict(os.environ, {"KENT_PERSISTENCE_ROOT": str(fixture.persistence),
                                         "KENT_SESSION_ID": OWNER_SESSION,
                                         "KENT_STEP_ID": fixture.step}), patch.object(
                    proof, "KENT_QUESTION_SOURCE_SHA256", fixture.question_source_hashes), patch.object(
                    proof, "LEGACY_PRODUCER_SHA256", fixture.producer_hashes), patch.object(
                    proof, "LEGACY_PROJECT_DELEGATE_SHA256", fixture.delegate_hashes), patch.object(
                    proof, "_native_owner_active"), patch.object(
                    proof, "_inspect_terminal_lease", return_value=terminal), patch.object(
                    proof, "_atomic_write", side_effect=OSError("simulated replace failure")):
                with self.assertRaisesRegex(OSError, "simulated"):
                    proof.apply_terminal_reconciliation(verified)
            self.assertEqual(fixture.status_path.read_bytes(), fixture.status_bytes)
            fixture.next_reconciliation_call("error: simulated replace failure")
            verified = fixture.verify()
            with patch.dict(os.environ, {"KENT_PERSISTENCE_ROOT": str(fixture.persistence),
                                         "KENT_SESSION_ID": OWNER_SESSION,
                                         "KENT_STEP_ID": fixture.step}), patch.object(
                    proof, "KENT_QUESTION_SOURCE_SHA256", fixture.question_source_hashes), patch.object(
                    proof, "LEGACY_PRODUCER_SHA256", fixture.producer_hashes), patch.object(
                    proof, "LEGACY_PROJECT_DELEGATE_SHA256", fixture.delegate_hashes), patch.object(
                    proof, "_native_owner_active"), patch.object(
                    proof, "_inspect_terminal_lease", return_value=terminal), patch.object(
                    proof, "_atomic_write", wraps=original_write):
                retried = proof.apply_terminal_reconciliation(verified)
            self.assertEqual(retried.status["phase"], "finished")

    def test_stop_barrier_replacement_during_inspection_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            barrier = fixture.run / "finish.json"
            fixture._write(barrier, canonical({"schema": 1}))
            verified = fixture.verify()
            terminal = {
                "state": "completed", "cleanup_pending": False,
                "quarantined": False, "endpoint": None, "id": "lease-1",
                "slot": "slot-a", "generation": None, "observed_at": 123.0,
            }

            def inspect(*_args):
                # Same bytes with a new inode is still a changed barrier.
                barrier.unlink()
                fixture._write(barrier, canonical({"schema": 1}))
                return terminal

            with patch.dict(os.environ, {"KENT_PERSISTENCE_ROOT": str(fixture.persistence),
                                         "KENT_SESSION_ID": OWNER_SESSION,
                                         "KENT_STEP_ID": fixture.step}), patch.object(
                    proof, "KENT_QUESTION_SOURCE_SHA256", fixture.question_source_hashes), patch.object(
                    proof, "LEGACY_PRODUCER_SHA256", fixture.producer_hashes), patch.object(
                    proof, "LEGACY_PROJECT_DELEGATE_SHA256", fixture.delegate_hashes), patch.object(
                    proof, "_native_owner_active"), patch.object(
                    proof, "_inspect_terminal_lease", side_effect=inspect):
                with self.assertRaisesRegex(proof.ProofError, "stop signal changed"):
                    proof.apply_terminal_reconciliation(verified)
            self.assertEqual(fixture.status_path.read_bytes(), fixture.status_bytes)

    def test_markers_and_unconfirmed_terminal_observation_block(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            verified = fixture.verify()
            marker = fixture.run / "command-stop-uncertain.json"
            fixture._write(marker, canonical({"schema": 1}))
            with patch.dict(os.environ, {"KENT_PERSISTENCE_ROOT": str(fixture.persistence),
                                         "KENT_SESSION_ID": OWNER_SESSION,
                                         "KENT_STEP_ID": fixture.step}), patch.object(
                    proof, "KENT_QUESTION_SOURCE_SHA256", fixture.question_source_hashes), patch.object(
                    proof, "LEGACY_PRODUCER_SHA256", fixture.producer_hashes), patch.object(
                    proof, "LEGACY_PROJECT_DELEGATE_SHA256", fixture.delegate_hashes), patch.object(
                    proof, "_native_owner_active"), patch.object(
                    proof, "_inspect_terminal_lease", return_value={}):
                with self.assertRaisesRegex(proof.ProofError, "marker"):
                    proof.apply_terminal_reconciliation(verified)
            marker.unlink()
            active = {
                "state": "active", "cleanup_pending": False, "quarantined": False,
                "endpoint": {"adb_serial": "emulator-5574"}, "id": "lease-1",
                "slot": "slot-a", "generation": 9,
            }
            with patch.dict(os.environ, {"KENT_PERSISTENCE_ROOT": str(fixture.persistence),
                                         "KENT_SESSION_ID": OWNER_SESSION,
                                         "KENT_STEP_ID": fixture.step}), patch.object(
                    proof, "KENT_QUESTION_SOURCE_SHA256", fixture.question_source_hashes), patch.object(
                    proof, "LEGACY_PRODUCER_SHA256", fixture.producer_hashes), patch.object(
                    proof, "LEGACY_PROJECT_DELEGATE_SHA256", fixture.delegate_hashes), patch.object(
                    proof, "_native_owner_active"), patch.object(
                    proof, "_inspect_terminal_lease", return_value=active):
                with self.assertRaisesRegex(proof.ProofError, "terminal and clean"):
                    proof.apply_terminal_reconciliation(verified)
            self.assertEqual(fixture.status_path.read_bytes(), fixture.status_bytes)

    def test_busy_lock_and_changed_preimage_block(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            verified = fixture.verify()
            fd = os.open(fixture.run / "helper.lock", os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(proof.ProofError, "busy"):
                    proof.apply_terminal_reconciliation(verified)
            finally:
                os.close(fd)
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProofFixture(Path(directory))
            verified = fixture.verify()
            fixture.status_path.write_bytes(canonical(dict(fixture.status, error="changed")) + b"\n")
            with patch.dict(os.environ, {"KENT_PERSISTENCE_ROOT": str(fixture.persistence),
                                         "KENT_SESSION_ID": OWNER_SESSION,
                                         "KENT_STEP_ID": fixture.step}), patch.object(
                    proof, "KENT_QUESTION_SOURCE_SHA256", fixture.question_source_hashes), patch.object(
                    proof, "LEGACY_PRODUCER_SHA256", fixture.producer_hashes), patch.object(
                    proof, "LEGACY_PROJECT_DELEGATE_SHA256", fixture.delegate_hashes), patch.object(
                    proof, "_native_owner_active"), patch.object(
                    proof, "_inspect_terminal_lease"):
                with self.assertRaisesRegex(proof.ProofError, "preimage changed"):
                    proof.apply_terminal_reconciliation(verified)


if __name__ == "__main__":
    unittest.main()
