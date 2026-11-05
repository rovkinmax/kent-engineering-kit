#!/usr/bin/env python3
"""Validate bounded native evidence for terminal-only legacy stop reconciliation.

This module deliberately has no command-line entry point. A caller must supply
the native-history and terminal-lease readers appropriate to its qualified
environment before it can request the narrowly scoped status replacement.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import selectors
import shlex
import shutil
import sqlite3
import signal
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


REQUEST_SCHEMA = "lease-stop-reconcile-request-v1"
CORE_SCHEMA = "native-start-stop-evidence-core-v1"
INTENT_SCHEMA = "lease-stop-reconcile-intent-v1"
QUALIFICATION_SCHEMA = "native-start-stop-qualification-envelope-v1"
AUDIT_SCHEMA = "lease-stop-audit-report-v1"
APPROVAL_SCHEMA = "lease-stop-reconcile-approval-v1"
RECONCILIATION_SCHEMA = "lease-stop-reconciliation-v1"
PRODUCER_CONTRACT = "native-start-borrowed-android-reviewed-history-v1"
# This reader qualifies the reviewed legacy producer closure, not arbitrary
# programs with similarly named methods. New producer semantics require review.
LEGACY_PRODUCER_SHA256 = {
    "stage-input": "e61ea24f3f8fe254c141b9998de78b13494f925bea695e7f4b021c8decc9ca2c",
    "resource-lease-client": "4fd7f884099e5fbb6195564d0b792b0ab09cc69da88415c676c3138ef19920f7",
    "android-tools": "a7d9d3502827799a6584892d82f495f2a9704094fa151781d8ba0322ebb89ee4",
    "android_tools_custody.py": "782470769557bc9327f19ede7b297b6666593ed8fd4feb3890dd7887868cf008",
    "android_tools_native.py": "97cad8d0fba456afcd31460fe862876fa762e2c486e6def10a2d5f83e43f0e1f",
    "android_tools_evidence.py": "1d5e7b63c27f972bb2757709a128e45226a7a7abdc0bb6cdbb24632771989d1d",
}
LEGACY_PROJECT_DELEGATE_SHA256 = {
    ".kent/adapters/mobile/resource-lease-client":
        "d9afbe8cd6d0dcf1a161df5f8aae6095ba57dad0b33a29a01eea77d1f9b6a8d3",
    ".kent/adapters/android/android-tools":
        "7645cef477e547d9cbba569bd61ac2aa0c014439d1e5f2165ddd29c47ee5f3ed",
}
PRIVATE_PREPARATION_SHA256 = {
    "prepare-private-input.rb": "66b6d49de7dbd29d6048b39e383411b27996d9ad9420fdafc7cbd2bc620e261a",
}
# Exact nonexecuting output-filter revisions reviewed for this legacy class.
# Their native input remains the source reference; no private program is vendored.
LEGACY_FACADE_FILTER_SHA256 = frozenset({
    "2d30abcb1c939526e0ed3ef8047b0911b5e5a65dca82ae46f5f5144b0ead073d",
    "8e89282d8bc545bbb5ec40bda5e1def9270dbdc459982c294da4d966ba8b9958",
    "c8628fa3f4071c2845c57c63f036dfb109fee681d7f7d8191080f3b50133077c",
    "b67cb13974a9305c7cef5e362357e16ef2a927100ac9f28021b237738e0ab7b6",
    "cfbf2296328e93b66ac7ae6be8d6f3540f8d8bff8eef254b70e57b35c3c96bdb",
    "53e444d13cea3547f1bb1c92b31374645f9cce5f6c057c084af58aeb8ed2c98a",
    "1f5c0b86f23737755648f2b478a81698c032cff55bcaa9fd45350b3c657565ff",
    "d112c698c7a5611807bf2bc5a1319e9f42bf5c8d718829b5dc8ca04d38812feb",
    "0ce518f36bd4766a5d51e21b27a4a9b8380d11a870f1f32b12544678c89ce9aa",
    "950eb0ba073eaca9d97a8d6258acba7eb3743753d695c6827c355771b891ef12",
    "9fba838623e57285c03ae180f05300c6584857d274e57d1a87f2629f953f5002",
    "60ee9410f25b7aec5064f134ce43e439df115c5f562a42a9917775749234632a",
    "49c60026b883e80e1afecca9e081ab07b87815b76f09d90950ef7cdfcd2736d9",
    "853635817f06134a15a4d4d7666517b54215dbb535cf93d55d78945df3899d2d",
    "8965de26849381d0cabe5e86cc5286f90f30338927f208b54b115d885475a7fd",
    "2f9ded71cdcef37e5323f9d4152236f9f04ab9dac0ac58d90ad20f55234371f5",
    "5fda326cb18816799b466be512084d661942a00ab16dde5f5ab9d6ed4cfd4f78",
})
KENT_QUESTION_PROTOCOL_SCHEMA = "kent-question-runtime-source-v1"
SUPPORTED_KENT_QUESTION_VERSIONS = frozenset({"2.12.0", "2.13.0"})
EFFECT_KENT_QUESTION_VERSION = "2.13.0"
KENT_QUESTION_SOURCE_SHA256 = {
    "ask_question_resolution.go": "aa9dfe7b1133f1a0e652cc330c77f6cc0a15be51cf5692654d03b3db1d22dfa1",
    "ask_question_tool.go": "43e613716bb9137d96504f0ea62c1f49723c6ee3513d5d5eb5f17b2f9fd14ac9",
    "question_history_projection.go": "0f3f65065176d41812e6514aac6248331f9e8127148e616ba0bc4d6fac0f0959",
}
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_NATIVE_BYTES = 128 * 1024 * 1024
MAX_COMMAND_BYTES = 256 * 1024
COMMAND_TIMEOUT = 10.0
MAX_EVENTS = 100_000
MAX_COVERAGE = 100_000
MAX_MANIFESTS = 20_000
MAX_REFERENCE_COUNT = 10_000
MAX_TOTAL_REFERENCE_BYTES = 128 * 1024 * 1024
MAX_LIST_ITEMS = 100_000
MAX_DEPTH = 24
NATIVE_SPOOL_ROOT = Path("/tmp/kent")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NATIVE_EVENT_KINDS = {
    "message", "tool_completed", "cache_request_observed",
    "cache_response_observed", "generation_context", "configuration_updated",
    "history_replaced",
}
REQUIRED_AUDIT_CLAIMS = {
    "native_owner_binding_exact",
    "source_closure_reviewed",
    "stage_input_stop_confirmed",
    "facade_manifest_coverage_complete",
    "process_settlement_complete",
    "direct_command_coverage_complete",
    "parent_stop_cause_rejected",
    "unknown_effects_preserved",
    "history_interval_complete",
    "history_tail_reviewed",
    "terminal_cleanup_confirmed",
    "crash_retry_and_cas_reviewed",
}
DISPOSITIONS = {
    "stage_input_stop_confirmed",
    "facade_process_settled",
    "qualified_pre_dispatch_rejection",
    "qualified_install_completed",
    "bounded_read_only_check_completed",
    "administrative_source_reviewed",
    "evidence_export_completed",
    # Reviewed execution boundary only: composites can write checkpoints,
    # steer, inspect, export, or attempt lifecycle commands. This is not a
    # success, stop, backend-effect, or absence-of-effect assertion.
    "mixed_administrative_execution_reviewed",
}


class ProofError(ValueError):
    """The supplied proof is incomplete, ambiguous, or unsafe to apply."""


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ProofError("duplicate JSON field")
        value[key] = item
    return value


def _reject_constant(_value):
    raise ProofError("non-finite JSON number")


def _check_tree(value, depth=0):
    if depth > MAX_DEPTH:
        raise ProofError("JSON nesting exceeds limit")
    if isinstance(value, dict):
        if len(value) > MAX_LIST_ITEMS:
            raise ProofError("JSON object exceeds field limit")
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProofError("JSON object key is not a string")
            if re.search(r"password|secret|token|otp|credential", key, re.I):
                raise ProofError("proof objects must not contain credentials")
            _check_tree(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_LIST_ITEMS:
            raise ProofError("JSON array exceeds item limit")
        for item in value:
            _check_tree(item, depth + 1)
    elif value is None or type(value) in (bool, int, str):
        return
    elif type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ProofError("non-finite JSON number")
    else:
        raise ProofError("unsupported JSON value")


def canonical_json(value) -> bytes:
    _check_tree(value)
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ProofError("value is not canonical JSON") from error


def _private_file(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise ProofError(f"cannot read proof object: {path.name}") from error
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ProofError(f"proof object is not an owner-private regular file: {path.name}")
    return info


def _native_source_file(path):
    """Native metadata may be readable; only its owner may mutate provenance."""
    try:
        info = path.lstat()
    except OSError as error:
        raise ProofError("native metadata source is unavailable") from error
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o7022):
        raise ProofError("native metadata source is not an owner-controlled regular file")
    return info


def read_json(path: Path, *, limit=MAX_FILE_BYTES):
    path = Path(path)
    before = _private_file(path)
    if before.st_size > limit:
        raise ProofError("proof object exceeds size limit")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ProofError("proof object changed while opening")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        after = os.fstat(fd)
        if (len(raw) > limit or len(raw) != opened.st_size
                or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)):
            raise ProofError("proof object changed or exceeded size limit")
    finally:
        os.close(fd)
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProofError("proof object is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ProofError("proof object must be a JSON object")
    _check_tree(value)
    return value, raw


def _closed(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ProofError(f"{label} has unsupported fields")
    return value


def _hash(value, label):
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ProofError(f"{label} is not a SHA-256 digest")
    return value


def _uuid(value, label):
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ProofError(f"{label} is not a UUID") from None
    if str(parsed) != value:
        raise ProofError(f"{label} is not a canonical UUID")
    return value


def _native_identifier(value, prefix, label):
    if not isinstance(value, str) or not value.startswith(f"{prefix}-"):
        raise ProofError(f"{label} is not a native {prefix} ID")
    suffix = value[len(prefix) + 1:]
    try:
        parsed = uuid.UUID(suffix)
    except (ValueError, TypeError, AttributeError):
        raise ProofError(f"{label} is not a canonical native {prefix} ID") from None
    if str(parsed) != suffix or parsed.version != 4:
        raise ProofError(f"{label} is not a canonical native {prefix} ID")
    return value


def _ref(value, label):
    _closed(value, {"path", "object_sha256", "byte_size"}, label)
    path = value["path"]
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ProofError(f"{label} path is not absolute")
    normalized = Path(path)
    if str(normalized) != str(normalized.resolve()):
        raise ProofError(f"{label} path is not canonical")
    digest = _hash(value["object_sha256"], f"{label} hash")
    size = value["byte_size"]
    if type(size) is not int or size < 1 or size > MAX_FILE_BYTES:
        raise ProofError(f"{label} size is invalid")
    return normalized, digest, size


def _native_locator(value, label):
    _closed(value, {"project_id", "session_id", "event_sequence", "event_sha256"}, label)
    _native_identifier(value["project_id"], "project", f"{label}.project_id")
    _uuid(value["session_id"], f"{label}.session_id")
    if type(value["event_sequence"]) is not int or value["event_sequence"] < 1:
        raise ProofError(f"{label} sequence is invalid")
    _hash(value["event_sha256"], f"{label} hash")
    return value


def _private_bytes(path: Path, *, limit=MAX_FILE_BYTES) -> bytes:
    info = _private_file(path)
    if info.st_size > limit:
        raise ProofError("private file exceeds size limit")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ProofError("private file changed while opening")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        after = os.fstat(fd)
        if (len(raw) != opened.st_size or len(raw) > limit
                or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)):
            raise ProofError("private file changed or exceeded size limit")
        return raw
    finally:
        os.close(fd)


def _verify_ref_bytes(value, label):
    path, expected_hash, expected_size = _ref(value, label)
    raw = _private_bytes(path)
    if len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ProofError(f"{label} immutable reference changed")
    return raw


class ReferenceBudget:
    def __init__(self):
        self.count = 0
        self.total_bytes = 0

    def verify(self, value, label):
        self.count += 1
        if self.count > MAX_REFERENCE_COUNT:
            raise ProofError("proof reference count exceeds limit")
        raw = _verify_ref_bytes(value, label)
        self.total_bytes += len(raw)
        if self.total_bytes > MAX_TOTAL_REFERENCE_BYTES:
            raise ProofError("proof references exceed aggregate byte limit")


def _resolve_ref(value, label):
    raw = _verify_ref_bytes(value, label)
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                         parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProofError(f"{label} is not JSON") from error
    if not isinstance(obj, dict):
        raise ProofError(f"{label} is not a JSON object")
    _check_tree(obj)
    return obj


def _native_session(locator, persistence_root: Path):
    """Read a bounded, stable native Session history from its canonical path."""
    locator = _native_locator(locator, "native event locator")
    project = persistence_root / "projects" / locator["project_id"]
    session = project / "sessions" / locator["session_id"]
    directories = (persistence_root, project, project / "sessions", session)
    for index, directory in enumerate(directories):
        try:
            info = directory.lstat()
        except OSError as error:
            raise ProofError("native history association is unavailable") from error
        is_session = index == len(directories) - 1
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o7022 or is_session and info.st_mode & 0o077):
            raise ProofError("native history directory association is unsafe")
    path = session / "events.jsonl"
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o077 or before.st_size > MAX_NATIVE_BYTES):
            raise ProofError("native Session history is not bounded and owner-private")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_NATIVE_BYTES + 1)
        after = os.fstat(fd)
        if (len(raw) != before.st_size or len(raw) > MAX_NATIVE_BYTES
                or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)):
            raise ProofError("native Session history changed while reading")
    finally:
        os.close(fd)
    lines = raw.splitlines()
    if not lines or len(lines) > MAX_EVENTS + 1:
        raise ProofError("native Session history is empty or exceeds event limit")
    try:
        header = json.loads(lines[0], object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProofError("native Session history header is invalid") from error
    if header != {"contract": "kent.session.events", "version": 2}:
        raise ProofError("native Session history schema is unsupported")
    events = []
    for sequence, line in enumerate(lines[1:], start=1):
        try:
            event = json.loads(
                line.decode("utf-8"), object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ProofError("native Session history contains an invalid event") from error
        if (not isinstance(event, dict) or event.get("seq") != sequence
                or event.get("kind") not in NATIVE_EVENT_KINDS
                or not isinstance(event.get("payload"), dict)):
            raise ProofError("native Session history event schema is unsupported")
        events.append(event)
    return events, lines[1:]


def _native_event(locator, persistence_root: Path):
    locator = _native_locator(locator, "native event locator")
    events, raw_lines = _native_session(locator, persistence_root)
    sequence = locator["event_sequence"]
    if sequence > len(events):
        raise ProofError("native event locator is beyond persisted history")
    if hashlib.sha256(raw_lines[sequence - 1]).hexdigest() != locator["event_sha256"]:
        raise ProofError("native event locator digest does not match")
    return events[sequence - 1]


def _response_calls(event):
    payload = event.get("payload", {})
    if payload.get("role") != "assistant":
        return {}
    response = payload.get("native_response")
    if not isinstance(response, dict) or response.get("format") != "openai-responses":
        raise ProofError("native assistant invocation lacks a supported response envelope")
    items = response.get("items")
    if not isinstance(items, list) or len(items) > MAX_LIST_ITEMS:
        raise ProofError("native response item inventory is invalid")
    presentations = payload.get("native_tool_presentation", {})
    if not isinstance(presentations, dict):
        raise ProofError("native tool presentations are invalid")
    calls = {}
    for encoded in items:
        if not isinstance(encoded, str):
            raise ProofError("native response item is not encoded JSON")
        try:
            raw = base64.b64decode(encoded, validate=True)
            item = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant)
        except (ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise ProofError("native response item is malformed") from error
        if not isinstance(item, dict):
            raise ProofError("native response item is not an object")
        if item.get("type") not in {"function_call", "custom_tool_call"}:
            continue
        custom = item["type"] == "custom_tool_call"
        call_id, name, arguments = item.get("call_id"), item.get("name"), item.get("arguments")
        if custom:
            if name != "patch" or not isinstance(item.get("input"), str):
                raise ProofError("unsupported native custom-tool invocation")
            arguments = json.dumps({"input": item["input"]})
        if (not isinstance(call_id, str) or not call_id or not isinstance(name, str)
                or not name or not isinstance(arguments, str) or call_id in calls):
            raise ProofError("native function call identity is invalid")
        try:
            parsed = json.loads(arguments, object_pairs_hook=_unique_object,
                                parse_constant=_reject_constant)
        except (ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise ProofError("native function call arguments are invalid") from error
        if not isinstance(parsed, dict):
            raise ProofError("native function call arguments are not an object")
        presentation = presentations.get(call_id)
        if (not isinstance(presentation, dict)
                or presentation.get("ToolName") != name):
            raise ProofError("native call is not joined to its presentation")
        if name == "exec_command" and parsed.get("cmd") != presentation.get("Command"):
            raise ProofError("native shell arguments differ from the presented command")
        if name == "ask_question" and parsed.get("question") != presentation.get("Question"):
            raise ProofError("native Question arguments differ from the presented question")
        calls[call_id] = {
            "name": name, "arguments": parsed, "presentation": presentation,
            "event": event, "output_kind": "custom" if custom else "function",
        }
    if set(presentations) != set(calls):
        raise ProofError("native tool presentation and invocation sets differ")
    return calls


def _completion_output(event, call):
    payload = event.get("payload", {})
    custom = call.get("output_kind") == "custom"
    image = call["name"] == "view_image"
    if (event.get("kind") != "tool_completed" or payload.get("name") != call["name"]
            or not isinstance(payload.get("call_id"), str)
            or event.get("step_id") != call["event"].get("step_id")
            or type(payload.get("is_error")) is not bool
            or (not custom and not isinstance(payload.get("output"), str)
                and not (image and isinstance(payload.get("output"), list)))
            or payload.get("output_kind") != ("custom" if custom else "function")
            or not isinstance(payload.get("presentation"), dict)
            or payload["presentation"].get("ToolName") != call["name"]):
        raise ProofError("native completion does not match its invocation and Step")
    providers = payload.get("provider_items")
    if not isinstance(providers, list) or len(providers) != 1:
        raise ProofError("native completion lacks one provider output")
    item = providers[0]
    raw = item.get("raw") if isinstance(item, dict) else None
    output_type = "custom_tool_call_output" if custom else "function_call_output"
    if (not isinstance(item, dict) or item.get("type") != output_type
            or item.get("call_id") != payload["call_id"]
            or item.get("name") != call["name"] or not isinstance(raw, dict)
            or raw.get("type") != output_type
            or raw.get("call_id") != payload["call_id"]):
        raise ProofError("provider output does not match the native completion")
    provider_output = raw.get("output")
    if custom:
        try:
            provider_output = json.loads(provider_output, object_pairs_hook=_unique_object,
                                         parse_constant=_reject_constant)
        except (TypeError, ValueError) as error:
            raise ProofError("native custom-tool output is not structured JSON") from error
    if provider_output != payload["output"]:
        raise ProofError("provider output does not match the native completion")
    return payload


def _native_trace(events, *, start_sequence=1, end_sequence=None, allow_pending=False):
    end_sequence = len(events) if end_sequence is None else end_sequence
    calls = {}
    completions = {}
    tool_messages = set()
    for event in events[start_sequence - 1:end_sequence]:
        payload = event["payload"]
        if event["kind"] == "message" and payload.get("role") == "assistant":
            for call_id, call in _response_calls(event).items():
                if call_id in calls:
                    raise ProofError("native call ID is duplicated in the audited interval")
                calls[call_id] = call
        elif event["kind"] == "tool_completed":
            call_id = payload.get("call_id")
            if call_id not in calls or call_id in completions:
                raise ProofError("native completion has no unique original invocation")
            _completion_output(event, calls[call_id])
            completions[call_id] = event
        elif event["kind"] == "message" and payload.get("role") == "tool":
            call_id = payload.get("tool_call_id")
            if call_id not in calls or call_id in tool_messages:
                raise ProofError("native tool message has no unique invocation")
            tool_messages.add(call_id)
    if allow_pending:
        pending = set(calls) - set(completions)
        if (len(pending) != 1 or not set(completions).issubset(tool_messages)
                or set(tool_messages) - set(calls)):
            raise ProofError("native tail has an ambiguous pending invocation")
        return calls, completions
    if set(calls) != set(completions) or set(calls) != tool_messages:
        raise ProofError("native invocation, completion and tool-message sets differ")
    return calls, completions


def _question_answerer(call, completion, session_id, native_question_version):
    payload = completion["payload"]
    if native_question_version not in SUPPORTED_KENT_QUESTION_VERSIONS:
        raise ProofError("native Question answerer semantics are not source-qualified")
    if "answered_by_session_id" in payload:
        return payload["answered_by_session_id"]
    # The qualified native encoder omits nil pointer fields. Absence alone is
    # not attribution: join the actual completed Question to Kent's projection,
    # which materializes the actor field explicitly.
    executable = shutil.which("kent")
    if not executable:
        raise ProofError("native Question actor history is unavailable")
    result = _command_json([
        str(Path(executable).resolve()), "questions", "list", "--session", session_id, "--json",
    ])
    questions = result.get("questions")
    if not isinstance(questions, list) or len(questions) > MAX_LIST_ITEMS:
        raise ProofError("native Question actor history is unsupported")
    answer = payload.get("question_answer")
    arguments = call["arguments"]
    if not isinstance(answer, dict) or type(completion.get("committed_at_unix_ms")) is not int:
        raise ProofError("native Question actor join lacks typed answer or committed timestamp")
    matched = []
    for row in questions:
        if not isinstance(row, dict) or row.get("question") != arguments.get("question", "").strip():
            continue
        try:
            instant = datetime.fromisoformat(row["at"].replace("Z", "+00:00"))
            timestamp = round(instant.timestamp() * 1000)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        selected = answer.get("selected_option_number")
        suggestions = arguments.get("suggestions", [])
        if selected is None:
            expected_answer = answer.get("freeform")
            commentary = None
        elif type(selected) is int and 1 <= selected <= len(suggestions):
            expected_answer = suggestions[selected - 1]
            commentary = answer.get("freeform")
        else:
            continue
        if (timestamp == completion["committed_at_unix_ms"]
                and row.get("selected_option_number") == selected
                and row.get("answer") == expected_answer
                and row.get("commentary") == commentary
                and "answered_by_session_id" in row):
            matched.append(row)
    if len(matched) != 1:
        raise ProofError("native Question actor is not joined to one genuine history record")
    return matched[0]["answered_by_session_id"]


def _effect_question_arguments(arguments):
    required = {"question", "suggestions"}
    if (not required.issubset(arguments)
            or set(arguments) - required - {"recommended_option_index"}
            or not isinstance(arguments["question"], str)
            or not isinstance(arguments["suggestions"], list)
            or not arguments["suggestions"]
            or any(not isinstance(value, str) for value in arguments["suggestions"])):
        return False
    if "recommended_option_index" in arguments:
        index = arguments["recommended_option_index"]
        if type(index) is not int or not 1 <= index <= len(arguments["suggestions"]):
            return False
    # Display preference never changes the explicit selected-option answer.
    return True


def _validate_effect_question(call, completion, session_id, native_question_version,
                              intent_hash, qualification_hash):
    args = call["arguments"]
    answer = completion["payload"].get("question_answer")
    if (call["name"] != "ask_question" or completion["payload"].get("is_error") is not False
            or not _effect_question_arguments(args)
            or args["suggestions"] != ["Approve", "Decline"]
            or intent_hash not in args["question"] or qualification_hash not in args["question"]
            or "terminal_status_reconciliation" not in args["question"]
            or not isinstance(answer, dict)
            or set(answer) - {"selected_option_number", "freeform"}
            or type(answer.get("selected_option_number")) is not int
            or answer["selected_option_number"] != 1
            or answer.get("freeform") not in (None, "")):
        raise ProofError("native approval does not name and approve the exact effect and hashes")
    actor = _question_answerer(call, completion, session_id, native_question_version)
    if actor is not None:
        _uuid(actor, "native Question answerer Session")
        raise ProofError("native Question answer is attributed to an Agent, not the User")


def _external_effect_approval(locator, root, native_question_version, intent_hash, qualification_hash):
    events, _lines = _native_session(locator, root)
    event = _native_event(locator, root)
    calls = _response_calls(event)
    if len(calls) != 1:
        raise ProofError("effect approval is not one original native Question")
    call_id, call = next(iter(calls.items()))
    completions = [entry for entry in events if entry["kind"] == "tool_completed"
                   and entry["payload"].get("call_id") == call_id]
    messages = [entry for entry in events if entry["kind"] == "message"
                and entry["payload"].get("role") == "tool"
                and entry["payload"].get("tool_call_id") == call_id]
    if (len(completions) != 1 or len(messages) != 1
            or not event["seq"] < completions[0]["seq"] < messages[0]["seq"]):
        raise ProofError("effect approval lacks its completed native Question sequence")
    _completion_output(completions[0], call)
    _validate_effect_question(call, completions[0], locator["session_id"],
                              native_question_version, intent_hash, qualification_hash)
    return event


def _reviewed_tail_declarations(core, budget):
    records = {}
    for reference in core["source_closure"]["auxiliary_refs"]:
        if Path(reference["path"]).name != "review-tail.json":
            continue
        budget.verify(reference, "reviewed native tail declaration")
        _path, declaration, _raw = _json_file_ref(reference, "reviewed native tail declaration")
        _closed(declaration, {"schema", "subject", "records"}, "reviewed native tail declaration")
        subject = {key: core["subject"][key] for key in ("project_id", "task_id", "session_id")}
        if declaration["schema"] != "native-stop-reviewed-tail-v1" or declaration["subject"] != subject:
            raise ProofError("reviewed native tail declaration belongs to a different subject")
        rows = declaration["records"]
        if not isinstance(rows, list) or not rows or len(rows) > 16 or records:
            raise ProofError("reviewed native tail declaration is empty, duplicated or exceeds its limit")
        for row in rows:
            _closed(row, {"invocation_locator", "purpose", "expected_answerer_session_id"},
                    "reviewed native tail record")
            locator = _native_locator(row["invocation_locator"], "reviewed tail invocation")
            if (row["purpose"] != "same_run_recovery_information"
                    or locator["project_id"] != subject["project_id"]
                    or locator["session_id"] != subject["session_id"]
                    or locator["event_sequence"] <= core["history"]["cutoff"]["event_sequence"]
                    or locator["event_sequence"] in records):
                raise ProofError("reviewed native tail record is not an exact recovery Question")
            _uuid(row["expected_answerer_session_id"], "reviewed factual answerer")
            records[locator["event_sequence"]] = row
    return records


def _validate_owner_tail(history, subject, persistence_root, request_path, qualification_path,
                         *, expected_intent_hash=None, expected_qualification_hash=None,
                         native_question_version=None, approval_locator=None,
                         reviewed_tail=None):
    events, raw_lines = _native_session(history["start"], persistence_root)
    start_sequence = history["start"]["event_sequence"]
    cutoff = history["cutoff"]
    cutoff_sequence = cutoff["event_sequence"]
    if (start_sequence > len(events) or cutoff_sequence > len(events)
            or hashlib.sha256(raw_lines[start_sequence - 1]).hexdigest()
            != history["start"]["event_sha256"]
            or hashlib.sha256(raw_lines[cutoff_sequence - 1]).hexdigest()
            != cutoff["event_sha256"]):
        raise ProofError("audited native history locators changed")
    for event in events[start_sequence - 1:cutoff_sequence]:
        if event["kind"] == "history_replaced":
            raise ProofError("native history replacement invalidates the audited interval")
    _native_trace(events, start_sequence=start_sequence, end_sequence=cutoff_sequence)

    tail = events[cutoff_sequence:]
    for event in tail:
        payload = event["payload"]
        if event["kind"] in {"cache_request_observed", "cache_response_observed", "tool_completed"}:
            continue
        if event["kind"] == "message" and payload.get("role") in {"assistant", "tool"}:
            continue
        raise ProofError("native history tail contains non-framework unreviewed activity")
    calls, completions = _native_trace(
        events, start_sequence=cutoff_sequence + 1, allow_pending=True,
    )
    pending_ids = set(calls) - set(completions)
    approvals = []
    reconciliations = []
    reviewed_tail = reviewed_tail or {}
    seen_reviews = set()
    previous_completion = cutoff_sequence
    reconciliation_argv = None
    for call_id, call in calls.items():
        name, args, presentation = call["name"], call["arguments"], call["presentation"]
        completed_event = completions.get(call_id)
        if name == "ask_question":
            if completed_event is None:
                raise ProofError("native approval Question has no completed answer")
            review = reviewed_tail.get(call["event"]["seq"])
            if review is not None:
                locator = review["invocation_locator"]
                if (hashlib.sha256(raw_lines[locator["event_sequence"] - 1]).hexdigest()
                        != locator["event_sha256"] or set(args) != {"question"}
                        or not isinstance(args["question"], str)):
                    raise ProofError("reviewed factual recovery Question differs from its native source")
                answer = completed_event["payload"].get("question_answer")
                if (completed_event["payload"].get("is_error") is not False
                        or not isinstance(answer, dict) or set(answer) != {"freeform"}
                        or not isinstance(answer["freeform"], str) or not answer["freeform"].strip()
                        or _question_answerer(call, completed_event, subject["session_id"],
                                              native_question_version)
                        != review["expected_answerer_session_id"]):
                    raise ProofError("reviewed factual recovery answer lacks its genuine native answerer")
                if reconciliations:
                    raise ProofError("factual recovery Question is not before the reconciliation sequence")
                seen_reviews.add(call["event"]["seq"])
                previous_completion = max(previous_completion, completed_event["seq"])
                continue
            completed = completed_event["payload"]
            if not _effect_question_arguments(args):
                raise ProofError("native approval Question has no exact text")
            answer = completed.get("question_answer")
            if (not isinstance(answer, dict) or set(answer) - {"selected_option_number", "freeform"}
                    or type(answer.get("selected_option_number")) is not int
                    or answer.get("selected_option_number") != 1
                    or answer.get("freeform") not in (None, "")):
                raise ProofError("native approval lacks typed Question answer and actor fields")
            answerer = _question_answerer(
                call, completed_event, subject["session_id"], native_question_version,
            )
            if answerer is None:
                if native_question_version not in SUPPORTED_KENT_QUESTION_VERSIONS:
                    raise ProofError("native User-answer semantics are not source-qualified")
            else:
                _uuid(answerer, "native Question answerer Session")
                raise ProofError("native Question answer is attributed to an Agent, not the User")
            approvals.append((call["event"], args, answer))
        elif name == "exec_command":
            command = presentation.get("Command")
            try:
                argv = shlex.split(command, posix=True)
            except (ValueError, TypeError) as error:
                raise ProofError("native history tail command is ambiguous") from error
            expected = ["reconcile", "--request", str(request_path),
                        "--qualification", str(qualification_path)]
            if (not isinstance(command, str) or len(argv) != 6
                    or Path(argv[0]).name != "resource-lease-client"
                    or argv[1:] != expected):
                raise ProofError("native history tail contains an unreviewed dispatch")
            if call_id in pending_ids:
                if (call["event"].get("step_id") != os.environ.get("KENT_STEP_ID")
                        or os.environ.get("KENT_SESSION_ID") != subject["session_id"]
                        or call_id != next(iter(pending_ids))):
                    raise ProofError("pending reconciliation is not the genuine current native Step")
            else:
                if completed_event["seq"] <= call["event"]["seq"]:
                    raise ProofError("reconciliation completion precedes its invocation")
            external_approval = approval_locator and approval_locator["session_id"] != subject["session_id"]
            if ((not external_approval and (
                    len(approvals) != 1 or call["event"]["seq"] <= approvals[0][0]["seq"]))
                    or call["event"]["seq"] <= previous_completion
                    or reconciliation_argv is not None and argv != reconciliation_argv):
                raise ProofError("reconciliation retries are not one ordered same-intent sequence")
            reconciliation_argv = argv
            previous_completion = (completed_event["seq"] if completed_event is not None
                                   else len(events) + 1)
            reconciliations.append(call["event"])
        else:
            raise ProofError("native history tail contains an unreviewed operation")
    external_approval = approval_locator and approval_locator["session_id"] != subject["session_id"]
    if (len(approvals) != (0 if external_approval else 1)
            or not reconciliations or len(pending_ids) != 1 or seen_reviews != set(reviewed_tail)):
        raise ProofError("native tail must contain one approval and a current reconciliation call")
    if external_approval:
        return None
    if expected_intent_hash is not None and expected_qualification_hash is not None:
        event, arguments, answer = approvals[0]
        question = arguments["question"]
        if (expected_intent_hash not in question or expected_qualification_hash not in question
                or "terminal_status_reconciliation" not in question
                or arguments.get("suggestions") != ["Approve", "Decline"]):
            raise ProofError("native Question does not name the exact approved effect and hashes")
        return event
    return approvals[0][0]


def _validate_subject(subject, run_directory):
    _closed(subject, {
        "project_id", "task_id", "session_id", "run_directory", "binding_sha256",
        "receipt_sha256", "lease_identity",
    }, "subject")
    _native_identifier(subject["project_id"], "project", "subject project")
    _native_identifier(subject["task_id"], "task", "subject task")
    _uuid(subject["session_id"], "subject session")
    if subject["run_directory"] != str(run_directory):
        raise ProofError("proof subject is for a different run")
    for key in ("binding_sha256", "receipt_sha256"):
        _hash(subject[key], f"subject {key}")
    identity = subject["lease_identity"]
    _closed(identity, {"id", "slot", "generation", "serial"}, "lease identity")
    for key in ("id", "slot", "serial"):
        if not isinstance(identity[key], str) or not identity[key]:
            raise ProofError(f"lease identity {key} is invalid")
    if type(identity["generation"]) is not int or identity["generation"] < 0:
        raise ProofError("lease identity generation is invalid")


_RESOLVE_RUN = re.compile(
    r'^run_dir="\$\(\.kent/scripts/workflow-checkpoint read --stage smoke --task '
    r'(?P<task>[A-Z][A-Z0-9]+-[0-9]+) --workspace "\$PWD" \| jq -er '
    r"'\.stage_data\.resource_lease_run_directory'"
    r'\)"$'
)
_STAGE_INVOKE = re.compile(
    r'^RESOURCE_LEASE_RUN_DIRECTORY="\$run_dir" \./tools/stage-input '
    r'--serial (?P<serial>[A-Za-z0-9._:-]{1,128}) --config "\$HOME/\.config/kent/'
    r'android-tools/[A-Za-z0-9._-]+\.json" --source "\$PWD/\.kent/runtime/'
    r'(?P<task>[A-Z][A-Z0-9]+-[0-9]+)/stage-input-private/[A-Za-z0-9_.-]+" '
    r'--account [A-Za-z0-9._-]+ --field (?:phone|code)$'
)


def _statements(command):
    """Split only literal top-level semicolons; never evaluate shell code."""
    if not isinstance(command, str) or not command or len(command) > MAX_COMMAND_BYTES:
        raise ProofError("qualified command is not bounded text")
    quote = None
    escaped = False
    start = 0
    parts = []
    for index, char in enumerate(command):
        if char in "\r\n`":
            raise ProofError("qualified command contains unsupported shell syntax")
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == ";":
            parts.append(command[start:index].strip())
            start = index + 1
    if quote or escaped:
        raise ProofError("qualified command has unbalanced quoting")
    parts.append(command[start:].strip())
    if not all(parts):
        raise ProofError("qualified command has empty statements")
    return parts


def _checkpoint_admin(statement, task):
    checkpoint = (r'\.kent/scripts/workflow-checkpoint {action} --stage smoke --task '
                  + re.escape(task) + r' --workspace "\$PWD"')
    match = re.fullmatch(
        checkpoint.format(action="read") + r" \| jq '(?P<jq>[^']+)' \| "
        + checkpoint.format(action="write"), statement,
    )
    if match is None:
        return False
    expression = match.group("jq")
    code = re.sub(r'"(?:[^"\\]|\\.)*"', '""', expression)
    return (expression.startswith((".stage_data.", ".next_action"))
            and not re.search(r"resource_lease_(?:run_directory|profile)|(?:^|\W)(?:input|inputs|include|import)\b",
                              code))


def _guarded_read_only(statement, serial):
    guard = (r'\.kent/adapters/mobile/resource-lease-client check --run-dir "\$run_dir" '
             r'>/dev/null && "\$HOME/Library/Android/sdk/platform-tools/adb" -s '
             + re.escape(serial) + r' shell ')
    return re.fullmatch(
        guard + r"(?:pidof [A-Za-z][A-Za-z0-9_.]+|dumpsys activity instrumentation"
        r" \| grep -c '[A-Za-z][A-Za-z0-9_.]+' \|\| true)", statement,
    ) is not None


def _stage_recipe(command):
    parts = _statements(command)
    resolves = [(i, _RESOLVE_RUN.fullmatch(part)) for i, part in enumerate(parts)
                if _RESOLVE_RUN.fullmatch(part)]
    stages = [(i, _STAGE_INVOKE.fullmatch(part)) for i, part in enumerate(parts)
              if _STAGE_INVOKE.fullmatch(part)]
    if len(resolves) != 1 or len(stages) != 1:
        raise ProofError("Stage-input command does not match the qualified checkpoint recipe")
    resolve_index, resolve = resolves[0]
    stage_index, stage = stages[0]
    task, serial = stage.group("task"), stage.group("serial")
    if resolve_index >= stage_index or resolve.group("task") != task:
        raise ProofError("Stage-input run resolution order or task differs")
    preparation = None
    admin_before = admin_after = 0
    read_checks = False
    for index, part in enumerate(parts):
        if index in (resolve_index, stage_index):
            continue
        if _checkpoint_admin(part, task):
            if index < stage_index:
                admin_before += 1
            else:
                admin_after += 1
            continue
        if resolve_index < index < stage_index and _guarded_read_only(part, serial):
            read_checks = True
            continue
        match = re.fullmatch(
            r"ruby (build/kent-workflow/" + re.escape(task)
            + r"/[A-Za-z0-9._-]+/prepare-private-input\.rb)", part,
        )
        if index == 0 and index < resolve_index and match and preparation is None:
            preparation = match.group(1)
            continue
        raise ProofError("Stage-input command has an unqualified surrounding producer")
    return serial, preparation, admin_before, admin_after, read_checks


def _stage_input_call(call, completion, *, qualify_preparation=None):
    command = call["presentation"].get("Command")
    serial, preparation, admin_before, admin_after, read_checks = _stage_recipe(command)
    if preparation is not None:
        if qualify_preparation is None:
            raise ProofError("private preparation lacks its qualified native source revision")
        qualify_preparation(preparation, call)
    lines = completion["payload"].get("output", "").splitlines()
    if len(lines) < 1 + admin_before + admin_after + bool(preparation):
        raise ProofError("Stage-input output lacks the recipe readback position")
    result_index = len(lines) - admin_after - 1
    try:
        result = json.loads(lines[result_index], object_pairs_hook=_unique_object)
    except (ValueError, json.JSONDecodeError) as error:
        raise ProofError("Stage-input result is not structurally attributable") from error
    if (not isinstance(result, dict) or set(result) != {"status", "readback", "reason"}
            or result.get("status") != "action_accepted" or result.get("reason") is not None
            or result.get("readback") not in {"equal", "different", "unavailable"}):
        raise ProofError("Stage-input output does not prove post-cleanup acceptance")
    before_checkpoints = after_checkpoints = 0
    for index, line in enumerate(lines):
        if index == result_index:
            continue
        if index < result_index and read_checks and re.fullmatch(r"[0-9]+(?: [0-9]+)*", line):
            continue
        try:
            record = json.loads(line, object_pairs_hook=_unique_object)
        except (ValueError, json.JSONDecodeError) as error:
            raise ProofError("Stage-input surrounding output is unattributable") from error
        if index == 0 and preparation is not None and record == {
                "status": "prepared_owner_private_sources", "original_account_match": True,
                "source_count": 3, "private_directory_mode": "0700",
                "private_file_modes": "0600", "raw_values_emitted": False}:
            continue
        if (not isinstance(record, dict) or set(record) != {"checkpoint_path", "updated_at"}
                or not isinstance(record.get("checkpoint_path"), str)
                or not record["checkpoint_path"].endswith("/smoke-checkpoint.json")
                or not isinstance(record.get("updated_at"), str)):
            raise ProofError("Stage-input surrounding output is not a qualified checkpoint result")
        if index < result_index:
            before_checkpoints += 1
        else:
            after_checkpoints += 1
    if (before_checkpoints, after_checkpoints) != (admin_before, admin_after):
        raise ProofError("Stage-input surrounding checkpoint output count differs")
    return serial


def _qualify_preparation(script, call, owner_calls, closure):
    name = Path(script).name
    expected = PRIVATE_PREPARATION_SHA256.get(name)
    refs = [ref for ref in closure["auxiliary_refs"] if Path(ref["path"]).name == name]
    if expected is None or len(refs) != 1 or refs[0]["object_sha256"] != expected:
        raise ProofError("private preparation lacks its exact reviewed source revision")
    source = _verify_ref_bytes(refs[0], "private preparation source")
    creation = []
    for candidate in owner_calls.values():
        if candidate["name"] != "patch" or candidate["event"]["seq"] >= call["event"]["seq"]:
            continue
        text = candidate["arguments"].get("input", "")
        lines = text.splitlines()
        headers = [i for i, line in enumerate(lines)
                   if line.startswith(("*** Add File: ", "*** Update File: ", "*** Delete File: "))
                   and line.split(": ", 1)[1].endswith("/" + script)]
        for index in headers:
            if not lines[index].startswith("*** Add File: "):
                raise ProofError("private preparation source was changed after its reviewed creation")
            body = []
            for line in lines[index + 1:]:
                if line.startswith("*** "):
                    break
                if not line.startswith("+"):
                    raise ProofError("private preparation creation is not an exact Add File")
                body.append(line[1:])
            if ("\n".join(body) + "\n").encode() != source:
                raise ProofError("private preparation source differs from its actual native patch")
            creation.append(candidate)
    if len(creation) != 1:
        raise ProofError("private preparation lacks one original native source creation")


def _start_administration(statement, task):
    checkpoint = (r'\.kent/scripts/workflow-checkpoint {action} --stage smoke --task '
                  + re.escape(task) + r' --workspace "\$PWD"')
    return re.fullmatch(
        checkpoint.format(action="read")
        + r''' \| jq --arg newrun "\$HOME/[A-Za-z0-9/._-]+-\$\(uuidgen \| tr '''
        + r"""'\[:upper:\]' '\[:lower:\]'\)" '[^']+' \| """
        + checkpoint.format(action="write"), statement,
    ) is not None


