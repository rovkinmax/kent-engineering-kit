"""Documentation guards, not native compaction execution tests."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ContextRefreshDocumentationTest(unittest.TestCase):
    def test_both_entrypoints_describe_retained_and_refreshed_locks(self):
        for relative in ("global/AGENTS.md", "docs/OPERATIONS.md"):
            with self.subTest(path=relative):
                text = " ".join((ROOT / relative).read_text().split())
                self.assertIn("Ordinary continuation and resume", text)
                self.assertIn("Compaction or a workflow context reset can clear", text)
                self.assertIn("next dispatch may load changed instructions", text)
                self.assertIn("Session ID", text)
                self.assertIn("retained helper", text)
                self.assertIn("carried values", text)
                self.assertIn("Kent restart", text)
                self.assertNotIn("Existing sessions retain their locked", text)
                self.assertNotIn("Existing and resumed sessions retain their locked", text)


if __name__ == "__main__":
    unittest.main()
