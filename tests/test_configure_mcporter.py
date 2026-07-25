from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "configure-mcporter"


class ConfigureMcporterTest(unittest.TestCase):
    def run_script(self, config: Path, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(SCRIPT), "--config", str(config), *arguments],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_apply_adds_mobile_and_preserves_existing_servers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "mcporter.json"
            config.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "existing": {
                                "type": "http",
                                "url": "https://example.invalid/mcp",
                            }
                        },
                        "imports": ["codex"],
                    }
                )
            )

            result = self.run_script(config, "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(config.read_text())
            self.assertIn("existing", value["mcpServers"])
            self.assertEqual(value["imports"], ["codex"])
            self.assertEqual(
                value["mcpServers"]["mobile"]["args"],
                ["-y", "claude-in-mobile@latest"],
            )
            self.assertNotIn("lifecycle", value["mcpServers"]["mobile"])

    def test_check_reports_missing_entry_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "mcporter.json"

            result = self.run_script(config)

            self.assertEqual(result.returncode, 1)
            self.assertFalse(config.exists())
            self.assertEqual(json.loads(result.stdout)["status"], "changes_required")

    def test_conflicting_mobile_entry_is_not_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "mcporter.json"
            original = {
                "mcpServers": {
                    "mobile": {
                        "command": "custom-mobile",
                    }
                }
            }
            config.write_text(json.dumps(original))

            result = self.run_script(config, "--apply")

            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(config.read_text()), original)
            self.assertEqual(json.loads(result.stdout)["status"], "conflict")

    def test_repeated_apply_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "mcporter.json"

            first = self.run_script(config, "--apply")
            second = self.run_script(config, "--apply")

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(second.stdout)["added"], [])
            self.assertEqual(json.loads(second.stdout)["unchanged"], ["mobile"])


if __name__ == "__main__":
    unittest.main()