def _validate_native_start(events, calls, completions, history, subject, binding):
    starts = [call for call in calls.values()
              if call["event"]["seq"] == history["start"]["event_sequence"]]
    if len(starts) != 1 or starts[0]["name"] != "exec_command":
        raise ProofError("history does not start with the native client producer")
    call = starts[0]
    command = call["presentation"].get("Command")
    parts = _statements(command)
    candidates = []
    task = None
    for part in parts:
        resolve = _RESOLVE_RUN.fullmatch(part)
        if resolve:
            if task is not None:
                raise ProofError("native start has multiple run resolutions")
            task = resolve.group("task")
        if re.match(r"(?:\.kent/adapters/mobile/|\./)resource-lease-client start ", part):
            candidates.append(part)
    if len(candidates) != 1 or candidates[0] != parts[-1]:
        raise ProofError("native start is not one final qualified client invocation")
    argv = shlex.split(candidates[0])
    expected_flags = ["--config", "--profile", "--run-dir", "--project", "--task", "--session"]
    if (len(argv) != 14 or argv[2::2] != expected_flags
            or argv[9] != subject["project_id"] or argv[11] != subject["task_id"]
            or argv[13] not in (subject["session_id"], "$KENT_SESSION_ID")
            or argv[5] != binding.get("profile")
            or argv[7] not in (subject["run_directory"], "$run_dir")):
        raise ProofError("native start arguments do not bind the original run and owner")
    if argv[7] == "$run_dir" and task is None:
        raise ProofError("native start lacks its original checkpoint run resolution")
    if any(not (_RESOLVE_RUN.fullmatch(part) or task and _start_administration(part, task))
           for part in parts[:-1]):
        raise ProofError("native start contains an unqualified surrounding producer")
    completion = next(done for cid, done in completions.items() if calls[cid] is call)
    output = completion["payload"].get("output", "")
    if completion["payload"].get("presentation", {}).get("MovedToBackground") is True:
        match = re.search(r"Process moved to background with ID ([A-Za-z0-9._:-]+)\.", output)
        notices = [e for e in events if e["payload"].get("message_type") == "background_notice"
                   and match and e["payload"].get("name") == match.group(1)]
        if len(notices) != 1:
            raise ProofError("native start lacks its actual framework completion")
        output = notices[0]["payload"].get("content", "")
    try:
        result = json.loads(output.splitlines()[-1], object_pairs_hook=_unique_object)
    except (IndexError, ValueError) as error:
        raise ProofError("native start output is not attributable to the producer") from error
    identity = subject["lease_identity"]
    lease = result.get("lease") if isinstance(result, dict) else None
    if (not isinstance(lease, dict) or "error" not in result
            or result.get("run_directory") != subject["run_directory"]
            or result.get("phase") != "active" or result.get("error") is not None
            or any(lease.get(key) != identity[key] for key in ("id", "slot", "generation"))
            or lease.get("owner") != binding.get("owner")
            or lease.get("capability") != binding.get("capability")
            or lease.get("state") != "active" or lease.get("cleanup_pending") is not False
            or lease.get("quarantined") is not False
            or not isinstance(lease.get("endpoint"), dict)
            or lease["endpoint"].get("adb_serial") != identity["serial"]):
        raise ProofError("native acquisition result does not bind the historical lease")


