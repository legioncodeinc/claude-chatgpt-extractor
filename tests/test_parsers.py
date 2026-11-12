from __future__ import annotations

from compression import zstd
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from aik_sessions.models import DiscoveredFile
from aik_sessions.parsers import parse_file


NATIVE_ID = "11111111-1111-1111-1111-111111111111"
PARENT_ID = "22222222-2222-2222-2222-222222222222"


class ParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_json(self, name: str, data: object) -> DiscoveredFile:
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return DiscoveredFile(path=str(path))

    def write_jsonl(self, name: str, records: list[dict[str, object]], *, source: str | None = None) -> DiscoveredFile:
        path = self.root / name
        encoded = "".join(json.dumps(record) + "\n" for record in records).encode()
        if name.endswith(".zst"):
            path.write_bytes(zstd.compress(encoded))
        else:
            path.write_bytes(encoded)
        return DiscoveredFile(path=str(path), source=source)

    def test_claude_code_visible_text_only_deduplicated(self) -> None:
        human = {"type": "user", "uuid": "u1", "sessionId": "session", "cwd": "/example", "timestamp": "2026-01-01T00:00:00Z", "message": {"role": "user", "content": "Find the test function"}}
        file = self.write_jsonl("session.jsonl", [
            human,
            human,
            {"type": "api-request-blob", "message": {"role": "user", "content": "duplicate request blob"}},
            {"type": "assistant", "uuid": "a1", "sessionId": "session", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "private reasoning"},
                {"type": "text", "text": "Found the function."},
                {"type": "tool_use", "id": "call1", "input": {"token": "hidden"}},
            ]}},
            {"type": "user", "uuid": "tool1", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call1", "content": "raw tool data"}]}},
            {"type": "attachment", "message": {"role": "user", "content": "attachment"}},
        ], source="claude-code")
        result = parse_file(file)
        self.assertEqual(len(result.sessions), 1)
        session = result.sessions[0]
        self.assertEqual(session.id, "claude-code:session")
        self.assertEqual(session.project, "/example")
        self.assertEqual([message.text for message in session.messages], ["Find the test function", "Found the function."])
        self.assertEqual(session.started_at, "2026-01-01T00:00:00+00:00")
        self.assertFalse(result.warnings)

    def test_claude_child_identity_is_separate_from_parent(self) -> None:
        file = self.write_jsonl("agent-child.jsonl", [{
            "type": "user", "uuid": "u1", "sessionId": "parent", "agentId": "child", "message": {"role": "user", "content": "Assigned task"},
        }], source="claude-code")
        session = parse_file(file).sessions[0]
        self.assertEqual(session.id, "claude-code:parent/agent/child")
        self.assertEqual(session.metadata["parent_session_id"], "parent")

    def test_incomplete_final_and_invalid_interior_records_warn_without_content(self) -> None:
        file = self.write_jsonl("session.jsonl", [{"type": "user", "sessionId": "s", "message": {"role": "user", "content": "complete"}}], source="claude-code")
        with Path(file.path).open("ab") as stream:
            stream.write(b'not-json SECRET\n{"unfinished": "SECRET')
        result = parse_file(file)
        self.assertEqual([message.text for message in result.sessions[0].messages], ["complete"])
        self.assertEqual(len(result.warnings), 2)
        self.assertNotIn("SECRET", " ".join(result.warnings))
        self.assertTrue(any("Incomplete final" in warning for warning in result.warnings))

    def test_codex_compression_own_metadata_and_inherited_boundary(self) -> None:
        file = self.write_jsonl(f"rollout-2026-01-01-{NATIVE_ID}.jsonl.zst", [
            {"type": "session_meta", "ordinal": 0, "payload": {"id": NATIVE_ID, "session_id": PARENT_ID, "parent_thread_id": PARENT_ID, "forked_from_id": PARENT_ID, "cwd": "/child", "subagent_history_start_ordinal": 100}},
            {"type": "session_meta", "ordinal": 1, "payload": {"id": PARENT_ID, "cwd": "/parent"}},
            {"type": "response_item", "ordinal": 10, "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "inherited parent prompt"}]}},
            {"type": "response_item", "ordinal": 100, "timestamp": "2026-01-01T10:00:00Z", "payload": {"type": "message", "id": "m1", "role": "user", "content": [{"type": "input_text", "text": "child prompt"}]}},
            {"type": "event_msg", "ordinal": 101, "payload": {"type": "item_completed", "item": {"text": "duplicate event"}}},
            {"type": "response_item", "ordinal": 102, "payload": {"type": "reasoning", "summary": [{"text": "reasoning"}]}},
            {"type": "response_item", "ordinal": 103, "payload": {"type": "message", "id": "m2", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "child answer"}]}},
            {"type": "response_item", "ordinal": 104, "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "instructions"}]}},
        ], source="codex")
        session = parse_file(file).sessions[0]
        self.assertEqual(session.id, f"codex:{NATIVE_ID}")
        self.assertEqual(session.project, "/child")
        self.assertEqual(session.metadata["parent_thread_id"], PARENT_ID)
        self.assertEqual([message.text for message in session.messages], ["child prompt", "child answer"])

    def test_codex_legacy_events_only_used_without_canonical_messages(self) -> None:
        legacy = {"type": "event_msg", "payload": {"type": "user_message", "message": "prompt", "id": "m1"}}
        file = self.write_jsonl("legacy.jsonl", [legacy], source="codex")
        self.assertEqual(parse_file(file).sessions[0].messages[0].text, "prompt")
        canonical = {"type": "response_item", "payload": {"type": "message", "role": "user", "id": "m1", "content": [{"type": "input_text", "text": "prompt"}]}}
        file = self.write_jsonl("legacy.jsonl", [legacy, canonical], source="codex")
        self.assertEqual(len(parse_file(file).sessions[0].messages), 1)

    def chatgpt(self) -> dict[str, object]:
        def node(identifier: str, parent: str | None, role: str, text: str, *, metadata: dict[str, bool] | None = None) -> dict[str, object]:
            return {"id": identifier, "parent": parent, "message": {"id": identifier, "author": {"role": role}, "content": {"content_type": "text", "parts": [text]}, "create_time": 1_700_000_000, "metadata": metadata or {}}}
        return {"id": "chat", "title": "Example chat", "current_node": "a2", "create_time": 1_700_000_000, "mapping": {
            "root": {"id": "root", "parent": None, "message": None},
            "u1": node("u1", "root", "user", "question"),
            "a1": node("a1", "u1", "assistant", "inactive answer"),
            "a2": node("a2", "u1", "assistant", "active answer"),
        }}

    def test_chatgpt_selects_only_current_branch(self) -> None:
        file = self.write_json("conversations.json", [self.chatgpt()])
        result = parse_file(file)
        session = result.sessions[0]
        self.assertEqual(session.id, "chatgpt-export:chat")
        self.assertEqual([message.text for message in session.messages], ["question", "active answer"])
        self.assertFalse(result.warnings)

    def test_chatgpt_broken_or_cyclic_branches_fail_closed(self) -> None:
        data = self.chatgpt()
        data["mapping"]["u1"]["parent"] = "a2"
        result = parse_file(self.write_json("conversations.json", [data]))
        self.assertFalse(result.sessions)
        self.assertTrue(any("Cycle" in warning for warning in result.warnings))

    def test_chatgpt_hidden_messages_and_analysis_are_omitted(self) -> None:
        data = self.chatgpt()
        data["mapping"]["u1"]["message"]["metadata"] = {"is_visually_hidden_from_conversation": True}
        data["mapping"]["a2"]["message"]["channel"] = "analysis"
        result = parse_file(self.write_json("conversations.json", [data]))
        self.assertFalse(result.sessions)

    def test_claude_export_supports_text_and_content_blocks(self) -> None:
        file = self.write_json("claude.json", [{"uuid": "conversation", "name": "Example", "created_at": "2026-01-01T12:00:00Z", "chat_messages": [
            {"uuid": "h", "sender": "human", "text": "question"},
            {"uuid": "a", "sender": "assistant", "content": [{"type": "thinking", "thinking": "private"}, {"type": "text", "text": "answer"}]},
        ]}])
        session = parse_file(file).sessions[0]
        self.assertEqual(session.id, "claude-export:conversation")
        self.assertEqual([message.role for message in session.messages], ["user", "assistant"])
        self.assertEqual([message.text for message in session.messages], ["question", "answer"])

    def test_fallback_ids_are_stable_across_export_filenames(self) -> None:
        content = [{"chat_messages": [{"sender": "human", "text": "a stable prompt"}]}]
        first = parse_file(self.write_json("one.json", content)).sessions[0]
        second = parse_file(self.write_json("two.json", content)).sessions[0]
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.messages[0].id, second.messages[0].id)

    def test_zip_supported_members_only_no_extraction(self) -> None:
        archive_path = self.root / "export.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("export/conversations.json", json.dumps([self.chatgpt()]))
            archive.writestr("export/photo.png", b"attachment bytes")
            archive.writestr("../unsafe.json", json.dumps([self.chatgpt()]))
        result = parse_file(DiscoveredFile(path=str(archive_path)))
        self.assertEqual(len(result.sessions), 1)
        self.assertIn("!/export/conversations.json", result.sessions[0].path)
        self.assertEqual(len(result.warnings), 2)
        self.assertEqual(list(self.root.iterdir()), [archive_path])

    def test_zip_member_and_total_bounds(self) -> None:
        archive_path = self.root / "export.zip"
        data = json.dumps([self.chatgpt()])
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("one.json", data)
            archive.writestr("two.json", data)
        with patch("aik_sessions.parsers.MAX_EXPORT_BYTES", 1):
            result = parse_file(DiscoveredFile(path=str(archive_path)))
        self.assertFalse(result.sessions)
        self.assertTrue(any("member size limit" in warning for warning in result.warnings))
        with patch("aik_sessions.parsers.MAX_ZIP_TOTAL_BYTES", len(data) + 1):
            result = parse_file(DiscoveredFile(path=str(archive_path)))
        self.assertEqual(len(result.sessions), 1)
        self.assertTrue(any("expanded size limit" in warning for warning in result.warnings))

    def test_zip_budget_counts_nested_zstd_expansion(self) -> None:
        archive_path = self.root / "nested.zip"
        row = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x" * 1000}]}}
        expanded = (json.dumps(row) + "\n").encode()
        compressed = zstd.compress(expanded)
        self.assertLess(len(compressed), len(expanded) // 2)
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("rollout.jsonl.zst", compressed)
        with patch("aik_sessions.parsers.MAX_ZIP_TOTAL_BYTES", len(expanded) // 2):
            result = parse_file(DiscoveredFile(path=str(archive_path)))
        self.assertFalse(result.sessions)
        self.assertTrue(any("ZIP expanded size limit" in warning for warning in result.warnings))

    def test_bad_archive_member_names_cannot_crash_parser(self) -> None:
        archive_path = self.root / "names.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(".", "not a conversation")
            archive.writestr("C:\\conversations.json", json.dumps([self.chatgpt()]))
            archive.writestr("safe/conversations.json", json.dumps([self.chatgpt()]))
        result = parse_file(DiscoveredFile(path=str(archive_path)))
        self.assertEqual(len(result.sessions), 1)
        self.assertEqual(len(result.warnings), 2)

    def test_missing_claude_agent_id_uses_agent_filename(self) -> None:
        file = self.write_jsonl("agent-child.jsonl", [{
            "type": "user", "sessionId": "parent", "message": {"role": "user", "content": "task"},
        }], source="claude-code")
        self.assertEqual(parse_file(file).sessions[0].id, "claude-code:parent/agent/child")

    def test_repeated_text_without_ids_is_not_lost(self) -> None:
        rows = [
            {"sender": "human", "text": "again"},
            {"sender": "assistant", "text": "answer"},
            {"sender": "human", "text": "again"},
        ]
        result = parse_file(self.write_json("claude.json", [{"uuid": "c", "chat_messages": rows}]))
        self.assertEqual(len(result.sessions[0].messages), 3)
        self.assertEqual(len({message.id for message in result.sessions[0].messages}), 3)

    def test_invalid_json_and_corrupt_zstd_warn_without_echoing_input(self) -> None:
        bad = self.root / "bad.json"
        bad.write_text('SECRET invalid', encoding="utf-8")
        result = parse_file(DiscoveredFile(path=str(bad)))
        self.assertFalse(result.sessions)
        self.assertNotIn("SECRET", " ".join(result.warnings))
        compressed = self.root / "bad.jsonl.zst"
        compressed.write_bytes(b"not zstandard")
        result = parse_file(DiscoveredFile(path=str(compressed), source="codex"))
        self.assertFalse(result.sessions)
        self.assertTrue(result.warnings)

    def test_native_jsonl_budget_is_larger_than_generic_import_budget(self) -> None:
        rows = [
            {"type": "user", "uuid": "u1", "sessionId": "s", "message": {"role": "user", "content": "first"}},
            {"type": "user", "uuid": "u2", "sessionId": "s", "message": {"role": "user", "content": "later"}},
        ]
        file = self.write_jsonl("session.jsonl", rows, source="claude-code")
        generic_cap = len((json.dumps(rows[0]) + "\n").encode()) + 1
        with patch("aik_sessions.parsers.MAX_JSONL_BYTES", generic_cap), patch("aik_sessions.parsers.MAX_NATIVE_JSONL_BYTES", generic_cap * 4):
            native = parse_file(file)
            imported = parse_file(DiscoveredFile(path=file.path))
        self.assertEqual([message.text for message in native.sessions[0].messages], ["first", "later"])
        self.assertFalse(native.warnings)
        self.assertEqual([message.text for message in imported.sessions[0].messages], ["first"])
        self.assertTrue(any("JSONL expanded size limit" in warning for warning in imported.warnings))

    def test_zip_jsonl_uses_generic_budget_even_with_native_source_label(self) -> None:
        rows = [
            {"type": "user", "uuid": "u1", "sessionId": "s", "message": {"role": "user", "content": "first"}},
            {"type": "user", "uuid": "u2", "sessionId": "s", "message": {"role": "user", "content": "later"}},
        ]
        archive_path = self.root / "native-labeled.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("session.jsonl", "".join(json.dumps(row) + "\n" for row in rows))
        generic_cap = len((json.dumps(rows[0]) + "\n").encode()) + 1
        with patch("aik_sessions.parsers.MAX_JSONL_BYTES", generic_cap), patch("aik_sessions.parsers.MAX_NATIVE_JSONL_BYTES", generic_cap * 4):
            parsed = parse_file(DiscoveredFile(path=str(archive_path), source="claude-code"))
        self.assertEqual([message.text for message in parsed.sessions[0].messages], ["first"])
        self.assertTrue(any("JSONL expanded size limit" in warning for warning in parsed.warnings))

    def test_native_oversized_record_is_still_skipped_and_warned(self) -> None:
        rows = [
            {"type": "attachment", "sessionId": "s", "unused": "x" * 1000},
            {"type": "user", "uuid": "u1", "sessionId": "s", "message": {"role": "user", "content": "later"}},
        ]
        file = self.write_jsonl("session.jsonl", rows, source="claude-code")
        with patch("aik_sessions.parsers.MAX_NATIVE_JSONL_LINE_BYTES", 200):
            parsed = parse_file(file)
        self.assertEqual([message.text for message in parsed.sessions[0].messages], ["later"])
        self.assertTrue(any("Skipped oversized JSONL record" in warning for warning in parsed.warnings))


if __name__ == "__main__":
    unittest.main()
