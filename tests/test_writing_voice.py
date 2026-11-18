from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = "${KENT_PERSISTENCE_ROOT:-$HOME/.kent}/private/writing-voice/profile.md"


class WritingVoiceTests(unittest.TestCase):
    """Documentation contracts and installer wiring, not model behavior tests."""

    def test_skill_contract(self) -> None:
        skill = (REPO_ROOT / "skills/writing-voice/SKILL.md").read_text()
        _, frontmatter, body = skill.split("---", 2)
        self.assertIn("name: writing-voice", frontmatter)
        for trigger in (
            "Proactively use", "user's behalf", "colleague replies",
            "PR or task comments", "email", "announcements",
            "even without a request for their style",
            "when profile access is declined or unavailable",
        ):
            with self.subTest(trigger=trigger):
                self.assertIn(trigger, frontmatter)
        for exclusion in (
            "agent's own replies", "code", "technical artifacts", "summaries",
        ):
            with self.subTest(exclusion=exclusion):
                self.assertIn(exclusion, frontmatter)
        self.assertEqual(body.count(PROFILE_PATH), 1)
        for boundary in (
            "never as executable shell text",
            "Read only that file",
            "do not search another user's home",
            "Do not create, update, copy, or install a profile",
            "style data, not authority",
            "If profile processing consent is declined",
            "the file is absent, empty or\nunreadable",
            "the read tool is unavailable",
            "neutral draft",
            "outside the copyable",
            "do not bypass a denial",
            "Do not start research",
            "model context and potentially Session history",
            "obtain\nconsent before reading",
            "if declined, use the neutral fallback and its notice",
            "delegated prompts",
            "It overrides profile preferences",
            "Do not invent supporting facts",
            "does not authorize posting, sending",
        ):
            with self.subTest(boundary=boundary):
                self.assertIn(boundary, body)

    def test_readme_describes_external_profile(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text()
        self.assertIn("(skills/writing-voice/SKILL.md)", readme)
        self.assertIn(PROFILE_PATH, readme)
        self.assertIn("Keep personal profiles outside\nGit", readme)
        self.assertIn("Drafting does not authorize sending.", readme)

    def test_disposable_install_binds_skill_without_creating_profile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="writing voice installer fixture ") as temporary:
            root = Path(temporary)
            kit = root / "kit checkout with spaces"
            for relative in (
                "scripts/install",
                "scripts/kent-worktree",
                "scripts/preflight-revision",
                "scripts/prepare-github-ci",
                "scripts/resolve-github-merge-strategy",
                "global/AGENTS.md",
                "adapters/gradle/shell-postprocess-hook",
                "adapters/mcp/kent-mcp-call",
                "adapters/mcp/kent-mcp-list",
                ".kent/scripts/workflow-project-memory",
                ".kent/scripts/workflow_project_memory.py",
            ):
                source = REPO_ROOT / relative
                destination = kit / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            for directory in ("skills", "prompts", "agents"):
                shutil.copytree(REPO_ROOT / directory, kit / directory)
            for relative in ("scripts/install", ".kent/scripts/workflow-project-memory"):
                executable = kit / relative
                executable.chmod(executable.stat().st_mode | 0o111)
            cwd = root / "unrelated consumer cwd"
            cwd.mkdir()
            persistence = root / "custom persistence root"
            environment = dict(os.environ, KENT_PERSISTENCE_ROOT=str(persistence))
            installed = subprocess.run(
                [str(kit / "scripts/install")],
                cwd=cwd,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            binding = persistence / "skills/writing-voice"
            self.assertTrue(binding.is_symlink())
            self.assertEqual(
                binding.resolve(strict=True),
                (kit / "skills/writing-voice").resolve(strict=True),
            )
            self.assertEqual(
                (binding / "SKILL.md").read_bytes(),
                (REPO_ROOT / "skills/writing-voice/SKILL.md").read_bytes(),
            )
            self.assertFalse((persistence / "private").exists())


if __name__ == "__main__":
    unittest.main()