def _facade_producers(call, subject):
    if call["name"] != "exec_command":
        return []
    parts = _statements(call["presentation"].get("Command"))
    producers = []
    resolve = None
    for part in parts:
        match = _RESOLVE_RUN.fullmatch(part)
        if match:
            if resolve is not None:
                raise ProofError("facade command has multiple run resolutions")
            resolve = match
            continue
        match = re.fullmatch(
            r'''RESOURCE_LEASE_RUN_DIRECTORY="\$run_dir" \.kent/adapters/android/android-tools '''
            r'''--config "\$HOME/\.config/kent/android-tools/[A-Za-z0-9._-]+\.json" '''
            r"""device (?P<operation>[a-z-]+) --arguments '(?P<arguments>[^']*)'"""
            r"(?: \| ruby (?P<filter>.+))?", part,
        )
        if not match:
            if "echo " in part or "printf " in part:
                raise ProofError("facade history has an unqualified output producer")
            continue
        if resolve is None:
            raise ProofError("facade invocation lacks its preceding run resolution")
        filtered = match.group("filter") is not None
        if filtered:
            argv = shlex.split(match.group("filter"))
            if (len(argv) not in (3, 4) or argv[-2] != "-e"
                    or argv[0] != "-rjson"
                    or len(argv) == 4 and argv[1] not in ("-rrexml/document", "-rdigest")
                    or hashlib.sha256(argv[-1].encode()).hexdigest()
                    not in LEGACY_FACADE_FILTER_SHA256):
                raise ProofError("facade output filter differs from the reviewed native source")
        arguments = json.loads(match.group("arguments"), object_pairs_hook=_unique_object,
                               parse_constant=_reject_constant)
        if (not isinstance(arguments, dict)
                or arguments.get("device_serial") != subject["lease_identity"]["serial"]):
            raise ProofError("facade invocation has a different original serial")
        producers.append((f"device.{match.group('operation')}", filtered))
    return producers


