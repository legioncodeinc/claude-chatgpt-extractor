"""Synthetic-only index tests. No local conversation folders are inspected."""

from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aik_sessions.index import SessionIndex
from aik_sessions.models import Message, Session


def session(
    suffix: str = "one", *, source: str = "codex", text: str = "A webhook was received",
    project: str = "/projects/demo", timestamp: str = "2026-11-11T12:00:00Z",
    title: str = "Synthetic session",
) -> Session:
    return Session(
        id=f"{source}:{suffix}", source=source, title=title, path="/synthetic/session.jsonl",
        project=project, started_at=timestamp, updated_at=timestamp,
        messages=[Message(id="m1", role="user", text=text, timestamp=timestamp)],
    )


class IndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.index = SessionIndex(self.root / "private" / "index.sqlite3")

    def tearDown(self) -> None:
        self.index.close()
        self.temporary.cleanup()

    def test_upsert_idempotency_and_duplicate_message_ids(self) -> None:
        item = session()
        item.messages += [
            Message(id="m1", role="user", text="A webhook was received", timestamp=item.started_at),
            Message(id="m2", role="assistant", text="Inspect the response"),
        ]
        self.assertEqual(self.index.upsert(item)["status"], "inserted")
        self.assertEqual(self.index.upsert(item)["status"], "unchanged")
        self.assertEqual(self.index.stats()["messages"], 2)
        self.assertEqual(self.index.stats()["sessions"], 1)
        self.assertEqual(self.index.stats()["revision"], 1)
        self.assertEqual(self.index.search("webhook")["total"], 1)

    def test_update_replaces_old_fts_entries(self) -> None:
        item = session(text="obsoleteword")
        self.index.upsert(item)
        item.messages = [Message(id="new", role="assistant", text="replacementword")]
        self.assertEqual(self.index.upsert(item)["status"], "updated")
        self.assertEqual(self.index.search("obsoleteword")["total"], 0)
        self.assertEqual(self.index.search("replacementword")["total"], 1)
        self.assertEqual(self.index.stats()["messages"], 1)

    def test_newest_duplicate_export_wins_in_either_discovery_order(self) -> None:
        older = session(source="chatgpt-export", text="original turn", timestamp="2026-11-10T18:00:00Z")
        older.path = "/synthetic/export-old/conversations.json"
        newer = older.model_copy(deep=True)
        newer.path = "/synthetic/export-new/conversations.json"
        newer.updated_at = "2026-11-11T18:00:00Z"
        newer.messages.append(Message(id="m2", role="assistant", text="newestunique history", timestamp=newer.updated_at))
        for number, snapshots in enumerate(((older, newer), (newer, older))):
            with self.subTest(order=number):
                index = SessionIndex(self.root / f"order-{number}" / "index.sqlite3")
                first = index.upsert(snapshots[0])
                revision = index.stats()["revision"]
                second = index.upsert(snapshots[1])
                self.assertEqual(first["status"], "inserted")
                self.assertEqual(index.read(newer.id)["total"], 2)
                self.assertEqual(index.read(newer.id)["session"]["path"], newer.path)
                self.assertEqual(index.search("newestunique")["total"], 1)
                if number == 1:
                    self.assertEqual(second["status"], "ignored_older")
                    self.assertEqual(second["reason"], "older_snapshot_timestamp")
                    self.assertEqual(index.stats()["revision"], revision)

    def test_snapshot_timestamps_are_compared_in_utc(self) -> None:
        first = session(text="earlier", timestamp="2026-11-11T01:00:00+02:00")
        first.path = "/synthetic/first.json"
        later = session(text="laterunique", timestamp="2026-11-10T23:30:00Z")
        later.path = "/synthetic/later.json"
        self.index.upsert(first)
        self.assertEqual(self.index.upsert(later)["status"], "updated")
        self.assertEqual(self.index.search("laterunique")["total"], 1)

    def test_snapshot_time_falls_back_to_latest_visible_message_then_start(self) -> None:
        newest = session(text="latestvisible", timestamp="2026-11-11T20:00:00Z")
        newest.path = "/synthetic/newest.json"
        newest.updated_at = "not-an-iso-date"
        newest.started_at = "2026-11-01T00:00:00Z"
        newest.messages.append(Message(id="m2", role="assistant", text="tail without a time"))
        older = session(text="older", timestamp="2026-11-10T20:00:00Z")
        older.path = "/synthetic/older.json"
        older.updated_at = None
        self.index.upsert(newest)
        self.assertEqual(self.index.upsert(older)["status"], "ignored_older")
        self.assertEqual(self.index.search("latestvisible")["total"], 1)
        started_only = session("start-only", text="start time", timestamp="2026-11-11T20:00:00Z")
        started_only.path = "/synthetic/start-new.json"
        started_only.updated_at = None
        started_only.messages[0].timestamp = None
        earlier_start = started_only.model_copy(deep=True)
        earlier_start.path = "/synthetic/start-old.json"
        earlier_start.started_at = "2026-11-09T00:00:00Z"
        self.index.upsert(started_only)
        self.assertEqual(self.index.upsert(earlier_start)["status"], "ignored_older")

    def test_undated_duplicate_cannot_discard_dated_history(self) -> None:
        dated = session(text="known dated history")
        dated.path = "/synthetic/dated.json"
        undated = dated.model_copy(deep=True)
        undated.path = "/synthetic/undated.json"
        undated.started_at = undated.updated_at = None
        undated.messages[0].timestamp = None
        undated.messages[0].text = "different undated history"
        self.index.upsert(dated)
        result = self.index.upsert(undated)
        self.assertEqual(result["status"], "ignored_ambiguous")
        self.assertEqual(result["reason"], "undated_snapshot_cannot_replace_dated_history")
        self.assertEqual(self.index.stats()["revision"], 1)
        self.assertEqual(self.index.read(dated.id)["messages"][0]["text"], dated.messages[0].text)

    def test_undated_prefix_extension_wins_in_either_order(self) -> None:
        short = session(text="shared original turn")
        short.path = "/synthetic/short.json"
        short.started_at = short.updated_at = None
        short.messages[0].timestamp = None
        long = short.model_copy(deep=True)
        long.path = "/synthetic/long.json"
        long.messages.append(Message(id="m2", role="assistant", text="continuation"))
        for number, snapshots in enumerate(((short, long), (long, short))):
            with self.subTest(order=number):
                index = SessionIndex(self.root / f"undated-order-{number}" / "index.sqlite3")
                index.upsert(snapshots[0])
                result = index.upsert(snapshots[1])
                self.assertEqual(index.read(short.id)["total"], 2)
                self.assertEqual(index.read(short.id)["session"]["path"], long.path)
                if number == 1:
                    self.assertEqual(result["status"], "ignored_ambiguous")
                    self.assertEqual(index.stats()["revision"], 1)

    def test_tied_timestamp_different_branches_are_not_merged_or_replaced(self) -> None:
        original = session(text="shared original turn")
        original.path = "/synthetic/active-branch.json"
        original.messages.append(Message(id="active", role="assistant", text="activeanswer"))
        other = original.model_copy(deep=True)
        other.path = "/synthetic/inactive-branch.json"
        other.messages[1] = Message(id="inactive", role="assistant", text="inactiveanswer")
        other.messages.append(Message(id="extra", role="user", text="another inactive turn"))
        self.index.upsert(original)
        result = self.index.upsert(other)
        self.assertEqual(result["status"], "ignored_ambiguous")
        self.assertEqual(result["reason"], "conflicting_snapshot_without_newer_timestamp")
        self.assertEqual(self.index.read(original.id)["total"], 2)
        self.assertEqual(self.index.search("activeanswer")["total"], 1)
        self.assertEqual(self.index.search("inactiveanswer")["total"], 0)
        self.assertEqual(self.index.stats()["revision"], 1)

    def test_identical_turns_in_another_path_do_not_advance_revision(self) -> None:
        original = session()
        duplicate = original.model_copy(deep=True)
        duplicate.path = "/synthetic/another-export.json"
        self.index.upsert(original)
        self.assertEqual(self.index.upsert(duplicate)["status"], "unchanged")
        self.assertEqual(self.index.stats()["revision"], 1)

    def test_same_path_truncation_remains_authoritative_even_with_older_timestamp(self) -> None:
        current = session()
        current.messages.append(Message(id="m2", role="assistant", text="removed by truncation"))
        self.index.upsert(current)
        truncated = current.model_copy(deep=True)
        truncated.messages = []
        truncated.updated_at = "2026-01-01T00:00:00Z"
        self.assertEqual(self.index.upsert(truncated)["status"], "updated")
        self.assertEqual(self.index.read(current.id)["total"], 0)

    def test_provider_namespaces_prevent_same_native_id_collision(self) -> None:
        self.index.upsert(session(source="codex"))
        self.index.upsert(session(source="claude-code"))
        self.assertEqual(self.index.stats()["sessions"], 2)
        self.assertEqual(self.index.stats()["by_source"]["codex"]["messages"], 1)
        invalid = session().model_copy(update={"id": "not-namespaced"})
        with self.assertRaises(ValueError):
            self.index.upsert(invalid)

    def test_search_and_filters(self) -> None:
        self.index.upsert(session("a", text="red fox", project="/projects/100%_demo"))
        self.index.upsert(session("b", source="claude-code", text="red quick fox", timestamp="2026-11-10T18:00:00Z"))
        self.index.upsert(session("c", text="unrelated text"))
        self.assertEqual(self.index.search("red fox")["total"], 2)
        self.assertEqual(self.index.search('"red fox"')["total"], 1)
        self.assertEqual(self.index.search("red", source="claude-code")["total"], 1)
        self.assertEqual(self.index.search("red", project="100%_demo")["total"], 1)
        self.assertEqual(self.index.search("red", start_date="2026-11-11", end_date="2026-11-11")["total"], 1)
        result = self.index.search("red", limit=1, offset=1)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["total"], 2)
        for row in self.index.search("red")["results"]:
            self.assertLessEqual(len(row["snippet"]), 500)
            self.assertIn("score", row)
            self.assertNotIn("text", row)

    def test_title_search_is_ranked(self) -> None:
        self.index.upsert(session("a", title="Webhook debugging", text="Inspect received requests"))
        self.index.upsert(session("b", text="A webhook arrives"))
        result = self.index.search("webhook")
        self.assertEqual(result["total"], 2)
        self.assertLessEqual(result["results"][0]["score"], result["results"][1]["score"])

    def test_search_never_interprets_fts_operators_or_sql(self) -> None:
        self.index.upsert(session())
        for query in ("'; DROP TABLE sessions; --", "webhook OR missing", "body:webhook", "***", '"', "(not closed", "NEAR(webhook)"):
            result = self.index.search(query)
            self.assertIsInstance(result["results"], list)
        self.assertEqual(self.index.stats()["sessions"], 1)

    def test_pagination_and_argument_bounds(self) -> None:
        self.index.upsert(session())
        with self.assertRaises(ValueError):
            self.index.search(" ")
        with self.assertRaises(ValueError):
            self.index.search("webhook", limit=0)
        with self.assertRaises(ValueError):
            self.index.search("webhook", offset=-1)
        with self.assertRaises(ValueError):
            self.index.search("webhook", source="other")
        with self.assertRaises(ValueError):
            self.index.search("webhook", end_date="bad date")
        with self.assertRaises(ValueError):
            self.index.search("webhook", limit=True)
        with self.assertRaises(ValueError):
            self.index.search("webhook", project=12)
        with self.assertRaises(ValueError):
            self.index.search("webhook", end_date=12)

    def test_search_reports_position_and_explicit_paging(self) -> None:
        item = session()
        item.messages.append(Message(id="m-extra", role="assistant",
                                     text="A later reply about the zeppelin manifest."))
        item.messages.append(Message(id="m-extra-2", role="user",
                                     text="Confirm the zeppelin manifest again."))
        self.index.upsert(item)
        first = self.index.search("zeppelin", limit=1)
        self.assertEqual((first["total"], first["has_more"], first["next_offset"]), (2, True, 1))
        last = self.index.search("zeppelin", limit=1, offset=first["next_offset"])
        self.assertEqual((last["has_more"], last["next_offset"]), (False, None))
        positions = {first["results"][0]["position"], last["results"][0]["position"]}
        self.assertEqual(positions, {len(item.messages) - 2, len(item.messages) - 1})
        for hit in (first["results"][0], last["results"][0]):
            page = self.index.read(item.id, offset=hit["position"], limit=1)
            self.assertEqual(page["messages"][0]["id"], hit["message_id"])
            self.assertEqual(page["messages"][0]["role"], hit["role"])
        unsearchable = self.index.search("?!")
        self.assertEqual(unsearchable["results"], [])
        self.assertIn("no searchable words", unsearchable["note"])

    def test_title_match_returns_messages_whose_text_lacks_the_words(self) -> None:
        # The documented contract: words may match the message text or the conversation
        # title. Tool descriptions and docs/tools.md state this; keep them in step.
        item = session(title="Zeppelin budget review", text="thanks, that works")
        self.index.upsert(item)
        result = self.index.search("zeppelin budget")
        self.assertEqual(result["total"], 1)
        self.assertNotIn("zeppelin", result["results"][0]["snippet"].lower())

    def test_read_is_paginated_and_missing_is_explicit(self) -> None:
        item = session()
        item.messages.append(Message(id="m2", role="assistant", text="A response"))
        self.index.upsert(item)
        result = self.index.read(item.id, offset=1, limit=1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["messages"][0]["id"], "m2")
        self.assertEqual(result["session"]["source"], "codex")
        with self.assertRaises(KeyError):
            self.index.read("codex:missing")
        with self.assertRaises(ValueError):
            self.index.read(item.id, limit=101)

    def test_export_utf8_chunks_resume_without_loss_and_respect_byte_budget(self) -> None:
        body = ("Māori 🐦 data café\n" * 750) + "END"
        item = session(text=body)
        item.messages.append(Message(id="m2", role="assistant", text="Second message"))
        self.index.upsert(item)
        cursor = None
        documents = []
        for _ in range(100):
            page = self.index.export_batch(after=cursor, limit=2, max_chars=1024)
            self.assertLessEqual(sum(len(d["content"].encode("utf-8")) for d in page["documents"]), 1024)
            documents.extend(page["documents"])
            if page["done"]:
                self.assertIsNone(page["next_cursor"])
                break
            self.assertNotEqual(page["next_cursor"], cursor)
            cursor = page["next_cursor"]
        else:
            self.fail("Export did not finish")
        first = [d for d in documents if d["message_id"] == "m1"]
        pieces = [d["content"].split("\n\n--- transcript text ---\n", 1)[1] for d in first]
        self.assertEqual("".join(pieces), body)
        self.assertEqual(len({d["id"] for d in documents}), len(documents))
        self.assertIn("Source: codex", first[0]["content"])
        self.assertIn("2026-11-11T12:00:00Z", first[0]["content"])
        self.assertIn("codex:one", first[0]["content"])
        self.assertEqual(self.index.export_batch(max_chars=1024)["documents"][0], documents[0])

    def test_export_cursor_is_bound_to_source_budget_and_revision(self) -> None:
        self.index.upsert(session(text="long message " * 300))
        page = self.index.export_batch(source="codex", limit=1, max_chars=1024)
        self.assertFalse(page["done"])
        with self.assertRaises(ValueError):
            self.index.export_batch(source="claude-code", after=page["next_cursor"], max_chars=1024)
        with self.assertRaises(ValueError):
            self.index.export_batch(source="codex", after=page["next_cursor"], max_chars=2048)
        with self.assertRaises(ValueError):
            self.index.export_batch(after="not-a-cursor")
        self.index.upsert(session("new"))
        with self.assertRaisesRegex(ValueError, "index changed"):
            self.index.export_batch(source="codex", after=page["next_cursor"], max_chars=1024)

    def test_empty_export_and_empty_message_terminate(self) -> None:
        self.assertTrue(self.index.export_batch()["done"])
        self.index.upsert(session(text=""))
        page = self.index.export_batch()
        self.assertTrue(page["done"])
        self.assertEqual(len(page["documents"]), 1)

    def test_delivery_ledger_persists_and_distinguishes_uncertainty(self) -> None:
        self.index.upsert(session())
        document = self.index.export_batch()["documents"][0]
        doc_id, digest = document["id"], document["content_hash"]
        self.assertIsNone(self.index.delivery_status(doc_id, digest))
        self.index.mark_delivery(doc_id, digest, "uncertain", "network response interrupted")
        self.assertFalse(self.index.delivered(doc_id, digest))
        reopened = SessionIndex(self.index.db_path)
        self.assertEqual(reopened.delivery_status(doc_id, digest), "uncertain")
        self.assertEqual(reopened.ingestion_status()["uncertain"], 1)
        reopened.mark_delivery(doc_id, digest, "sent")
        self.assertTrue(self.index.delivered(doc_id, digest))
        self.assertEqual(self.index.ingestion_status(), {"sent": 1, "uncertain": 0, "failed": 0, "total": 1})
        with self.assertRaises(ValueError):
            reopened.mark_delivery(doc_id, digest, "pending")
        with self.assertRaises(ValueError):
            reopened.mark_delivery(doc_id, "not-a-hash", "sent")

    def test_created_database_and_directory_are_private(self) -> None:
        self.assertEqual(self.index.db_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.index.db_path.parent.stat().st_mode & 0o777, 0o700)

    def test_database_symlinks_are_rejected(self) -> None:
        link = self.root / "link.sqlite3"
        link.symlink_to(self.index.db_path)
        with self.assertRaises(ValueError):
            SessionIndex(link)

    def test_existing_broadly_readable_paths_are_rejected_without_chmod(self) -> None:
        directory = self.root / "public"
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "directory must be private"):
            SessionIndex(directory / "index.sqlite3")
        self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
        self.index.db_path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "database must be private"):
            SessionIndex(self.index.db_path)
        self.assertEqual(self.index.db_path.stat().st_mode & 0o777, 0o644)

    def test_suggested_chmod_command_is_safe_to_paste(self) -> None:
        import shlex
        directory = self.root / "o'brien; echo PWNED #"
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
        with self.assertRaises(ValueError) as raised:
            SessionIndex(directory / "index.sqlite3")
        command = str(raised.exception).split("Fix it with: ", 1)[1]
        # A shell must see exactly three words, the last being the untouched path.
        self.assertEqual(shlex.split(command), ["chmod", "700", str(directory)])

    def test_same_index_can_be_called_from_worker_threads(self) -> None:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda i: self.index.upsert(session(str(i))), range(12)))
        self.assertEqual(len(results), 12)
        self.assertEqual(self.index.stats()["sessions"], 12)
        self.assertEqual(self.index.search("webhook")["total"], 12)


if __name__ == "__main__":
    unittest.main()
