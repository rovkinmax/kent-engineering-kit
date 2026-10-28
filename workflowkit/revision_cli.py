from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .revision import (
    RevisionPreflightError, preflight_project_revision, source_read_budget,
    verify_runtime_source_captures,
)
from .runtime import (
    RuntimeContractError,
    capture_runtime_source_envelope,
    capture_runtime_source_envelope_verified,
    canonical_bytes,
    SourceResourceBudgetExceeded,
    parse_runtime_external_captures,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that a Git revision contains the project profile, "
            "procedures, scripts, and required adapters used by generated "
            "Kent workflows."
        )
    )
    parser.add_argument(
        "--project",
        required=True,
        type=Path,
        help="Git repository root containing the Kent project adapter.",
    )
    parser.add_argument(
        "--ref",
        required=True,
        help="Local Git revision or exact commit selected for task execution.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--capture-runtime-envelope",
        action="store_true",
        help="Capture ordered runtime external roots from exact stdin.",
    )
    mode.add_argument("--capture-runtime-envelope-from-git", action="store_true")
    parser.add_argument("--envelope-version", type=int, choices=(1, 2))
    return parser.parse_args()


def read_runtime_capture_stdin() -> bytes:
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    raw = stream.read(6 * 1024 * 1024 + 1)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, bytes):
        raise RuntimeContractError("runtime capture stdin did not return bytes")
    return raw


def main() -> int:
    args = parse_args()
    if args.capture_runtime_envelope_from_git:
        if args.envelope_version is None:
            raise RuntimeContractError("Git capture requires explicit --envelope-version")
        if len(args.ref) != 40 or any(c not in "0123456789abcdef" for c in args.ref):
            raise RuntimeContractError("Git capture requires exact commit")
        with source_read_budget() as budget:
            result = preflight_project_revision(args.project, args.ref, budget=budget)
            inputs = result.selected_runtime_source_inputs
            if inputs is None:
                raise RuntimeContractError("runtime capture requires schema-4 selected inputs")
            observations = verify_runtime_source_captures(args.project, inputs, budget=budget)
            output = capture_runtime_source_envelope_verified(
                inputs, observations, envelope_version=args.envelope_version,
            )
            budget.metadata(output)
            raw_output = canonical_bytes(output) + b"\n"
            if len(raw_output) > budget.policy.metadata_bytes:
                raise SourceResourceBudgetExceeded("source capture output capacity exceeded")
            budget.check()
        sys.stdout.write(raw_output.decode("utf-8"))
        return 0
    if args.envelope_version is not None:
        raise RuntimeContractError("--envelope-version requires Git capture mode")
    result = preflight_project_revision(
        args.project,
        args.ref,
    )
    if args.capture_runtime_envelope:
        if result.selected_runtime_source_inputs is None:
            raise RuntimeContractError(
                "runtime capture requires a schema-4 selected source bundle"
            )
        captures = parse_runtime_external_captures(read_runtime_capture_stdin())
        output = capture_runtime_source_envelope(
            result.selected_runtime_source_inputs,
            captures,
        )
    else:
        output = result.as_json()
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RevisionPreflightError, RuntimeContractError) as error:
        print(f"preflight-revision: {error}", file=sys.stderr)
        raise SystemExit(1)