def _native_facade_inventory(calls, completions, subject, inventory, *, recovered_outputs=None):
    records = {}
    for item in inventory:
        path, manifest, _raw = _json_file_ref(item["object_ref"], "manifest object")
        records[path] = manifest
    attributed = {}
    rejected = set()
    for call_id, call in calls.items():
        if call["name"] != "exec_command":
            continue
        output = (recovered_outputs or {}).get(call_id, completions[call_id]["payload"].get("output"))
        # Administrative commands may contain Ruby/heredoc scripts; they do not
        # become facade producers by printing a matching manifest field.
        command = call["presentation"].get("Command", "")
        producers = (_facade_producers(call, subject)
                     if ".kent/adapters/android/android-tools --config " in command else [])
        outputs = []
        for line in output.splitlines():
            try:
                value = json.loads(line, object_pairs_hook=_unique_object,
                                   parse_constant=_reject_constant)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and (
                    "local_manifest" in value or "manifest" in value
                    or value == {"error": "lease_binding_invalid", "effect": "not_dispatched"}):
                outputs.append(value)
        if len(outputs) != len(producers):
            raise ProofError("facade result count differs from its qualified native invocations")
        for (operation, filtered), value in zip(producers, outputs):
            if value == {"error": "lease_binding_invalid", "effect": "not_dispatched"}:
                if filtered:
                    raise ProofError("filtered facade result cannot prove pre-dispatch rejection")
                rejected.add(call_id)
                continue
            key = "manifest" if filtered else "local_manifest"
            path_value = value.get(key)
            if not isinstance(path_value, str):
                raise ProofError("facade result is not emitted by its qualified producer")
            path = Path(path_value)
            manifest = records.get(path)
            if (not path.is_absolute() or path.name != "manifest.json" or manifest is None
                    or manifest.get("operation") != operation):
                raise ProofError("facade invocation is not bound to its original finished manifest")
            invocation_id = _uuid(path.parent.name, "facade invocation ID")
            if invocation_id in attributed:
                raise ProofError("native facade invocation ID is duplicated")
            if filtered:
                if "error" not in value or value["error"] != manifest["outcome"]["error"]:
                    raise ProofError("facade filter result differs from the original outcome")
            else:
                if (set(value) != {"transport", "effect", "error", "data", "artifacts", "local_manifest"}
                        or any(value[k] != manifest["outcome"][k] for k in ("transport", "effect", "error"))
                        or _legacy_evidence_data(value["data"]) != manifest["outcome"]["data"]
                        or not isinstance(value["artifacts"], list)
                        or len(value["artifacts"]) != len(manifest["artifacts"])):
                    raise ProofError("facade result differs from its original durable outcome")
                for native, stored in zip(value["artifacts"], manifest["artifacts"]):
                    expected_artifact = {
                        key: stored[key] for key in ("media_type", "sha256", "size_bytes",
                                                    "width", "height") if key in stored
                    }
                    expected_artifact["path"] = str(path.parent / "staging" / stored["name"])
                    if native != expected_artifact:
                        raise ProofError("facade artifact differs from its original durable manifest")
            attributed[invocation_id] = path
    expected = {item["invocation_id"]: Path(item["object_ref"]["path"]) for item in inventory}
    if attributed != expected:
        raise ProofError("native facade invocation IDs differ from started/finished manifest records")
    return rejected


def _native_tool_span(root, subject, call_id, call, completion):
    path = (root / "projects" / subject["project_id"] / "sessions"
            / subject["session_id"] / "steps.log")
    raw = _private_bytes(path, limit=MAX_NATIVE_BYTES)
    pattern = re.compile(
        r"^(?P<at>[0-9T:.\-]+Z) runtime\.event kind=tool_call_(?P<kind>started|completed) "
        r"step_id=" + re.escape(call["event"]["step_id"]) + r" call_id=" + re.escape(call_id)
        + r" name=exec_command(?: is_error=(?P<error>true|false))?$",
    )
    matches = {"started": [], "completed": []}
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError as error:
        raise ProofError("native full-output lifecycle log is not UTF-8") from error
    for line in lines:
        match = pattern.fullmatch(line)
        if match:
            timestamp = datetime.fromisoformat(match.group("at").replace("Z", "+00:00")).timestamp()
            if match.group("kind") == "completed" and (
                    match.group("error") != str(completion["payload"]["is_error"]).lower()):
                raise ProofError("native full-output completion facts differ from the original lifecycle")
            matches[match.group("kind")].append(timestamp)
    if (len(matches["started"]) != 1 or len(matches["completed"]) != 1
            or matches["started"][0] >= matches["completed"][0]):
        raise ProofError("native full-output call lacks one original lifecycle interval")
    return matches["started"][0], matches["completed"][0]


def _native_short_id(root, subject):
    database = root / "db" / "main.sqlite3"
    _native_source_file(database)
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=2)
    try:
        row = connection.execute(
            "SELECT short_id FROM sessions WHERE id=? AND project_id=?",
            (subject["session_id"], subject["project_id"]),
        ).fetchone()
        if row is None or type(row[0]) is not int or row[0] <= 0:
            raise ProofError("native original spool Session short ID is unavailable")
        return row[0]
    except sqlite3.Error as error:
        raise ProofError("native original spool Session binding is unsupported") from error
    finally:
        connection.close()


def _clip_matches(full_bytes, native_output):
    match = re.search(r"… \[([0-9]+) characters omitted\]$", native_output)
    if match is None or int(match.group(1)) < 1:
        return False
    try:
        full = full_bytes.decode("utf-8").strip()
    except UnicodeError:
        return False
    prefix = native_output[:match.start()]
    return full.startswith(prefix) and len(full) - len(prefix) == int(match.group(1))


def _qualified_shell_outputs(core, root, calls, completions, budget):
    """Recover original spooled output, never reconstruct a clipped JSON record."""
    recovered = {}
    for coverage in core["coverage"]:
        for reference in coverage["evidence_refs"]:
            if Path(reference["path"]).name != "full-output.json":
                continue
            budget.verify(reference, "original full-output declaration")
            _path, declaration, _raw = _json_file_ref(reference, "original full-output declaration")
            _closed(declaration, {"schema", "invocation_locator", "completion_locator",
                                  "full_output_ref", "original_spool_path"},
                    "original full-output declaration")
            if (declaration["schema"] != "native-shell-full-output-v1"
                    or declaration["invocation_locator"] != coverage["event_locator"]):
                raise ProofError("original full-output declaration is for a different invocation")
            completion_locator = _native_locator(
                declaration["completion_locator"], "original full-output completion",
            )
            if (any(completion_locator[key] != core["subject"][key]
                    for key in ("project_id", "session_id"))
                    or completion_locator["event_sequence"]
                    <= coverage["event_locator"]["event_sequence"]):
                raise ProofError("original full-output completion belongs to a different native history")
            invocation = _native_event(declaration["invocation_locator"], root)
            completion = _native_event(declaration["completion_locator"], root)
            matching = [(cid, call) for cid, call in calls.items() if call["event"] == invocation]
            if len(matching) != 1:
                raise ProofError("original full-output declaration has no unique audited invocation")
            call_id, call = matching[0]
            if (call["name"] != "exec_command" or completions[call_id] != completion
                    or call_id in recovered):
                raise ProofError("original full-output declaration has a foreign or duplicate completion")
            producers = _facade_producers(call, core["subject"])
            if len(producers) != 1 or not producers[0][1]:
                raise ProofError("full-output recovery requires one qualified filtered facade producer")
            _completion_output(completion, call)
            native = completion["payload"]["output"]
            budget.verify(declaration["full_output_ref"], "original complete stdout")
            full = _verify_ref_bytes(declaration["full_output_ref"], "original complete stdout")
            if not _clip_matches(full, native):
                raise ProofError("original full stdout does not exactly reproduce native clipping")
            original = Path(declaration["original_spool_path"])
            short_id = _native_short_id(root, core["subject"])
            directory = NATIVE_SPOOL_ROOT.resolve() / str(short_id)
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or not original.is_absolute() or original != original.resolve()
                    or original.parent != directory
                    or re.fullmatch(r"[0-9]+\.log", original.name) is None):
                raise ProofError("original full stdout is outside the private native Session spool")
            original_info = _private_file(original)
            if _private_bytes(original, limit=MAX_COMMAND_BYTES) != full:
                raise ProofError("original native spool changed from its preserved stdout reference")
            start, end = _native_tool_span(root, core["subject"], call_id, call, completion)
            if not start <= original_info.st_mtime_ns / 1e9 <= end:
                raise ProofError("original native spool is outside the exact call lifecycle interval")
            candidates = []
            for index, entry in enumerate(directory.iterdir()):
                if index >= MAX_MANIFESTS:
                    raise ProofError("native Session spool exceeds its bounded inventory limit")
                entry_info = entry.lstat()
                if (re.fullmatch(r"[0-9]+\.log", entry.name)
                        and start <= entry_info.st_mtime_ns / 1e9 <= end):
                    candidate = _private_bytes(entry, limit=MAX_COMMAND_BYTES)
                    if _clip_matches(candidate, native):
                        candidates.append(entry)
            if candidates != [original]:
                raise ProofError("original full stdout has ambiguous native spool provenance")
            final_info = original.lstat()
            identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                       value.st_mtime_ns, value.st_ctime_ns, value.st_mode)
            final_directory = directory.lstat()
            if (identity(original_info) != identity(final_info)
                    or (info.st_dev, info.st_ino, info.st_mode)
                    != (final_directory.st_dev, final_directory.st_ino, final_directory.st_mode)):
                raise ProofError("original native spool changed during provenance qualification")
            # The full native output is still a filtered projection. Its complete
            # JSON is joined below to independently validated durable manifests.
            recovered[call_id] = full.decode("utf-8").strip()
    return recovered


