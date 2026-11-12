"""Synthetic Littlebird tests. No network, browser, or real credentials."""

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from aik_sessions.littlebird import (
    CALLBACK_PORT, FileTokenStorage, LittlebirdError, _CallbackReceiver,
    ingest, select_ingest_tool, send_batch,
)


def document(identity="doc-1", content="Synthetic session text"):
    return {"id": identity, "session_id": "synthetic-session", "content": content,
            "content_hash": f"hash-{identity}"}


class FakeIndex:
    def __init__(self, documents=None):
        self.documents = documents if documents is not None else [document()]
        self.ledger = {}
        self.history = []

    def export_batch(self, **kwargs):
        return {"documents": self.documents[:kwargs["limit"]], "next_cursor": "cursor-2", "done": True}

    def delivered(self, identity, digest):
        return self.ledger.get((identity, digest)) == "sent"

    def delivery_status(self, identity, digest):
        return self.ledger.get((identity, digest))

    def mark_delivery(self, identity, digest, status, error=None):
        self.ledger[(identity, digest)] = status
        self.history.append((identity, status, error))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_checkpoints_before_and_after_dispatch(self):
        index = FakeIndex()
        async def sender(content):
            self.assertEqual(index.delivery_status("doc-1", "hash-doc-1"), "uncertain")
            self.assertEqual(content, "Synthetic session text")
            return {"isError": False, "content": []}
        report = await send_batch(index, index.documents, sender)
        self.assertEqual(report["sent"], 1)
        self.assertTrue(index.delivered("doc-1", "hash-doc-1"))
        self.assertEqual([item[1] for item in index.history], ["uncertain", "sent"])

    async def test_tool_error_after_dispatch_records_uncertain_and_stops(self):
        index = FakeIndex([document(), document("doc-2")])
        sender = AsyncMock(return_value={"isError": True})
        report = await send_batch(index, index.documents, sender)
        self.assertEqual(report["uncertain"], 1)
        # resolve-delivery needs both values, so the report must carry both.
        self.assertEqual((report["blocked_document_id"], report["blocked_content_hash"]),
                         ("doc-1", "hash-doc-1"))
        self.assertEqual(sender.await_count, 1)
        self.assertEqual(index.delivery_status("doc-1", "hash-doc-1"), "uncertain")

    async def test_side_effect_then_tool_error_is_never_automatically_repeated(self):
        index = FakeIndex()
        remote_appends = []
        async def sender(content):
            remote_appends.append(content)
            return {"isError": True, "content": []}
        first = await send_batch(index, index.documents, sender)
        second = await send_batch(index, index.documents, sender)
        self.assertEqual(first["uncertain"], 1)
        self.assertEqual(second["uncertain"], 1)
        self.assertEqual(len(remote_appends), 1)

    async def test_timeout_is_uncertain_without_retry(self):
        index = FakeIndex()
        sender = AsyncMock(side_effect=TimeoutError("synthetic private detail must not escape"))
        report = await send_batch(index, index.documents, sender)
        self.assertEqual(report["uncertain"], 1)
        self.assertEqual(sender.await_count, 1)
        self.assertEqual(index.delivery_status("doc-1", "hash-doc-1"), "uncertain")
        self.assertNotIn("private detail", str(report))

    async def test_resume_skips_acknowledged_content(self):
        index = FakeIndex([document(), document("doc-2")])
        index.mark_delivery("doc-1", "hash-doc-1", "sent")
        sender = AsyncMock(return_value=SimpleNamespace(is_error=False, result_type="complete", content=[]))
        report = await send_batch(index, index.documents, sender)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(report["sent"], 1)
        self.assertEqual(sender.await_count, 1)

    async def test_prior_uncertainty_preflights_entire_batch(self):
        index = FakeIndex([document(), document("doc-2")])
        index.mark_delivery("doc-2", "hash-doc-2", "uncertain")
        sender = AsyncMock()
        report = await send_batch(index, index.documents, sender)
        self.assertEqual(report["uncertain"], 1)
        sender.assert_not_awaited()

    async def test_cancellation_leaves_unknown_outcome(self):
        index = FakeIndex()
        sender = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await send_batch(index, index.documents, sender)
        self.assertEqual(index.delivery_status("doc-1", "hash-doc-1"), "uncertain")

    async def test_missing_acknowledgement_is_not_marked_sent(self):
        index = FakeIndex()
        report = await send_batch(index, index.documents, AsyncMock(return_value=None))
        self.assertEqual(report["uncertain"], 1)
        self.assertFalse(index.delivered("doc-1", "hash-doc-1"))

    async def test_dry_run_never_connects_reads_tokens_or_creates_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "not-created"
            index = FakeIndex([document(str(n), "x" * 1000) for n in range(10)])
            with patch("aik_sessions.littlebird._connected_session", side_effect=AssertionError("network")), \
                 patch.object(FileTokenStorage, "get_tokens", side_effect=AssertionError("credentials")):
                report = await ingest(index, data_dir=directory)
            self.assertTrue(report["dry_run"])
            self.assertEqual(len(report["preview"]), 3)
            self.assertLessEqual(len(report["preview"][0]["text"]), 240)
            self.assertFalse(directory.exists())
            self.assertFalse(index.history)

    async def test_uncertain_send_never_connects(self):
        index = FakeIndex()
        index.mark_delivery("doc-1", "hash-doc-1", "uncertain")
        with patch("aik_sessions.littlebird._connected_session", side_effect=AssertionError("network")):
            report = await ingest(index, data_dir=Path("unused"), send=True, after="old-cursor")
        self.assertEqual(report["next_cursor"], "old-cursor")
        self.assertFalse(report["done"])

    @asynccontextmanager
    async def connected(self, result):
        session = SimpleNamespace(
            list_tools=AsyncMock(return_value=SimpleNamespace(tools=[ToolContractTests().tool()], next_cursor=None)),
            call_tool=AsyncMock(side_effect=result if isinstance(result, Exception) else None,
                                return_value=result if not isinstance(result, Exception) else None),
        )
        self.session = session
        yield session

    async def test_ingest_entrypoint_success_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            index = FakeIndex()
            with patch("aik_sessions.littlebird._connected_session", lambda _: self.connected({"content": [], "isError": False})):
                report = await ingest(index, data_dir=Path(temporary), send=True)
            self.assertEqual(report["sent"], 1)
            self.assertEqual(report["next_cursor"], "cursor-2")
            self.assertTrue(report["done"])
            self.session.call_tool.assert_awaited_once_with("ingest", {"content": "Synthetic session text"}, read_timeout_seconds=60.0)
            self.assertFalse((Path(temporary) / "littlebird-ingest.lock").exists())
            with patch("aik_sessions.littlebird._connected_session", side_effect=AssertionError("unneeded network")):
                resumed = await ingest(index, data_dir=Path(temporary), send=True)
            self.assertEqual(resumed["skipped"], 1)

    async def test_ingest_entrypoint_failure_preserves_resume_cursor(self):
        with tempfile.TemporaryDirectory() as temporary:
            index = FakeIndex([document(), document("doc-2")])
            with patch("aik_sessions.littlebird._connected_session", lambda _: self.connected(TimeoutError("synthetic"))):
                report = await ingest(index, data_dir=Path(temporary), send=True, after="original-cursor")
            self.assertEqual(report["uncertain"], 1)
            self.assertEqual(report["next_cursor"], "original-cursor")
            self.assertFalse(report["done"])
            self.assertEqual(self.session.call_tool.await_count, 1)

    async def test_missing_credentials_do_not_start_browser_or_transport(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.dict("os.environ", {}, clear=True), \
             patch("aik_sessions.littlebird.streamable_http_client", side_effect=AssertionError("network")), \
             patch("aik_sessions.littlebird.webbrowser.open", side_effect=AssertionError("browser")):
            report = await ingest(FakeIndex(), data_dir=Path(temporary), send=True)
        self.assertIn("explicit Littlebird auth", report["error"])

    async def test_real_index_pages_large_messages_and_checkpoints_hashes(self):
        from aik_sessions.index import SessionIndex
        from aik_sessions.models import Message, Session
        with tempfile.TemporaryDirectory() as temporary:
            index = SessionIndex(Path(temporary) / "index.sqlite3")
            index.upsert(Session(
                id="codex:synthetic-session", source="codex", title="Synthetic paging",
                path="/synthetic/session.jsonl", messages=[
                    Message(id="m1", role="user", text="alpha " * 6000),
                    Message(id="m2", role="assistant", text="beta " * 6000),
                ],
            ))
            with patch("aik_sessions.littlebird._connected_session", lambda _: self.connected({"content": [], "isError": False})):
                report = await ingest(index, data_dir=Path(temporary), send=True, max_documents=100)
            self.assertGreater(report["sent"], 2)
            self.assertTrue(report["done"])
            self.assertEqual(index.ingestion_status()["sent"], report["sent"])
            with patch("aik_sessions.littlebird._connected_session", side_effect=AssertionError("duplicate network")):
                repeat = await ingest(index, data_dir=Path(temporary), send=True)
            self.assertEqual(repeat["skipped"], report["sent"])

    async def test_more_than_one_hundred_documents_stream_in_bounded_pages(self):
        class PagedIndex(FakeIndex):
            def __init__(self):
                super().__init__([document(str(number)) for number in range(250)])
                self.limits = []
            def export_batch(self, **kwargs):
                self.limits.append(kwargs["limit"])
                offset = int(kwargs["after"] or 0)
                end = min(offset + min(kwargs["limit"], 25), len(self.documents))
                return {"documents": self.documents[offset:end],
                        "next_cursor": str(end) if end < len(self.documents) else None,
                        "done": end == len(self.documents)}
        with tempfile.TemporaryDirectory() as temporary:
            index = PagedIndex()
            with patch("aik_sessions.littlebird._connected_session", lambda _: self.connected({"content": [], "isError": False})):
                report = await ingest(index, data_dir=Path(temporary), send=True, max_documents=200)
            self.assertEqual(report["sent"], 200)
            self.assertEqual(report["next_cursor"], "200")
            self.assertFalse(report["done"])
            self.assertLessEqual(max(index.limits), 100)
            self.assertEqual(len(report["preview"]), 3)
            with patch("aik_sessions.littlebird._connected_session", lambda _: self.connected({"content": [], "isError": False})):
                resumed = await ingest(index, data_dir=Path(temporary), send=True, max_documents=200, after="200")
            self.assertEqual(resumed["sent"], 50)
            self.assertTrue(resumed["done"])


class ToolContractTests(unittest.TestCase):
    def tool(self, name="ingest", **extra):
        return {"name": name, "inputSchema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}, **extra}

    def test_exact_name_wins_and_unique_prefixed_name_supported(self):
        selected = select_ingest_tool([self.tool("other_ingest"), self.tool()])
        self.assertEqual(selected["name"], "ingest")
        self.assertEqual(select_ingest_tool([self.tool("littlebird_ingest")])["name"], "littlebird_ingest")

    def test_ambiguous_names_and_incompatible_schema_rejected(self):
        with self.assertRaises(LittlebirdError):
            select_ingest_tool([self.tool("first_ingest"), self.tool("second_ingest")])
        tool = self.tool()
        tool["inputSchema"]["required"].append("unexpected")
        with self.assertRaises(LittlebirdError):
            select_ingest_tool([tool])

    def test_callback_accepts_only_expected_loopback_path_and_state(self):
        receiver = _CallbackReceiver()
        receiver.expected_state = "synthetic-state"
        result = receiver.parse_callback("/callback?state=synthetic-state&code=synthetic-code", f"127.0.0.1:{CALLBACK_PORT}")
        self.assertEqual(result.code, "synthetic-code")
        for target, host in [("/callback?state=wrong&code=x", f"127.0.0.1:{CALLBACK_PORT}"),
                             ("/other?state=synthetic-state&code=x", f"127.0.0.1:{CALLBACK_PORT}"),
                             ("/callback?state=synthetic-state&code=x", "hostile.invalid")]:
            with self.assertRaises(LittlebirdError):
                receiver.parse_callback(target, host)


class TokenStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_windows_policy_uses_profile_acl_without_posix_fchmod(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = FileTokenStorage(Path(temporary))
            windows_os = Mock(wraps=os)
            windows_os.name = "nt"
            windows_os.O_RDONLY = os.O_RDONLY
            windows_os.O_NOFOLLOW = 0
            with patch("aik_sessions.littlebird.os", windows_os):
                await storage.set_tokens(OAuthToken(access_token="synthetic-token", token_type="Bearer", expires_in=60))
                storage.directory.chmod(0o755)
                (storage.directory / "tokens.json").chmod(0o644)
                self.assertEqual((await storage.get_tokens()).access_token, "synthetic-token")
                windows_os.fchmod.assert_not_called()
                (storage.directory / "client.json").symlink_to(storage.directory / "tokens.json")
                with self.assertRaises(LittlebirdError):
                    await storage.get_client_info()

    async def test_synthetic_tokens_private_and_expiry_not_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = FileTokenStorage(Path(temporary))
            with patch("aik_sessions.littlebird.time.time", return_value=1000):
                await storage.set_tokens(OAuthToken(access_token="synthetic-token", token_type="Bearer", expires_in=60))
            with patch("aik_sessions.littlebird.time.time", return_value=1040):
                tokens = await storage.get_tokens()
            self.assertEqual(tokens.expires_in, 20)
            self.assertEqual(stat.S_IMODE((storage.directory / "tokens.json").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(storage.directory.stat().st_mode), 0o700)

    async def test_client_registration_round_trip_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = FileTokenStorage(Path(temporary))
            info = OAuthClientInformationFull(client_id="synthetic-client", redirect_uris=[_CallbackReceiver.redirect_uri])
            await storage.set_client_info(info)
            self.assertEqual((await storage.get_client_info()).client_id, "synthetic-client")
            (storage.directory / "tokens.json").symlink_to(storage.directory / "client.json")
            with self.assertRaises(LittlebirdError):
                await storage.get_tokens()


if __name__ == "__main__":
    unittest.main()
