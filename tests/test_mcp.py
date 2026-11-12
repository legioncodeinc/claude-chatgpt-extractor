"""Real SDK/subprocess and ASGI transport tests, using synthetic conversations only."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from aik_sessions.server import BearerGate, create_server
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
