"""Real SDK/subprocess and ASGI transport tests, using synthetic conversations only."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from aik_sessions.index import SessionIndex
from aik_sessions.server import BearerGate, _annotate_search, create_server
from aik_sessions.service import SessionService, Settings


def structured(result):
    value = result.model_dump(by_alias=True)
    if value.get("structuredContent") is not None:
        return value["structuredContent"]
    for item in value["content"]:
        if item["type"] == "text":
            return json.loads(item["text"])
    raise AssertionError("Missing structured tool response")


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        folder = self.home / ".codex/sessions/2026/11/11"
        folder.mkdir(parents=True)
        records = [
            {"type": "session_meta", "payload": {"id": "fixture", "cwd": "/synthetic-project"}},
            {"type": "response_item", "timestamp": "2026-11-11T12:00:00Z", "payload": {
                "type": "message", "id": "u1", "role": "user", "content": [
                    {"type": "input_text", "text": "Find the synthetic harbor webhook."}]}},
        ]
        (folder / "rollout-fixture.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n")
        self.settings = Settings(data_dir=self.root / "state", home=self.home)
        SessionService(self.settings).index_sources()

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_real_stdio_tool_discovery_search_read_and_validation(self):
        src = str(Path(__file__).resolve().parents[1] / "src")
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "aik_sessions", "--home", str(self.home), "--data-dir",
                  str(self.settings.data_dir), "serve", "--no-auto-index"],
            env={"PYTHONPATH": src},
        )
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                tools = await client.list_tools()
                self.assertEqual({tool.name for tool in tools.tools}, {
                    "sessions_discover", "sessions_status", "sessions_refresh",
                    "sessions_search", "sessions_read", "sessions_export_batch",
                })
                results = structured(await client.call_tool("sessions_search", {"query": "harbor"}))
                self.assertTrue(results["results"])
                match = results["results"][0]
                page = structured(await client.call_tool("sessions_read", {
                    "session_id": match["session_id"]}))
                self.assertIn("harbor", page["messages"][0]["text"])
                invalid = await client.call_tool("sessions_search", {"query": "harbor", "limit": 0})
                self.assertTrue(invalid.model_dump(by_alias=True).get("isError"))
                empty = structured(await client.call_tool("sessions_search", {"query": "nonexistent"}))
                self.assertEqual(empty["results"], [])
                exported = structured(await client.call_tool("sessions_export_batch", {}))
                self.assertTrue(exported["documents"])
                minimum = structured(await client.call_tool("sessions_export_batch", {
                    "max_chars": 1024}))
                self.assertTrue(minimum["documents"])
                too_small = await client.call_tool("sessions_export_batch", {"max_chars": 1000})
                self.assertTrue(too_small.model_dump(by_alias=True).get("isError"))

    def _stdio(self, *extra):
        src = str(Path(__file__).resolve().parents[1] / "src")
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "aik_sessions", "--home", str(self.home), "--data-dir",
                  str(self.settings.data_dir), *extra, "serve", "--no-auto-index"],
            env={"PYTHONPATH": src},
        )

    @staticmethod
    def _error_text(result):
        value = result.model_dump(by_alias=True)
        assert value.get("isError"), value
        return " ".join(item.get("text", "") for item in value["content"])

    async def test_failures_tell_the_calling_model_what_to_fix(self):
        async with stdio_client(self._stdio()) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                unknown = self._error_text(await client.call_tool(
                    "sessions_read", {"session_id": "codex:missing"}))
                self.assertIn("Session not found: codex:missing", unknown)
                self.assertIn("source prefix", unknown)
                bad_date = self._error_text(await client.call_tool(
                    "sessions_search", {"query": "harbor", "start_date": "last week"}))
                self.assertIn("YYYY-MM-DD", bad_date)
                reversed_dates = self._error_text(await client.call_tool(
                    "sessions_search", {"query": "harbor", "start_date": "2026-11-12",
                                        "end_date": "2026-11-01"}))
                self.assertIn("on or before end_date", reversed_dates)
                cursor = self._error_text(await client.call_tool(
                    "sessions_export_batch", {"after": "garbage"}))
                self.assertIn("Invalid export cursor", cursor)

    async def test_search_results_compose_with_read_and_explain_empty_pages(self):
        async with stdio_client(self._stdio()) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                found = structured(await client.call_tool("sessions_search", {"query": "harbor"}))
                hit = found["results"][0]
                self.assertEqual((hit["position"], hit["role"]), (0, "user"))
                self.assertFalse(found["has_more"])
                self.assertIsNone(found["next_offset"])
                self.assertNotIn("note", found)
                page = structured(await client.call_tool("sessions_read", {
                    "session_id": hit["session_id"], "offset": hit["position"]}))
                self.assertEqual(page["messages"][0]["id"], hit["message_id"])
                missing = structured(await client.call_tool(
                    "sessions_search", {"query": "nonexistent", "source": "codex"}))
                self.assertIn("keyword based", missing["note"])
                self.assertIn("filters", missing["note"])
                symbols = structured(await client.call_tool("sessions_search", {"query": "!!!"}))
                self.assertIn("no searchable words", symbols["note"])
                status = structured(await client.call_tool("sessions_status", {}))
                self.assertEqual(status["indexing"]["state"], "never_run")
                self.assertEqual(status["stats"]["sessions"], 1)

    async def test_empty_index_search_says_how_to_recover(self):
        empty = Settings(data_dir=self.root / "empty-state", home=self.root / "no-home")
        server, _ = create_server(empty)
        result = await server.call_tool("sessions_search", {"query": "harbor"})
        self.assertIn("sessions_refresh", json.dumps(result, default=str))

    async def test_every_tool_and_parameter_is_described_for_the_model(self):
        async with stdio_client(self._stdio()) as streams:
            async with ClientSession(*streams) as client:
                initialized = await client.initialize()
                self.assertIn("sessions_search", initialized.instructions)
                for tool in (await client.list_tools()).tools:
                    described = tool.model_dump(by_alias=True)
                    self.assertGreater(len(described["description"]), 120, tool.name)
                    self.assertTrue(described.get("title"), tool.name)
                    self.assertIsNotNone(described["annotations"]["readOnlyHint"], tool.name)
                    for name, schema in described["inputSchema"].get("properties", {}).items():
                        self.assertTrue(schema.get("description"), f"{tool.name}.{name}")

    async def test_search_notes_cover_building_index_and_offset_past_the_end(self):
        index = SessionIndex(self.settings.database)

        class Building:
            def progress(self):
                return {"state": "running"}

        hit = _annotate_search(index.search("harbor"), index, Building(),
                               offset=0, filtered=False)
        self.assertTrue(hit["results"])
        self.assertIn("still building", hit["note"])
        idle = SessionService(self.settings)
        past_end = _annotate_search(index.search("harbor", offset=5), index, idle,
                                    offset=5, filtered=False)
        self.assertEqual((past_end["results"], past_end["total"]), ([], 1))
        self.assertEqual(past_end["note"], "No more results at this offset.")
        title_aware = _annotate_search(index.search("nonexistent"), index, idle,
                                       offset=0, filtered=False)
        self.assertIn("conversation title", title_aware["note"])

    async def test_http_bearer_rejects_before_app_and_lifespan_is_forwarded(self):
        calls = []

        async def downstream(scope, receive, send):
            calls.append(scope["type"])
            if scope["type"] == "http":
                await send({"type": "http.response.start", "status": 204, "headers": []})
                await send({"type": "http.response.body", "body": b""})

        gate = BearerGate(downstream, "a" * 32)
        for headers in ([], [(b"authorization", b"Bearer wrong")],
                        [(b"authorization", b"Bearer " + b"a" * 32)] * 2):
            sent = []

            async def send(message):
                sent.append(message)

            await gate({"type": "http", "headers": headers}, None, send)
            self.assertEqual(sent[0]["status"], 401)
        self.assertEqual(calls, [])
        sent = []

        async def send(message):
            sent.append(message)

        await gate({"type": "http", "headers": [
            (b"authorization", b"Bearer " + b"a" * 32)]}, None, send)
        self.assertEqual(sent[0]["status"], 204)
        await gate({"type": "lifespan"}, None, send)
        self.assertEqual(calls, ["http", "lifespan"])

    async def test_real_http_sdk_app_auth_host_and_discovery(self):
        import httpx2

        server, _ = create_server(self.settings)
        app = server.streamable_http_app(stateless_http=True, json_response=True)
        gate = BearerGate(app, "b" * 32)
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=gate),
                                         base_url="http://127.0.0.1:8765") as client:
                body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "synthetic-test", "version": "1"}}}
                response = await client.post("/mcp", json=body)
                self.assertEqual(response.status_code, 401)
                headers = {"Authorization": "Bearer " + "b" * 32,
                           "Accept": "application/json, text/event-stream"}
                response = await client.post("/mcp", json=body, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("result", response.json())
                response = await client.post("/mcp", json=body, headers={
                    **headers, "Host": "untrusted.invalid"})
                self.assertEqual(response.status_code, 421)


if __name__ == "__main__":
    unittest.main()