def _legacy_evidence_data(raw_data):
    """The sanitization projection of the exact pinned legacy evidence source."""
    data = {}
    if not isinstance(raw_data, dict):
        return data
    for key, item in raw_data.items():
        if key in {"captured", "completed", "visible"} and type(item) is bool:
            data[key] = item
        elif key in {"match_count", "duration_ms", "exit_code", "api_level", "http_status"}:
            if type(item) is int and 0 <= item <= 2**31 - 1:
                data[key] = item
        elif key == "state" and isinstance(item, str) and item in {"present", "absent", "unknown", "sent"}:
            data[key] = item
        elif key == "selector" and isinstance(item, dict) and item.get("field") in {"text", "resource_id"}:
            data[key] = {"field": item["field"]}
    return data


def _json_file_ref(value, label):
    path, _expected_hash, _expected_size = _ref(value, label)
    raw = _verify_ref_bytes(value, label)
    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                            parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProofError(f"{label} is not UTF-8 JSON") from error
    if not isinstance(record, dict):
        raise ProofError(f"{label} is not a JSON object")
    return path, record, raw


def _validate_kent_question_runtime(reference, budget):
    budget.verify(reference, "native Question runtime schema")
    _path, manifest, _raw = _json_file_ref(reference, "native Question runtime schema")
    _closed(manifest, {"schema", "kent_version", "source_refs"}, "native Question runtime schema")
    if (manifest["schema"] != KENT_QUESTION_PROTOCOL_SCHEMA
            or not isinstance(manifest["kent_version"], str)
            or manifest["kent_version"] not in SUPPORTED_KENT_QUESTION_VERSIONS):
        raise ProofError("native Question runtime source version is unsupported")
    refs = manifest["source_refs"]
    if not isinstance(refs, list) or len(refs) != len(KENT_QUESTION_SOURCE_SHA256):
        raise ProofError("native Question runtime source closure is incomplete")
    observed = set()
    markers = {
        "ask_question_resolution.go": (b"func questionAnswererLabel", b'return "User"',
                                       b'return "Agent "'),
        "ask_question_tool.go": (b"if answer.Answerer != nil", b"AnsweredBySessionID",),
        "question_history_projection.go": (b"completion.AnsweredBySessionID",
                                            b"AnsweredBySessionID: answerer"),
    }
    for index, ref in enumerate(refs):
        path, expected_hash, _size = _ref(ref, f"native Question source {index}")
        name = path.name
        if name not in KENT_QUESTION_SOURCE_SHA256 or name in observed:
            raise ProofError("native Question source identity is unsupported or duplicated")
        raw = _verify_ref_bytes(ref, f"native Question source {name}")
        budget.verify(ref, f"native Question source {name}")
        if (expected_hash != KENT_QUESTION_SOURCE_SHA256[name]
                or hashlib.sha256(raw).hexdigest() != KENT_QUESTION_SOURCE_SHA256[name]
                or any(marker not in raw for marker in markers[name])):
            raise ProofError("native Question source differs from its reviewed version pin")
        observed.add(name)
    if observed != set(KENT_QUESTION_SOURCE_SHA256):
        raise ProofError("native Question runtime source closure is incomplete")
    return manifest["kent_version"]


def _validate_core(core, run_directory, budget):
    _closed(core, {
        "schema", "producer_contract", "subject", "source_closure", "history",
        "coverage", "manifest_inventory",
    }, "evidence core")
    if core["schema"] != CORE_SCHEMA or core["producer_contract"] != PRODUCER_CONTRACT:
        raise ProofError("unsupported evidence core")
    _validate_subject(core["subject"], run_directory)
    closure = core["source_closure"]
    _closed(closure, {
        "candidate_ref", "source_refs", "runtime_schema_ref", "auxiliary_refs",
    }, "source closure")
    budget.verify(closure["candidate_ref"], "candidate source")
    producers = {}
    delegates = set()
    for name in ("source_refs", "auxiliary_refs"):
        values = closure[name]
        if not isinstance(values, list) or not values or len(values) > MAX_LIST_ITEMS:
            raise ProofError(f"{name} must be a bounded non-empty list")
        for index, value in enumerate(values):
            budget.verify(value, f"{name}[{index}]")
            path, digest, _size = _ref(value, f"{name}[{index}]")
            matched_delegates = [relative for relative, pinned in LEGACY_PROJECT_DELEGATE_SHA256.items()
                                 if pinned == digest and Path(relative).name == path.name]
            if matched_delegates:
                if name != "auxiliary_refs" or len(matched_delegates) != 1 or digest in delegates:
                    raise ProofError("project delegate source closure is ambiguous")
                delegates.add(digest)
                continue
            if path.name in LEGACY_PRODUCER_SHA256:
                if (path.name in producers
                        or digest != LEGACY_PRODUCER_SHA256[path.name]
                        or name == "source_refs" and path.name != "stage-input"):
                    raise ProofError("producer source differs from the exact reviewed closure")
                producers[path.name] = value
    if set(producers) != set(LEGACY_PRODUCER_SHA256):
        raise ProofError("reviewed producer source closure is incomplete")
    if delegates != set(LEGACY_PROJECT_DELEGATE_SHA256.values()):
        raise ProofError("actual project delegate source closure is incomplete")
    _candidate_path, candidate, _candidate_raw = _json_file_ref(
        closure["candidate_ref"], "candidate source",
    )
    files = candidate.get("files")
    if not isinstance(files, list) or len(files) > MAX_LIST_ITEMS:
        raise ProofError("candidate source has no bounded file inventory")
    stage_files = [row for row in files if isinstance(row, dict)
                   and row.get("path") == "tools/stage-input"]
    if (len(stage_files) != 1
            or stage_files[0].get("sha256") != LEGACY_PRODUCER_SHA256["stage-input"]
            or stage_files[0].get("mode") != "100755"):
        raise ProofError("candidate does not bind the invoked Stage-input producer bytes")
    for relative, digest in LEGACY_PROJECT_DELEGATE_SHA256.items():
        entries = [row for row in files if isinstance(row, dict) and row.get("path") == relative]
        if len(entries) != 1 or entries[0].get("sha256") != digest or entries[0].get("mode") != "100755":
            raise ProofError("candidate does not bind the actual project delegate source")
    runtime_version = _validate_kent_question_runtime(closure["runtime_schema_ref"], budget)
    history = core["history"]
    _closed(history, {
        "start", "async_completion", "audited_start_sequence", "cutoff",
    }, "history")
    start = _native_locator(history["start"], "history start")
    if start["project_id"] != core["subject"]["project_id"]:
        raise ProofError("history start belongs to another project")
    if history["async_completion"] is not None:
        asynchronous = _native_locator(history["async_completion"], "asynchronous completion")
        if (asynchronous["project_id"] != start["project_id"]
                or asynchronous["session_id"] != start["session_id"]):
            raise ProofError("asynchronous completion belongs to another Session")
    cutoff = _native_locator(history["cutoff"], "history cutoff")
    if (cutoff["project_id"] != core["subject"]["project_id"]
            or cutoff["session_id"] != core["subject"]["session_id"]
            or start["session_id"] != core["subject"]["session_id"]):
        raise ProofError("history interval belongs to another native Session")
    if (type(history["audited_start_sequence"]) is not int
            or history["audited_start_sequence"] != start["event_sequence"]
            or cutoff["event_sequence"] < start["event_sequence"]):
        raise ProofError("audited history interval is invalid")
    coverage = core["coverage"]
    if not isinstance(coverage, list) or not coverage or len(coverage) > MAX_COVERAGE:
        raise ProofError("coverage inventory is empty or exceeds its limit")
    coverage_ids = set()
    for index, item in enumerate(coverage):
        _closed(item, {"event_locator", "disposition", "evidence_refs"},
                f"coverage[{index}]")
        locator = _native_locator(item["event_locator"], f"coverage[{index}] event")
        if (locator["project_id"] != start["project_id"]
                or locator["session_id"] != start["session_id"]
                or not (start["event_sequence"] <= locator["event_sequence"]
                        <= cutoff["event_sequence"])):
            raise ProofError("coverage event is outside the audited native Session interval")
        key = (locator["project_id"], locator["session_id"], locator["event_sequence"])
        if key in coverage_ids:
            raise ProofError("duplicate native invocation coverage")
        coverage_ids.add(key)
        if item["disposition"] not in DISPOSITIONS:
            raise ProofError("coverage contains an unsupported disposition")
        refs = item["evidence_refs"]
        if not isinstance(refs, list) or not refs:
            raise ProofError("coverage disposition lacks evidence")
        for ref_index, ref in enumerate(refs):
            budget.verify(ref, f"coverage[{index}] evidence[{ref_index}]")
    inventory = core["manifest_inventory"]
    if not isinstance(inventory, list) or len(inventory) > MAX_MANIFESTS:
        raise ProofError("manifest inventory exceeds its limit")
    invocation_ids = set()
    for index, item in enumerate(inventory):
        _closed(item, {
            "invocation_id", "started_sha256", "manifest_sha256", "object_ref",
        }, f"manifest inventory[{index}]")
        invocation_id = _uuid(item["invocation_id"], "invocation ID")
        if invocation_id in invocation_ids:
            raise ProofError("duplicate manifest invocation ID")
        invocation_ids.add(invocation_id)
        started_hash = _hash(item["started_sha256"], "started record hash")
        manifest_hash = _hash(item["manifest_sha256"], "manifest hash")
        path, manifest, manifest_bytes = _json_file_ref(item["object_ref"], "manifest object")
        if (path.name != "manifest.json" or hashlib.sha256(manifest_bytes).hexdigest() != manifest_hash
                or manifest.get("schema") != "android-tools-run-v1"
                or manifest.get("invocation_id") != invocation_id
                or manifest.get("state") != "finished"):
            raise ProofError("manifest inventory does not reference its actual finished record")
        started_path = path.parent / "started.json"
        started_raw = _private_bytes(started_path, limit=MAX_FILE_BYTES)
        try:
            started = json.loads(started_raw.decode("utf-8"), object_pairs_hook=_unique_object,
                                 parse_constant=_reject_constant)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ProofError("started record is not valid UTF-8 JSON") from error
        if (hashlib.sha256(started_raw).hexdigest() != started_hash
                or not isinstance(started, dict)
                or set(started) != {"schema", "invocation_id", "started_at", "operation",
                                    "provider", "context", "state", "outcome"}
                or started.get("schema") != "android-tools-run-start-v1"
                or started.get("invocation_id") != invocation_id
                or started.get("state") != "started"
                or manifest.get("started_at") != started.get("started_at")
                or manifest.get("operation") != started.get("operation")
                or manifest.get("provider") != started.get("provider")
                or manifest.get("context") != started.get("context")):
            raise ProofError("started record and manifest identity differ")
        context = started.get("context")
        subject_identity = core["subject"]["lease_identity"]
        if (not isinstance(context, dict)
                or context.get("lease_id") != subject_identity["id"]
                or context.get("generation") != subject_identity["generation"]
                or context.get("device_serial") != subject_identity["serial"]):
            raise ProofError("facade start record is not bound to the original lease and serial")
        outcome = manifest.get("outcome")
        if (not isinstance(outcome, dict) or set(outcome) != {
                "transport", "effect", "error", "data"}
                or outcome.get("transport") not in {
                    "not_started", "completed", "failed", "timed_out", "unknown"}
                or outcome.get("effect") not in {
                    "not_dispatched", "not_applicable", "observed", "unknown"}
                or outcome.get("error") == "drain_unknown"
                or not isinstance(outcome.get("data"), dict)):
            raise ProofError("facade manifest outcome is invalid or process settlement is unknown")
        for artifact in manifest.get("artifacts", []):
            if not isinstance(artifact, dict):
                raise ProofError("facade manifest artifact is malformed")
            keys = set(artifact)
            if (not {"name", "media_type", "sha256", "size_bytes", "local_locator", "exported"}.issubset(keys)
                    or keys - {"name", "media_type", "sha256", "size_bytes", "local_locator",
                               "exported", "width", "height"}):
                raise ProofError("facade artifact schema is unsupported")
            digest = _hash(artifact.get("sha256"), "artifact digest")
            size = artifact.get("size_bytes")
            if (type(size) is not int or size < 1
                    or artifact.get("local_locator") != f"artifacts/{digest}"
                    or type(artifact.get("exported")) is not bool):
                raise ProofError("facade artifact metadata is invalid")
            artifact_path = path.parent / "artifacts" / digest
            artifact_raw = _private_bytes(artifact_path, limit=8 * 1024 * 1024)
            if len(artifact_raw) != size or hashlib.sha256(artifact_raw).hexdigest() != digest:
                raise ProofError("facade artifact bytes do not match their manifest")
        budget.verify(item["object_ref"], "manifest object")
    if invocation_ids and len(invocation_ids) > len(coverage):
        raise ProofError("manifest inventory is not covered by native history")
    return runtime_version


def _validate_intent(intent, core_hash, expected_preimage):
    _closed(intent, {
        "schema", "operation_id", "core_sha256", "expected_preimage_sha256",
        "permitted_mutation", "target_status", "application_timestamp_policy",
    }, "intent")
    if intent["schema"] != INTENT_SCHEMA:
        raise ProofError("unsupported reconciliation intent")
    if intent["application_timestamp_policy"] != "monotonic_at_application":
        raise ProofError("intent does not authorize the application timestamp")
    _uuid(intent["operation_id"], "reconciliation operation ID")
    if intent["core_sha256"] != core_hash:
        raise ProofError("intent is bound to a different evidence core")
    if intent["expected_preimage_sha256"] != expected_preimage:
        raise ProofError("intent is bound to a different status preimage")
    mutation = intent["permitted_mutation"]
    _closed(mutation, {"kind", "phase", "command_stop"}, "mutation")
    if mutation != {
        "kind": "terminal_status_only", "phase": "finished",
        "command_stop": "confirmed",
    }:
        raise ProofError("intent permits a non-terminal or broader mutation")
    target = intent["target_status"]
    _closed(target, {
        "phase", "command_stop", "preserve_error", "preserve_unknown_effects",
        "operation_id",
    }, "target status")
    if (target["phase"] != "finished" or target["command_stop"] != "confirmed"
            or target["preserve_error"] is not True
            or target["preserve_unknown_effects"] is not True
            or target["operation_id"] != intent["operation_id"]):
        raise ProofError("intent target status is unsafe")


