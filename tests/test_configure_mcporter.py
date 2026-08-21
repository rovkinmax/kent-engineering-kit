from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "configure-mcporter"


class ConfigureMcporterTest(unittest.TestCase):
    def run_script(
        self,
        config: Path,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            fake_mcporter = Path(directory) / "mcporter"
            fake_mcporter.write_text(
                textwrap.dedent(
                    f"""\
                    exec {shlex.quote(str(Path(sys.executable).resolve()))} \
                      - "$@" <<'PYTHON'
                    import json
                    import os
                    from pathlib import Path
                    import sys

                    arguments = sys.argv[1:]
                    if len(arguments) < 3 or arguments[0] != "--config":
                        raise SystemExit(2)
                    config = Path(arguments[1])
                    command = arguments[2:]

                    if command == ["config", "get", "mobile", "--json"]:
                        if not config.is_file():
                            raise SystemExit(1)
                        value = json.loads(config.read_text())
                        entry = value.get("mcpServers", {{}}).get("mobile")
                        if not isinstance(entry, dict):
                            raise SystemExit(1)
                        result = dict(entry)
                        result["source"] = {{
                            "path": os.path.realpath(config),
                        }}
                        print(json.dumps(result))
                        raise SystemExit(0)

                    if command == [
                        "config",
                        "add",
                        "mobile",
                        "--command",
                        "npx",
                        "--",
                        "-y",
                        "claude-in-mobile@latest",
                    ]:
                        value = (
                            json.loads(config.read_text())
                            if config.is_file()
                            else {{}}
                        )
                        value.setdefault("mcpServers", {{}})["mobile"] = {{
                            "command": "npx",
                            "args": ["-y", "claude-in-mobile@latest"],
                        }}
                        config.write_text(json.dumps(value))
                        raise SystemExit(0)

                    raise SystemExit(2)
                    PYTHON
                    """
                )
            )
            fake_mcporter.chmod(0o700)
            environment = os.environ.copy()
            inherited_path = environment.get("PATH", "")
            environment["PATH"] = str(fake_mcporter.parent) + (
                os.pathsep + inherited_path if inherited_path else ""
            )
            return subprocess.run(
                [str(SCRIPT), "--config", str(config), *arguments],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
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
