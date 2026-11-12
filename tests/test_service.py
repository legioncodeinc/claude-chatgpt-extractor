import json
import tempfile
import unittest
from pathlib import Path

from aik_sessions.index import SessionIndex
from aik_sessions.models import Message, Session
from aik_sessions.service import SessionService, Settings, read_page


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.settings = Settings(data_dir=self.root / "state", home=self.home)

    def tearDown(self):
        self.directory.cleanup()

    def test_discovery_index_incremental_and_secret_masking(self):
        project = self.home / ".claude/projects/fixture"
        project.mkdir(parents=True)
        fixture = project / "chat.jsonl"
        records = [
            {"type": "user", "sessionId": "fixture", "uuid": "u1",
             "timestamp": "2026-11-11T12:00:00Z", "message": {
                 "role": "user", "content": "Find the harbor webhook password=synthetic-secret"}},
            {"type": "assistant", "sessionId": "fixture", "uuid": "a1",
             "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "The harbor request was received."}]}},
        ]
        fixture.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        service = SessionService(self.settings)
        first = service.index_sources()
        self.assertEqual(first["stats"]["sessions"], 1)
        second = service.index_sources()
        self.assertGreaterEqual(second["skipped_files"], 1)
        index = SessionIndex(self.settings.database)
        try:
            self.assertTrue(index.search("harbor")["results"])
            self.assertFalse(index.search("synthetic-secret")["results"])
        finally:
            index.close()

    def test_oversized_message_continuation_is_lossless(self):
        index = SessionIndex(self.settings.database)
        text = "tāiko " * 4000
        try:
            index.upsert(Session(id="codex:test", source="codex", title="Test", path="/fixture",
                                 messages=[Message(id="m1", role="user", text=text),
                                           Message(id="m2", role="assistant", text="finished")]))
            first = read_page(index, "codex:test", max_chars=10000)
            self.assertEqual(first["next_offset"], 0)
            self.assertEqual(first["next_text_offset"], 10000)
            self.assertFalse(first["done"])
            reconstructed = first["messages"][0]["text"]
            page = first
            while not page["done"]:
                page = read_page(index, "codex:test", offset=page["next_offset"],
                                 text_offset=page["next_text_offset"], max_chars=10000)
                for message in page["messages"]:
                    if message["id"] == "m1":
                        reconstructed += message["text"]
            self.assertEqual(reconstructed, text)
        finally:
            index.close()

    def test_warning_sources_are_rechecked_instead_of_silently_cached(self):
        project = self.home / ".claude/projects/fixture"
        project.mkdir(parents=True)
        (project / "partial.jsonl").write_text(json.dumps({
            "type": "user", "sessionId": "partial", "uuid": "1",
            "message": {"role": "user", "content": "Visible fixture."},
        }) + "\n{broken\n")
        service = SessionService(self.settings)
        first = service.index_sources()
        self.assertGreater(first["warning_count"], 0)
        second = service.index_sources()
        self.assertEqual(second["indexed_files"], 1)
        self.assertGreater(second["warning_count"], 0)


if __name__ == "__main__":
    unittest.main()