def _native_assistant_text(event, label):
    if event.get("kind") != "message" or event.get("payload", {}).get("role") != "assistant":
        raise ProofError(f"{label} is not a native assistant message")
    response = event["payload"].get("native_response")
    if not isinstance(response, dict) or response.get("format") != "openai-responses":
        raise ProofError(f"{label} lacks the native response envelope")
    texts = []
    for encoded in response.get("items", []):
        if not isinstance(encoded, str):
            raise ProofError(f"{label} has an invalid native response item")
        try:
            item = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"),
                              object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant)
        except (ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise ProofError(f"{label} has a malformed native response item") from error
        if not isinstance(item, dict):
            raise ProofError(f"{label} has a non-object native response item")
        if item.get("type") == "message" and item.get("role") == "assistant":
            for part in item.get("content", []):
                if (isinstance(part, dict) and part.get("type") == "output_text"
                        and isinstance(part.get("text"), str)):
                    texts.append(part["text"])
    if len(texts) != 1:
        raise ProofError(f"{label} does not contain one native assistant report")
    return texts[0]


def _parse_native_report(event, schema, label):
    content = _native_assistant_text(event, label)
    try:
        report = json.loads(content, object_pairs_hook=_unique_object,
                             parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProofError(f"{label} content is not a structured report") from error
    _check_tree(report)
    if not isinstance(report, dict) or report.get("schema") != schema:
        raise ProofError(f"{label} schema is unsupported")
    return report


def _native_session_metadata(session_id, project_id, persistence_root):
    database = persistence_root / "db" / "main.sqlite3"
    _native_source_file(database)
    uri = f"file:{database.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        session_columns = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)")}
        required = {"id", "project_id", "parent_agent_session_id", "task_id",
                    "locked_json", "continuation_json"}
        if not required.issubset(session_columns):
            raise ProofError("native Session metadata schema is unsupported")
        row = connection.execute(
            "SELECT id,project_id,parent_agent_session_id,task_id,locked_json,continuation_json "
            "FROM sessions WHERE id=?", (session_id,),
        ).fetchone()
        if row is None or row["project_id"] != project_id:
            raise ProofError("native Session metadata association is unavailable")
        locked = json.loads(row["locked_json"])
        continuation = json.loads(row["continuation_json"])
        if not isinstance(locked, dict) or not isinstance(continuation, dict):
            raise ProofError("native Session lock snapshot is invalid")
        return dict(row), locked, continuation
    except (sqlite3.Error, ValueError, TypeError) as error:
        raise ProofError("native Session metadata read failed") from error
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass


def _assignment_output_matches(completion, reviewer_session_id, project_id, persistence_root):
    if completion["payload"].get("is_error") is not False:
        return False
    output = completion["payload"].get("output", "")
    # Preserve the explicit UUID producer. Short IDs are not UUID substrings:
    # qualify the exact native CLI response against the reviewer's native row.
    if output == f"started session {reviewer_session_id}":
        return True
    presentation = completion["payload"].get("presentation", {})
    if presentation.get("IsShell") is not True:
        return False
    background = presentation.get("MovedToBackground") is True
    if background:
        if presentation.get("ShellExitCode") is not None:
            return False
        prefix = r"Process moved to background with ID [1-9][0-9]*\. Output:\n"
    else:
        if (type(presentation.get("ShellExitCode")) is not int
                or presentation["ShellExitCode"] != 0):
            return False
        prefix = ""
    match = re.fullmatch(
        prefix + r'Started a new session, `kent run steer ([1-9][0-9]*) "prompt"` '
        r"to send messages while it runs\n?", output,
    )
    if match is None:
        return False
    return int(match.group(1)) == _native_short_id(persistence_root, {
        "project_id": project_id, "session_id": reviewer_session_id,
    })


def _verify_assignment(locator_event, reviewer_session_id, task_id, persistence_root,
                       project_id, core_hash, intent_hash, label):
    row, locked, continuation = _native_session_metadata(
        reviewer_session_id, project_id, persistence_root,
    )
    role = continuation.get("agent_role")
    allowed_roles = {"grill", "architecture-designer", "quality-reviewer"}
    prompt = locked.get("system_prompt")
    tools = locked.get("enabled_tools")
    if (role not in allowed_roles or not isinstance(prompt, str)
            or locked.get("has_system_prompt") is not True
            or locked.get("has_enabled_tools") is not True
            or "read-only" not in prompt.lower() or not isinstance(tools, list)
            or "patch" in tools or row.get("task_id") not in (None, task_id)):
        raise ProofError(f"{label} retained Session role is not an approved read-only reviewer")
    parent = row.get("parent_agent_session_id")
    if not isinstance(parent, str):
        raise ProofError(f"{label} has no native parent assignment")
    parent_project = persistence_root / "projects" / project_id / "sessions" / parent / "events.jsonl"
    parent_locator = {"project_id": project_id, "session_id": parent,
                      "event_sequence": 1, "event_sha256": "0" * 64}
    parent_events, parent_raw = _native_session(parent_locator, persistence_root)
    sequence = locator_event["event_sequence"]
    if (locator_event["session_id"] != parent or sequence > len(parent_events)
            or hashlib.sha256(parent_raw[sequence - 1]).hexdigest()
            != locator_event["event_sha256"]):
        raise ProofError(f"{label} does not bind its exact native parent invocation")
    # Assignment attribution needs this invocation's complete native join, not
    # interpretation of unrelated parent tools (for example a context handoff).
    # The audited owner's full interval still uses the strict complete trace.
    calls = _response_calls(parent_events[sequence - 1])
    matched = False
    for call_id, call in calls.items():
        if call["name"] != "exec_command":
            continue
        command = call["presentation"].get("Command", "")
        try:
            argv = shlex.split(command, posix=True)
        except ValueError:
            continue
        agent = None
        for index, value in enumerate(argv):
            if value.startswith("--agent="):
                agent = value.split("=", 1)[1]
            elif value == "--agent" and index + 1 < len(argv):
                agent = argv[index + 1]
        dispatches = [
            event for event in parent_events
            if call_id in event.get("payload", {}).get("native_tool_presentation", {})
        ]
        completions = [
            event for event in parent_events if event.get("kind") == "tool_completed"
            and event.get("payload", {}).get("call_id") == call_id
        ]
        messages = [
            event for event in parent_events if event.get("kind") == "message"
            and event.get("payload", {}).get("role") == "tool"
            and event.get("payload", {}).get("tool_call_id") == call_id
        ]
        if len(dispatches) != 1 or len(completions) != 1 or len(messages) != 1:
            continue
        completion = completions[0]
        _completion_output(completion, call)
        if (messages[0].get("step_id") != call["event"].get("step_id")
                or not sequence < completion["seq"] < messages[0]["seq"]):
            continue
        if (len(argv) >= 3 and Path(argv[0]).name == "kent" and argv[1] == "run"
                and agent == role and completion is not None
                and task_id in command and core_hash in command and intent_hash in command
                and _assignment_output_matches(
                    completion, reviewer_session_id, project_id, persistence_root)):
            matched = True
    if not matched:
        raise ProofError(f"{label} lacks its genuine native agent assignment call")
    return role


def _verify_report(
    report, core_hash, intent_hash, interval, label, *, resolver, budget, project_id,
):
    _closed(report, {
        "schema", "reviewer_session_id", "assignment_locator", "core_sha256",
        "intent_sha256", "interval", "coverage_complete", "facts", "findings",
    }, label)
    _uuid(report["reviewer_session_id"], f"{label} reviewer")
    assignment = _native_locator(report["assignment_locator"], f"{label} assignment")
    if report["core_sha256"] != core_hash or report["intent_sha256"] != intent_hash:
        raise ProofError(f"{label} does not bind the exact core and intent")
    if report["interval"] != interval or report["coverage_complete"] is not True:
        raise ProofError(f"{label} does not cover the full audited interval")
    facts, findings = report["facts"], report["findings"]
    if (not isinstance(facts, list) or not isinstance(findings, list)
            or any(not isinstance(item, str) or not item.strip() for item in findings)):
        raise ProofError(f"{label} must state factual conclusions and findings")
    claims = set()
    for index, fact in enumerate(facts):
        _closed(fact, {"claim", "conclusion", "evidence_refs"}, f"{label} fact {index}")
        claim = fact["claim"]
        if (not isinstance(claim, str) or claim not in REQUIRED_AUDIT_CLAIMS
                or claim in claims or fact["conclusion"] != "pass"):
            raise ProofError(f"{label} has a missing, duplicate or failed factual claim")
        claims.add(claim)
        references = fact["evidence_refs"]
        if not isinstance(references, list) or not references:
            raise ProofError(f"{label} fact lacks evidence references")
        for ref_index, reference in enumerate(references):
            _closed(reference, {"kind", "value"}, f"{label} fact evidence")
            if reference["kind"] == "native_event":
                locator = _native_locator(reference["value"], f"{label} fact locator")
                if locator["project_id"] != project_id:
                    raise ProofError(f"{label} fact belongs to another project")
                resolver(locator)
            elif reference["kind"] == "immutable_object":
                budget.verify(reference["value"], f"{label} fact evidence {ref_index}")
            else:
                raise ProofError(f"{label} fact evidence kind is unsupported")
    if claims != REQUIRED_AUDIT_CLAIMS:
        raise ProofError(f"{label} lacks required factual conclusions")
    if findings:
        raise ProofError(f"{label} contains unresolved findings")
    return assignment


@dataclass(frozen=True)
class VerifiedProof:
    request: dict
    core: dict
    intent: dict
    qualification: dict
    approval_locator: dict
    hashes: dict
    native_question_version: str
    run_directory: Path
    persistence_root: Path
    request_path: Path
    qualification_path: Path
    stop_barriers: dict



def _validate_background_settlement(
    events, calls, completions, history, subject, persistence_root,
):
    background = {}
    for call_id, call in calls.items():
        completion = completions[call_id]
        presentation = completion["payload"].get("presentation", {})
        if presentation.get("MovedToBackground") is not True:
            continue
        output = completion["payload"].get("output", "")
        match = re.search(r"Process moved to background with ID ([A-Za-z0-9._:-]+)\.", output)
        if match is None:
            raise ProofError("background dispatch lacks its framework process identifier")
        background[match.group(1)] = call_id
    notices = {}
    for event in events:
        payload = event["payload"]
        if (event["kind"] == "message" and payload.get("role") == "developer"
                and payload.get("message_type") == "background_notice"):
            process_id = payload.get("name")
            if process_id not in background or process_id in notices:
                raise ProofError("framework background notice has no unique native dispatch")
            if type(payload.get("background_exit_code")) is not int or payload["background_exit_code"] != 0:
                raise ProofError("framework background process did not settle successfully")
            notices[process_id] = event
    settlements = dict(notices)
    for call_id, call in calls.items():
        if call["name"] != "write_stdin":
            continue
        arguments = call["arguments"]
        process_id = str(arguments.get("session_id"))
        completion = completions[call_id]
        presentation = completion["payload"].get("presentation", {})
        if (process_id not in background or type(arguments.get("session_id")) is not int
                or arguments.get("chars", "") != ""
                or call["event"]["seq"] <= calls[background[process_id]]["event"]["seq"]):
            raise ProofError("native background poll is not a read-only observation of its original dispatch")
        exit_code = presentation.get("ShellExitCode")
        if exit_code is None:
            # A nonterminal empty poll is not process settlement.
            continue
        output = completion["payload"].get("output", "")
        if (type(exit_code) is not int or exit_code != 0
                or presentation.get("IsShell") is not True
                or presentation.get("MovedToBackground") is not False
                or not re.match(r"^Wall time: [0-9.]+ seconds\nOutput file: [^\n]+\n"
                                r"Exit code 0, output:\n", output)):
            raise ProofError("native background poll lacks its genuine terminal framework result")
        if process_id in settlements:
            raise ProofError("native background job has duplicate terminal observations")
        settlements[process_id] = completion
    if set(background) != set(settlements):
        raise ProofError("background dispatch and framework completion sets differ")
    asynchronous = history.get("async_completion")
    if not background:
        if asynchronous is not None:
            raise ProofError("asynchronous completion locator has no background process")
        return
    if asynchronous is None:
        raise ProofError("background process lacks its native asynchronous completion locator")
    locator = _native_locator(asynchronous, "asynchronous completion")
    if locator["session_id"] != subject["session_id"] or locator["project_id"] != subject["project_id"]:
        raise ProofError("asynchronous completion belongs to another native owner")
    matches = [event for event in settlements.values() if event["seq"] == locator["event_sequence"]]
    if len(matches) != 1 or _native_event(locator, persistence_root) != matches[0]:
        raise ProofError("asynchronous completion locator does not identify a framework notice")

