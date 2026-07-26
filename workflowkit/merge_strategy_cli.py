from __future__ import annotations

import argparse
import json
import sys

from .merge_strategy import blocked, resolve_github_merge_strategy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve one GitHub pull-request merge strategy from repository, "
            "target-branch, ruleset, and merge-queue evidence."
        )
    )
    parser.add_argument(
        "--policy",
        required=True,
        choices=("auto", "merge", "squash", "rebase"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as error:
        result = blocked("invalid_json", f"Cannot parse GitHub evidence: {error}")
    else:
        if not isinstance(payload, dict):
            result = blocked(
                "invalid_github_payload",
                "GitHub evidence must be a JSON object.",
            )
        else:
            result = resolve_github_merge_strategy(args.policy, payload)
    print(json.dumps(result.as_json(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
