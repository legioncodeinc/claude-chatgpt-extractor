"""Command-line behaviour a user sees directly, on synthetic data only."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        (root / "home").mkdir()
        self.base = [sys.executable, "-m", "aik_sessions", "--home", str(root / "home"),
                     "--data-dir", str(root / "state")]

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, *arguments):
        return subprocess.run([*self.base, *arguments], capture_output=True, text=True,
                              timeout=60, env={"PYTHONPATH": SRC, "PATH": os.defpath})

    def test_unknown_session_is_one_clean_line_and_exit_code_one(self):
        done = self.run_cli("read", "codex:nope")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stderr, "aik-sessions: Session not found: codex:nope\n")
        self.assertEqual(done.stdout, "")

    def test_invalid_date_is_reported_without_a_traceback(self):
        done = self.run_cli("search", "harbor", "--start-date", "last week")
        self.assertEqual(done.returncode, 1)
        self.assertIn("YYYY-MM-DD", done.stderr)
        self.assertNotIn("Traceback", done.stderr)


if __name__ == "__main__":
    unittest.main()