def verify_bundle(
    request_path: Path,
    qualification_path: Path,
    *,
    _persistence_root: Path | None = None,
) -> VerifiedProof:
    """Validate immutable C/I/Q objects and native audit/approval locators."""
    request, _request_bytes = read_json(Path(request_path), limit=64 * 1024)
    _closed(request, {
        "schema", "run_directory", "expected_preimage_sha256",
        "qualification_ref", "effect_approval_ref",
    }, "request")
    if request["schema"] != REQUEST_SCHEMA:
        raise ProofError("unsupported reconciliation request")
    run_value = request["run_directory"]
    if not isinstance(run_value, str) or not Path(run_value).is_absolute():
        raise ProofError("run directory must be absolute")
    run = Path(run_value)
    if str(run) != str(run.resolve()):
        raise ProofError("run directory is not canonical")
    request_path = Path(request_path)
    qualification_path = Path(qualification_path)
    if (not request_path.is_absolute() or str(request_path) != str(request_path.resolve())
            or not qualification_path.is_absolute()
            or str(qualification_path) != str(qualification_path.resolve())):
        raise ProofError("proof paths must be canonical absolute paths")
    _hash(request["expected_preimage_sha256"], "expected status preimage")
    qualification_ref = request["qualification_ref"]
    _closed(qualification_ref, {"object_sha256", "byte_size"}, "qualification reference")
    q_hash = _hash(qualification_ref["object_sha256"], "qualification hash")
    q_size = qualification_ref["byte_size"]
    if type(q_size) is not int or q_size < 1 or q_size > MAX_FILE_BYTES:
        raise ProofError("qualification size is invalid")
    qualification, q_raw = read_json(Path(qualification_path))
    if len(q_raw) != q_size or hashlib.sha256(q_raw).hexdigest() != q_hash:
        raise ProofError("qualification reference changed")
    _closed(qualification, {
        "schema", "core_sha256", "intent_sha256", "core_ref", "intent_ref",
        "audit_refs",
    }, "qualification envelope")
    if qualification["schema"] != QUALIFICATION_SCHEMA:
        raise ProofError("unsupported qualification envelope")
    core_hash = _hash(qualification["core_sha256"], "core hash")
    intent_hash = _hash(qualification["intent_sha256"], "intent hash")
    core = _resolve_ref(qualification["core_ref"], "evidence core")
    if hashlib.sha256(canonical_json(core)).hexdigest() != core_hash:
        raise ProofError("evidence core digest does not match")
    budget = ReferenceBudget()
    native_question_version = _validate_core(core, run, budget)
    reviewed_tail = _reviewed_tail_declarations(core, budget)
    intent = _resolve_ref(qualification["intent_ref"], "reconciliation intent")
    if hashlib.sha256(canonical_json(intent)).hexdigest() != intent_hash:
        raise ProofError("reconciliation intent digest does not match")
    _validate_intent(intent, core_hash, request["expected_preimage_sha256"])

    configured_root = (_persistence_root if _persistence_root is not None else Path(
        os.environ.get("KENT_PERSISTENCE_ROOT", Path.home() / ".kent"),
    ))
    root = configured_root.resolve()
    if root != configured_root:
        raise ProofError("native persistence root must be canonical")
    resolver = lambda locator: _native_event(locator, root)
    interval = {
        "project_id": core["subject"]["project_id"],
        "session_id": core["subject"]["session_id"],
        "start_sequence": core["history"]["start"]["event_sequence"],
        "cutoff_sequence": core["history"]["cutoff"]["event_sequence"],
    }
    owner_events, _owner_raw = _native_session(core["history"]["start"], root)
    start_sequence = core["history"]["start"]["event_sequence"]
    cutoff_sequence = core["history"]["cutoff"]["event_sequence"]
    owner_calls, owner_completions = _native_trace(
        owner_events, start_sequence=start_sequence, end_sequence=cutoff_sequence,
    )
    _validate_background_settlement(
        owner_events[start_sequence - 1:cutoff_sequence], owner_calls,
        owner_completions, core["history"], core["subject"], root,
    )
    original_binding, original_binding_raw = read_json(run / "binding.json")
    if hashlib.sha256(original_binding_raw).hexdigest() != core["subject"]["binding_sha256"]:
        raise ProofError("native start owner binding fingerprint changed")
    _validate_native_start(
        owner_events[start_sequence - 1:cutoff_sequence], owner_calls, owner_completions,
        core["history"], core["subject"], original_binding,
    )
    covered_sequences = {item["event_locator"]["event_sequence"] for item in core["coverage"]}
    call_sequences = {call["event"]["seq"] for call in owner_calls.values()}
    if covered_sequences != call_sequences:
        raise ProofError("native attributable invocation set differs from disposition coverage")
    for item in core["coverage"]:
        locator = item["event_locator"]
        actual = _native_event(locator, root)
        if actual != owner_events[locator["event_sequence"] - 1]:
            raise ProofError("coverage locator does not identify the actual invocation")
        call_id = next(call_id for call_id, call in owner_calls.items()
                       if call["event"]["seq"] == locator["event_sequence"])
        call = owner_calls[call_id]
        completion = owner_completions[call_id]
        stage_dispatch = re.search(
            r'(?:^|;\s*)(?:RESOURCE_LEASE_RUN_DIRECTORY=\S+\s+)?\./tools/stage-input\s',
            str(call["presentation"].get("Command", "")),
        )
        if stage_dispatch or item["disposition"] == "stage_input_stop_confirmed":
            if item["disposition"] != "stage_input_stop_confirmed":
                raise ProofError("Stage-input invocation has the wrong proof disposition")
            serial = _stage_input_call(
                call, completion,
                qualify_preparation=lambda script, producer: _qualify_preparation(
                    script, producer, owner_calls, core["source_closure"],
                ),
            )
            if serial != core["subject"]["lease_identity"]["serial"]:
                raise ProofError("Stage-input invocation used a different original serial")

    recovered_outputs = _qualified_shell_outputs(core, root, owner_calls, owner_completions, budget)
    pre_dispatch_ids = _native_facade_inventory(
        owner_calls, owner_completions, core["subject"], core["manifest_inventory"],
        recovered_outputs=recovered_outputs,
    )
    if len(pre_dispatch_ids) > 1:
        raise ProofError("duplicate pre-dispatch rejection evidence")
    for item in core["coverage"]:
        if item["disposition"] == "qualified_pre_dispatch_rejection":
            seq = item["event_locator"]["event_sequence"]
            matching = [call_id for call_id, call in owner_calls.items()
                        if call["event"]["seq"] == seq]
            if len(matching) != 1 or matching[0] not in pre_dispatch_ids:
                raise ProofError("pre-dispatch disposition lacks its exact native rejection")

    audit_refs = qualification["audit_refs"]
    if not isinstance(audit_refs, list) or len(audit_refs) != 2:
        raise ProofError("qualification requires exactly two audit reports")
    reviewer_sessions = set()
    reviewer_roles = set()
    for index, locator in enumerate(audit_refs):
        _native_locator(locator, f"audit reference {index}")
        report_event = resolver(locator)
        report = _parse_native_report(
            report_event, AUDIT_SCHEMA, f"audit report {index}",
        )
        if locator["session_id"] != report.get("reviewer_session_id"):
            raise ProofError("audit report is not from its assigned native reviewer Session")
        assignment_locator = _verify_report(
            report, core_hash, intent_hash, interval, f"audit report {index}",
            resolver=resolver, budget=budget, project_id=core["subject"]["project_id"],
        )
        assignment_event = resolver(assignment_locator)
        role = _verify_assignment(
            assignment_locator, report["reviewer_session_id"],
            core["subject"]["task_id"], root, core["subject"]["project_id"],
            core_hash, intent_hash, f"audit assignment {index}",
        )
        if role in reviewer_roles:
            raise ProofError("audit reviewer roles are not independent")
        reviewer_roles.add(role)
        if (assignment_locator["project_id"] != core["subject"]["project_id"]):
            raise ProofError("audit assignment belongs to another project")
        reviewer = report["reviewer_session_id"]
        if reviewer in reviewer_sessions or reviewer == core["subject"]["session_id"]:
            raise ProofError("audit reviewers are not independent")
        reviewer_sessions.add(reviewer)

    approval = request["effect_approval_ref"]
    _closed(approval, {"native_question_locator", "approved_intent_sha256"},
            "effect approval reference")
    approval_locator = _native_locator(
        approval["native_question_locator"], "effect approval locator",
    )
    if approval["approved_intent_sha256"] != intent_hash:
        raise ProofError("approval does not bind the exact intent")
    history = core["history"]
    if approval_locator["project_id"] != core["subject"]["project_id"]:
        raise ProofError("effect approval belongs to another native project")
    external = approval_locator["session_id"] != core["subject"]["session_id"]
    if not external and approval_locator["event_sequence"] <= history["cutoff"]["event_sequence"]:
        raise ProofError("effect approval is outside the original native Session tail")
    approval_event = _validate_owner_tail(
        core["history"], core["subject"], root, request_path, qualification_path,
        expected_intent_hash=intent_hash, expected_qualification_hash=q_hash,
        native_question_version=native_question_version,
        approval_locator=approval_locator, reviewed_tail=reviewed_tail,
    )
    if external:
        approval_event = _external_effect_approval(
            approval_locator, root, native_question_version, intent_hash, q_hash,
        )
    if approval_event.get("seq") != approval_locator["event_sequence"]:
        raise ProofError("native approval locator differs from the approved Question")
    if resolver(approval_locator) != approval_event:
        raise ProofError("native approval locator does not resolve to the approved Question")
    return VerifiedProof(
        request=request, core=core, intent=intent, qualification=qualification,
        approval_locator=approval_locator,
        hashes={"core": core_hash, "intent": intent_hash, "qualification": q_hash},
        native_question_version=native_question_version,
        run_directory=run, persistence_root=root,
        request_path=Path(request_path).resolve(),
        qualification_path=Path(qualification_path).resolve(),
        stop_barriers=_stop_barrier_snapshot(run),
    )

def _stop_barrier_snapshot(run):
    snapshot = {}
    for name in ("finish.json", "stop-renewal.json"):
        path = run / name
        try:
            before = path.lstat()
        except FileNotFoundError:
            snapshot[name] = None
            continue
        _record, raw = read_json(path)
        after = path.lstat()
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size,
                                 info.st_mtime_ns, info.st_ctime_ns)
        if identity(before) != identity(after):
            raise ProofError("owner stop signal changed while qualifying")
        snapshot[name] = (identity(after), hashlib.sha256(raw).hexdigest())
    return snapshot


def _read_status(path: Path):
    value, raw = read_json(path, limit=MAX_FILE_BYTES)
    return value, raw


