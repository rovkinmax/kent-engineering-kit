from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from workflowkit.operations import (
    PlanValidationError,
    canonical_bytes,
    load_plan,
    verify_release_portfolio,
)


def plan_file(root: Path, value: dict) -> tuple[Path, str]:
    path = root / "plan.json"
    raw = canonical_bytes(value)
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


class ReleasePortfolioTest(unittest.TestCase):
    def test_raw_plan_must_be_canonical_and_duplicate_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "plan.json"
            path.write_bytes(b'{ "schema": "release-portfolio-plan-v1" }')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaises(PlanValidationError):
                load_plan(
                    path,
                    schema="release-portfolio-plan-v1",
                    expected_sha256=digest,
                )
            path.write_bytes(
                b'{"schema":"release-portfolio-plan-v1","schema":"release-portfolio-plan-v1"}'
            )
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaises(PlanValidationError):
                load_plan(
                    path,
                    schema="release-portfolio-plan-v1",
                    expected_sha256=digest,
                )

    def test_portfolio_rejects_a_missing_origin_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = {
                "schema": "release-portfolio-plan-v1",
                "kit": {
                    "root": str(root),
                    "repository": "kit/example",
                    "commit": "a" * 40,
                },
                "projects": [
                    {
                        "root": str(root),
                        "repository": f"owner/project-{index}",
                        "commit": "b" * 40,
                    }
                    for index in range(4)
                ],
            }
            path, digest = plan_file(root, value)
            plan = load_plan(
                path,
                schema="release-portfolio-plan-v1",
                expected_sha256=digest,
            )
            with self.assertRaises(Exception):
                verify_release_portfolio(plan)


if __name__ == "__main__":
    unittest.main()
