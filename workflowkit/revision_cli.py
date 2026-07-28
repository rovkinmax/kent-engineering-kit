from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .revision import RevisionPreflightError, preflight_project_revision


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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = preflight_project_revision(
        args.project,
        args.ref,
    )
    print(json.dumps(result.as_json(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RevisionPreflightError as error:
        print(f"preflight-revision: {error}", file=sys.stderr)
        raise SystemExit(1)
