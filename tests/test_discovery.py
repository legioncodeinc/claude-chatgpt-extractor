from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aik_sessions.discovery import discover


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()

    def touch(self, relative: str) -> Path:
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
        return path

    def test_known_roots_nested_agents_archives_and_compression(self) -> None:
        self.touch(".claude/projects/encoded/main.jsonl")
        self.touch(".claude/projects/encoded/main/subagents/agent-one.jsonl")
        self.touch(".claude/projects/encoded/main/subagents/nested/subagents/agent-two.jsonl")
        self.touch(".claude/projects/encoded/sessions-index.json")
        self.touch(".codex/sessions/2026/11/11/rollout-one.jsonl")
        self.touch(".codex/sessions/2026/11/11/rollout-two.jsonl.zst")
        archived = self.touch(".codex/archived_sessions/rollout-three.jsonl.zst")
        self.touch("Downloads/conversations.json")
        result = discover(home=self.home, import_paths=[])
        self.assertEqual(len(result.files), 6)
        self.assertEqual(sum(file.source == "claude-code" for file in result.files), 3)
        self.assertEqual(sum(file.source == "codex" for file in result.files), 3)
        self.assertEqual([file.path for file in result.files if file.archived], [str(archived)])
        self.assertFalse(result.warnings)

    def test_explicit_imports_are_deduplicated_and_do_not_follow_symlinks(self) -> None:
        exported = self.touch("imports/export/conversations.json")
        self.touch("imports/export/archive.zip")
        self.touch("imports/export/photo.png")
        outside = self.touch("unrequested/secret.json")
        (self.home / "imports/export/link.json").symlink_to(outside)
        (self.home / "imports/export/linked-dir").symlink_to(outside.parent)
        result = discover(home=self.home, import_paths=[exported.parent, exported])
        self.assertEqual(len(result.files), 2)
        self.assertTrue(all(file.source is None for file in result.files))
        self.assertNotIn(str(outside), [file.path for file in result.files])

    def test_custom_roots_are_configuration_roots(self) -> None:
        expected = self.touch("custom-claude/projects/test/session.jsonl")
        self.touch("custom-codex/sessions/2026/11/11/session.jsonl")
        self.touch(".claude/projects/ignored/session.jsonl")
        result = discover(
            home=self.home,
            import_paths=[],
            claude_root=self.home / "custom-claude",
            codex_root=self.home / "custom-codex",
        )
        self.assertEqual(len(result.files), 2)
        self.assertIn(str(expected), [file.path for file in result.files])

    def test_missing_and_unsupported_explicit_imports_warn(self) -> None:
        unsupported = self.touch("imports/data.txt")
        result = discover(home=self.home, import_paths=[self.home / "missing", unsupported])
        self.assertFalse(result.files)
        self.assertEqual(len(result.warnings), 2)


if __name__ == "__main__":
    unittest.main()