def _atomic_write(path: Path, data: bytes):
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _command_json(command, *, timeout=COMMAND_TIMEOUT):
    child = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    streams = {child.stdout: bytearray(), child.stderr: bytearray()}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream in streams:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map() or child.poll() is None:
                if time.monotonic() >= deadline:
                    raise ProofError("read-only native/resource inspection timed out")
                for key, _events in selector.select(0.1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        streams[key.fileobj].extend(chunk)
                        if sum(map(len, streams.values())) > MAX_COMMAND_BYTES:
                            raise ProofError("read-only inspection output exceeds limit")
        if child.wait() != 0:
            raise ProofError("read-only native/resource inspection failed")
        raw = bytes(streams[child.stdout])
        try:
            value = json.loads(raw, object_pairs_hook=_unique_object,
                               parse_constant=_reject_constant)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ProofError("read-only inspection returned invalid JSON") from error
        if not isinstance(value, dict):
            raise ProofError("read-only inspection returned a non-object")
        _check_tree(value)
        return value
    except BaseException:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
        raise
    finally:
        for stream in streams:
            stream.close()


def _native_cli_version(executable):
    try:
        result = subprocess.run([str(executable), "--version"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=COMMAND_TIMEOUT,
                                check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProofError("native Kent version could not be qualified") from error
    if (result.returncode != 0 or result.stderr or len(result.stdout) > 128
            or result.stdout.strip() != EFFECT_KENT_QUESTION_VERSION):
        raise ProofError("installed Kent version differs from the requested 2.13 Question protocol")
    return result.stdout.strip()


def _native_owner_active(subject):
    if (os.environ.get("KENT_SESSION_ID") != subject["session_id"]
            or not isinstance(os.environ.get("KENT_STEP_ID"), str)):
        raise ProofError("current native Session/Step binding is unavailable")
    _uuid(os.environ["KENT_STEP_ID"], "current native Step ID")
    owner_row, _locked, _continuation = _native_session_metadata(
        subject["session_id"], subject["project_id"],
        Path(os.environ.get("KENT_PERSISTENCE_ROOT", Path.home() / ".kent")).resolve(),
    )
    if (owner_row.get("task_id") != subject["task_id"]
            or owner_row.get("project_id") != subject["project_id"]):
        raise ProofError("current native Session is not bound to the original Task/Project")
    executable = shutil.which("kent")
    if not executable:
        raise ProofError("native Kent CLI is unavailable")
    executable_path = Path(executable).resolve()
    _native_cli_version(executable_path)
    detail = _command_json([
        str(executable_path), "task", "show", subject["task_id"],
        "--project", subject["project_id"], "--json",
    ])
    summary, status = detail.get("summary", {}), detail.get("status", {})
    if (not isinstance(summary, dict) or not isinstance(status, dict)
            or summary.get("id") != subject["task_id"]
            or summary.get("project_id") != subject["project_id"]
            or summary.get("done") is not False
            or status.get("kind") != "running"
            or status.get("native_state") not in {"running", "active"}):
        raise ProofError("original native Task is not the active current node")
    offset = 0
    while offset <= 10_000:
        result = _command_json([
            str(executable_path), "task", "sessions", subject["task_id"],
            "--project", subject["project_id"], "--offset", str(offset),
            "--limit", "100", "--json",
        ])
        if result.get("task_id") != subject["task_id"] or not isinstance(
                result.get("items"), list):
            raise ProofError("native Session inventory is invalid")
        matches = [row for row in result["items"] if isinstance(row, dict)
                   and row.get("session_id") == subject["session_id"]]
        if matches:
            if len(matches) != 1 or matches[0].get("status") != "running":
                raise ProofError("original native Session is not running in the current Step")
            break
        following = result.get("next_offset")
        if following is None:
            raise ProofError("original native Session is absent from the Task")
        if type(following) is not int or following <= offset:
            raise ProofError("native Session pagination is invalid")
        offset = following
    else:
        raise ProofError("native Session inventory exceeds its bounded page limit")
    nodes = detail.get("current_nodes")
    active = [node for node in nodes if isinstance(node, dict)
              and node.get("session_id") == subject["session_id"]] if isinstance(nodes, list) else []
    if len(active) != 1 or active[0].get("node_id") not in status.get("node_ids", []):
        raise ProofError("original Session is not the genuine current Task node")
    database = Path(os.environ.get("KENT_PERSISTENCE_ROOT", Path.home() / ".kent")) / "db" / "main.sqlite3"
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row["name"] for row in connection.execute(
            "PRAGMA table_info(session_workflow_node_associations)")}
        if not {"session_id", "node_id"}.issubset(columns):
            raise ProofError("native current-node association schema is unsupported")
        associations = connection.execute(
            "SELECT node_id FROM session_workflow_node_associations WHERE session_id=?",
            (subject["session_id"],),
        ).fetchall()
        if not any(row[0] == active[0]["node_id"] for row in associations):
            raise ProofError("native current-node association is not persisted")
    except sqlite3.Error as error:
        raise ProofError("native current-node association read failed") from error
    finally:
        connection.close()


def _inspect_terminal_lease(subject, run):
    binding_path = run / "binding.json"
    receipt_path = run / "receipt.json"
    binding, binding_bytes = read_json(binding_path)
    if hashlib.sha256(binding_bytes).hexdigest() != subject["binding_sha256"]:
        raise ProofError("original owner binding fingerprint changed")
    _private_file(receipt_path)
    receipt_bytes = _private_bytes(receipt_path)
    if hashlib.sha256(receipt_bytes).hexdigest() != subject["receipt_sha256"]:
        raise ProofError("original receipt fingerprint changed")
    config = binding.get("config")
    if not isinstance(config, dict) or not {
            "executable", "pool"}.issubset(config):
        raise ProofError("original resource client configuration is invalid")
    executable = Path(config["executable"])
    pool = Path(config["pool"])
    if (not executable.is_absolute() or not pool.is_absolute()
            or str(executable) != str(executable.resolve())
            or str(pool) != str(pool.resolve()) or not os.access(executable, os.X_OK)):
        raise ProofError("original resource client configuration changed")
    owner = binding.get("owner")
    native_binding = binding.get("binding")
    if (not isinstance(native_binding, dict)
            or native_binding.get("project") != subject["project_id"]
            or native_binding.get("task") != subject["task_id"]
            or native_binding.get("session") != subject["session_id"]):
        raise ProofError("original native binding does not match the proof subject")
    if not isinstance(owner, str) or not owner:
        raise ProofError("original lease owner is missing")
    result = _command_json([
        str(executable), "--root", str(pool), "inspect", "--owner", owner,
        "--lease", str(receipt_path),
    ])
    identity = subject["lease_identity"]
    if any(result.get(key) != identity[key] for key in ("id", "slot")):
        raise ProofError("terminal observation is for a different historical lease")
    generation = result.get("generation")
    if generation is not None and (type(generation) is not int
                                   or generation != identity["generation"]):
        raise ProofError("terminal observation has a different current generation")
    if (result.get("owner") != owner or result.get("capability") != binding.get("capability")
            or result.get("state") not in {"completed", "cancelled", "expired"}
            or result.get("cleanup_pending") is not False
            or result.get("quarantined") is not False
            or result.get("endpoint") is not None):
        raise ProofError("original lease is not terminal and clean")
    return {
        "state": result["state"], "cleanup_pending": False,
        "quarantined": False, "endpoint": None,
        "id": identity["id"], "generation": generation,
        "slot": identity["slot"], "observed_at": time.monotonic(),
    }


@dataclass(frozen=True)
class ReconciliationResult:
    status: dict
    already_applied: bool
    record_path: Path


def _read_reconciliation_record(path, proof):
    record, raw = read_json(path)
    expected_keys = {
        "schema", "operation_id", "preimage_sha256", "preimage",
        "preimage_bytes_utf8", "core_sha256", "intent_sha256",
        "qualification_sha256", "approval_locator", "terminal_observation",
        "applied_at_monotonic", "status_sha256", "status_bytes_utf8",
    }
    _closed(record, expected_keys, "reconciliation record")
    if (record["schema"] != RECONCILIATION_SCHEMA
            or record["operation_id"] != proof.intent["operation_id"]
            or record["core_sha256"] != proof.hashes["core"]
            or record["intent_sha256"] != proof.hashes["intent"]
            or record["qualification_sha256"] != proof.hashes["qualification"]
            or record["approval_locator"] != proof.approval_locator):
        raise ProofError("existing reconciliation record belongs to another operation")
    _hash(record["preimage_sha256"], "reconciliation preimage hash")
    _hash(record["status_sha256"], "reconciliation status hash")
    if not isinstance(record["preimage_bytes_utf8"], str):
        raise ProofError("reconciliation preimage bytes are missing")
    preimage_bytes = record["preimage_bytes_utf8"].encode("utf-8")
    if hashlib.sha256(preimage_bytes).hexdigest() != record["preimage_sha256"]:
        raise ProofError("reconciliation preimage digest changed")
    try:
        original_status = json.loads(
            preimage_bytes, object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProofError("reconciliation preimage is invalid") from error
    if original_status != record["preimage"]:
        raise ProofError("reconciliation preimage object differs from its bytes")
    terminal = record["terminal_observation"]
    _closed(terminal, {
        "state", "cleanup_pending", "quarantined", "endpoint", "id",
        "generation", "slot", "observed_at",
    }, "terminal observation")
    if (terminal["state"] not in {"completed", "cancelled", "expired"}
            or terminal["cleanup_pending"] is not False
            or terminal["quarantined"] is not False or terminal["endpoint"] is not None
            or terminal["generation"] is not None and type(terminal["generation"]) is not int):
        raise ProofError("reconciliation record has an unsafe terminal observation")
    applied_at = record["applied_at_monotonic"]
    if (type(applied_at) not in (int, float) or applied_at < 0
            or not isinstance(record["status_bytes_utf8"], str)):
        raise ProofError("reconciliation postimage or timestamp is missing")
    postimage = record["status_bytes_utf8"].encode("utf-8")
    if hashlib.sha256(postimage).hexdigest() != record["status_sha256"]:
        raise ProofError("reconciliation postimage digest changed")
    try:
        post_status = json.loads(
            postimage.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProofError("reconciliation postimage is invalid") from error
    if (not isinstance(post_status, dict)
            or post_status.get("updated_at") != applied_at
            or post_status.get("phase") != "finished"
            or post_status.get("command_stop") != "confirmed"
            or post_status.get("reconciliation_operation_id") != record["operation_id"]):
        raise ProofError("reconciliation postimage has an unsupported shape")
    expected_status = dict(record["preimage"])
    expected_status["phase"] = "finished"
    expected_status["command_stop"] = "confirmed"
    expected_status["reconciliation_operation_id"] = record["operation_id"]
    expected_status["updated_at"] = applied_at
    if canonical_json(expected_status) + b"\n" != postimage:
        raise ProofError("reconciliation changed fields outside its approved mutation")
    return record, postimage, post_status


def _exclusive_record(path: Path, data: bytes, directory_fd: int):
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        os.unlink(temporary)
        os.fsync(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_legacy_status_identity(lease, identity):
    if (not isinstance(lease, dict) or any(lease.get(key) != identity[key] for key in ("id", "slot"))
            or "generation" not in lease):
        raise ProofError("legacy status belongs to a different lease")
    generation = lease["generation"]
    if generation is None:
        # The validated native acquisition/source closure retains historical
        # generation. Keep this terminal pool observation null in the preimage.
        if (lease.get("state") not in {"completed", "cancelled", "expired"}
                or lease.get("cleanup_pending") is not False
                or lease.get("quarantined") is not False or lease.get("endpoint") is not None):
            raise ProofError("legacy null generation is not a clean terminal observation")
    elif type(generation) is not int or generation != identity["generation"]:
        raise ProofError("legacy status belongs to a different lease")


def apply_terminal_reconciliation(proof: VerifiedProof) -> ReconciliationResult:
    """Atomically settle one legacy status after fresh native/lease readback."""
    run = proof.run_directory
    run_info = run.lstat()
    if (not stat.S_ISDIR(run_info.st_mode) or stat.S_ISLNK(run_info.st_mode)
            or run_info.st_uid != os.getuid() or run_info.st_mode & 0o077):
        raise ProofError("run directory is not owner-private")
    helper_path = run / "helper.lock"
    helper_info = _private_file(helper_path)
    helper_fd = os.open(helper_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    directory_fd = os.open(run, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                           | getattr(os, "O_NOFOLLOW", 0))
    record_path = run / f"reconciliation-{proof.intent['operation_id']}.json"
    try:
        if (os.fstat(helper_fd).st_ino, os.fstat(helper_fd).st_dev) != (
                helper_info.st_ino, helper_info.st_dev):
            raise ProofError("helper lock changed while opening")
        fcntl.flock(helper_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held_dir = os.fstat(directory_fd)
        current_dir = run.lstat()
        if (held_dir.st_dev, held_dir.st_ino) != (current_dir.st_dev, current_dir.st_ino):
            raise ProofError("run directory changed while locking")

        fresh = verify_bundle(
            proof.request_path, proof.qualification_path,
            _persistence_root=proof.persistence_root,
        )
        if (fresh.request != proof.request or fresh.hashes != proof.hashes
                or fresh.intent["operation_id"] != proof.intent["operation_id"]
                or fresh.native_question_version != proof.native_question_version
                or fresh.stop_barriers != proof.stop_barriers):
            raise ProofError("qualified proof changed before application")
        status_path = run / "status.json"
        status, preimage = _read_status(status_path)
        preimage_hash = hashlib.sha256(preimage).hexdigest()
        expected = proof.request["expected_preimage_sha256"]
        record = None
        stored_postimage = None
        stored_poststatus = None
        if record_path.exists() or record_path.is_symlink():
            record, stored_postimage, stored_poststatus = _read_reconciliation_record(
                record_path, proof,
            )
            if record["preimage_sha256"] != expected:
                raise ProofError("existing reconciliation record has another preimage")
        for entry in run.iterdir():
            if "stop" in entry.name and "uncertain" in entry.name:
                raise ProofError("uncertainty or foreign stop marker remains")
        if preimage_hash != expected:
            if (record is not None and preimage_hash == record["status_sha256"]
                    and status == stored_poststatus
                    and status.get("reconciliation_operation_id")
                    == proof.intent["operation_id"]):
                return ReconciliationResult(status, True, record_path)
            raise ProofError("status preimage changed")
        if record is not None and record["preimage"] != status:
            raise ProofError("existing reconciliation record has a different preimage")
        if status.get("stop_protocol") == 2:
            raise ProofError("new-protocol status cannot use legacy reconciliation")
        if (status.get("phase") != "failed" or status.get("command_stop") != "unconfirmed"
                or status.get("parent_command_stop") not in (None, "unconfirmed")
                or status.get("remote_command_stop") not in (None, "pending", "unconfirmed")):
            raise ProofError("legacy status does not establish the supported unconfirmed stop cause")
        if status.get("run_directory") != str(run):
            raise ProofError("legacy status belongs to another run")
        binding, binding_bytes = read_json(run / "binding.json")
        if hashlib.sha256(binding_bytes).hexdigest() != proof.core["subject"]["binding_sha256"]:
            raise ProofError("original binding fingerprint changed")
        config = binding.get("config")
        if (not isinstance(config, dict)
                or config.get("runs_directory") != str(run.parent)):
            raise ProofError("run is outside its original configured runs directory")
        receipt_path = run / "receipt.json"
        _private_file(receipt_path)
        receipt_bytes = _private_bytes(receipt_path)
        if hashlib.sha256(receipt_bytes).hexdigest() != proof.core["subject"]["receipt_sha256"]:
            raise ProofError("original receipt fingerprint changed")
        lease = status.get("lease")
        identity = proof.core["subject"]["lease_identity"]
        _validate_legacy_status_identity(lease, identity)

        _validate_owner_tail(
            proof.core["history"], proof.core["subject"], proof.persistence_root,
            proof.request_path, proof.qualification_path,
            native_question_version=proof.native_question_version,
            approval_locator=proof.approval_locator,
            reviewed_tail=_reviewed_tail_declarations(proof.core, ReferenceBudget()),
        )
        _native_owner_active(proof.core["subject"])
        terminal = _inspect_terminal_lease(proof.core["subject"], run)
        # Recheck all stop signals after the external pool read. The directory lock
        # serializes cooperative publishers, while the second snapshot also catches
        # non-cooperative or crash-recovery writes during inspection.
        latest_binding, latest_binding_bytes = read_json(run / "binding.json")
        latest_status, latest_status_bytes = _read_status(status_path)
        if (hashlib.sha256(latest_binding_bytes).hexdigest()
                != proof.core["subject"]["binding_sha256"]
                or hashlib.sha256(latest_status_bytes).hexdigest() != expected
                or latest_status != status):
            raise ProofError("owner binding or status changed during terminal inspection")
        for entry in run.iterdir():
            if "stop" in entry.name and "uncertain" in entry.name:
                raise ProofError("uncertainty or foreign stop marker appeared during inspection")
        if _stop_barrier_snapshot(run) != proof.stop_barriers:
            raise ProofError("owner stop signal changed during terminal inspection")
        if (not isinstance(terminal, dict)
                or terminal.get("state") not in {"completed", "cancelled", "expired"}
                or terminal.get("cleanup_pending") is not False
                or terminal.get("quarantined") is not False
                or terminal.get("endpoint") is not None
                or any(terminal.get(key) != identity[key] for key in ("id", "slot"))
                or terminal.get("generation") is not None
                and (type(terminal.get("generation")) is not int
                     or terminal.get("generation") != identity["generation"])):
            raise ProofError("original lease is not terminal and clean")
        if record is not None:
            previous_terminal = record["terminal_observation"]
            for key in ("state", "cleanup_pending", "quarantined", "endpoint", "id",
                        "generation", "slot"):
                if previous_terminal.get(key) != terminal.get(key):
                    raise ProofError("terminal observation changed during reconciliation retry")
            postimage = stored_postimage
            post_status = stored_poststatus
        else:
            post_status = dict(status)
            post_status["phase"] = "finished"
            post_status["command_stop"] = "confirmed"
            post_status["reconciliation_operation_id"] = proof.intent["operation_id"]
            post_status["updated_at"] = time.monotonic()
            applied_at = post_status["updated_at"]
            postimage = canonical_json(post_status) + b"\n"
            record = {
                "schema": RECONCILIATION_SCHEMA,
                "operation_id": proof.intent["operation_id"],
                "preimage_sha256": preimage_hash,
                "preimage": status,
                "preimage_bytes_utf8": preimage.decode("utf-8"),
                "core_sha256": proof.hashes["core"],
                "intent_sha256": proof.hashes["intent"],
                "qualification_sha256": proof.hashes["qualification"],
                "approval_locator": proof.approval_locator,
                "terminal_observation": terminal,
                "applied_at_monotonic": applied_at,
                "status_sha256": hashlib.sha256(postimage).hexdigest(),
                "status_bytes_utf8": postimage.decode("utf-8"),
            }
            record_bytes = canonical_json(record) + b"\n"
            if len(record_bytes) > MAX_FILE_BYTES:
                raise ProofError("reconciliation record exceeds size limit")
            _exclusive_record(record_path, record_bytes, directory_fd)

        latest_status, latest_bytes = _read_status(status_path)
        if hashlib.sha256(latest_bytes).hexdigest() != expected:
            raise ProofError("status changed after preimage verification")
        if latest_status != status:
            raise ProofError("status changed after preimage verification")
        _atomic_write(status_path, postimage)
        os.fsync(directory_fd)
        return ReconciliationResult(post_status, False, record_path)
    except BlockingIOError as error:
        raise ProofError("run is busy; reconciliation did not acquire its locks") from error
    finally:
        os.close(directory_fd)
        os.close(helper_fd)
