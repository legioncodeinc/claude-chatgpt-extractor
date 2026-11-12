"""Explicit OAuth and checkpointed text ingestion through Littlebird's public MCP.

No network, token reads, browser, or callback listener is used by a dry run.
Littlebird publishes no ingestion idempotency contract. A document is reserved as
uncertain before dispatch, and an interrupted call is never blindly retried.

References:
https://docs.littlebird.ai/docs/mcp/#adding-context-from-another-app
https://github.com/modelcontextprotocol/python-sdk/tree/main/examples/clients/simple-auth-client
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager, nullcontext
import hmac
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit
import webbrowser

import httpx2
from mcp import ClientSession, types
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

SERVER_URL = "https://mcp.littlebird.ai/mcp"
CALLBACK_PORT = 47683
CALLBACK_TIMEOUT = 300.0
MAX_DOCUMENT_CHARS = 20_000  # Conservative client bound, not a claimed vendor limit.
MAX_PREVIEW_CHARS = 240


class LittlebirdError(RuntimeError):
    """A safe, actionable local error that does not include token or source text."""


class AuthenticationRequired(LittlebirdError):
    pass


class DeliveryIndex(Protocol):
    def export_batch(self, *, source: str | None, after: str | None,
                     limit: int, max_chars: int) -> dict[str, Any]: ...
    def delivered(self, document_id: str, content_hash: str) -> bool: ...
    def delivery_status(self, document_id: str, content_hash: str) -> str | None: ...
    def mark_delivery(self, document_id: str, content_hash: str,
                      status: str, error: str | None = None) -> None: ...


def _private_directory(directory: Path) -> None:
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or (os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077):
        raise LittlebirdError("Littlebird credential directory must be a private directory (0700).")


class FileTokenStorage:
    """SDK TokenStorage with no symlinks and persisted expiry.

    POSIX credential directories/files require 0700/0600. Windows relies on
    the private per-user profile directory's inherited ACL rather than Unix
    mode bits; use a data directory restricted to the current Windows user.
    """

    def __init__(self, data_dir: Path):
        self.directory = Path(data_dir) / "littlebird-auth"

    def _read(self, name: str) -> dict[str, Any] | None:
        path = self.directory / name
        if not self.directory.exists():
            return None
        _private_directory(self.directory)
        if path.is_symlink():
            raise LittlebirdError("Refusing a symlink at the Littlebird credential path.")
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise LittlebirdError("Cannot safely read Littlebird credentials.") from exc
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077):
                raise LittlebirdError("Littlebird credential files must be regular private files (0600).")
            try:
                value = json.load(stream)
            except (ValueError, UnicodeError) as exc:
                raise LittlebirdError("Stored Littlebird credentials are invalid; authenticate again.") from exc
        if not isinstance(value, dict):
            raise LittlebirdError("Stored Littlebird credentials are invalid; authenticate again.")
        return value

    def _write(self, name: str, value: dict[str, Any]) -> None:
        _private_directory(self.directory)
        target = self.directory / name
        if target.is_symlink():
            raise LittlebirdError("Refusing a symlink at the Littlebird credential path.")
        fd, temporary = tempfile.mkstemp(prefix=".oauth-", dir=self.directory)
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    async def get_tokens(self) -> OAuthToken | None:
        saved = self._read("tokens.json")
        if saved is None:
            return None
        payload = saved.get("tokens")
        if not isinstance(payload, dict):
            raise LittlebirdError("Stored Littlebird token data is invalid; authenticate again.")
        payload = dict(payload)
        expiry = saved.get("expires_at")
        if isinstance(expiry, (float, int)):
            payload["expires_in"] = max(0, math.floor(expiry - time.time()))
        try:
            return OAuthToken.model_validate(payload)
        except ValueError as exc:
            raise LittlebirdError("Stored Littlebird token data is invalid; authenticate again.") from exc

    async def set_tokens(self, tokens: OAuthToken) -> None:
        expiry = time.time() + tokens.expires_in if tokens.expires_in is not None else None
        self._write("tokens.json", {"tokens": tokens.model_dump(mode="json"), "expires_at": expiry})

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        saved = self._read("client.json")
        if saved is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate(saved)
        except ValueError as exc:
            raise LittlebirdError("Stored Littlebird client registration is invalid; authenticate again.") from exc

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._write("client.json", client_info.model_dump(mode="json"))


class _CallbackReceiver:
    """A bounded loopback-only callback, constructed only by authenticate()."""

    redirect_uri = f"http://127.0.0.1:{CALLBACK_PORT}/callback"

    def __init__(self) -> None:
        self.expected_state: str | None = None
        self.result: asyncio.Future[AuthorizationCodeResult] | None = None
        self.server: asyncio.Server | None = None

    async def __aenter__(self) -> _CallbackReceiver:
        self.result = asyncio.get_running_loop().create_future()
        try:
            self.server = await asyncio.start_server(
                self._handle, "127.0.0.1", CALLBACK_PORT, limit=8192,
            )
        except OSError as exc:
            raise LittlebirdError(f"OAuth callback port {CALLBACK_PORT} is unavailable; close the other login and retry.") from exc
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.result and not self.result.done():
            self.result.cancel()

    def parse_callback(self, target: str, host: str) -> AuthorizationCodeResult:
        parsed = urlsplit(target)
        if host != f"127.0.0.1:{CALLBACK_PORT}" or parsed.path != "/callback" or parsed.netloc:
            raise LittlebirdError("Invalid OAuth callback address.")
        query = parse_qs(parsed.query, keep_blank_values=True)
        states = query.get("state", [])
        if (not self.expected_state or len(states) != 1 or not states[0].isascii()
                or not hmac.compare_digest(states[0], self.expected_state)):
            raise LittlebirdError("Invalid OAuth callback state.")
        if "error" in query:
            raise AuthenticationRequired("Littlebird authorization was declined; start login again.")
        codes = query.get("code", [])
        issuers = query.get("iss", [])
        if len(codes) != 1 or not codes[0] or len(issuers) > 1:
            raise LittlebirdError("Invalid OAuth callback parameters.")
        return AuthorizationCodeResult(code=codes[0], state=states[0], iss=issuers[0] if issuers else None)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status = "400 Bad Request"
        body = b"Invalid login callback. Return to the terminal."
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            if len(raw) > 8192:
                raise LittlebirdError("Callback request is too large.")
            lines = raw.decode("ascii").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            hosts = [line.split(":", 1)[1].strip() for line in lines[1:] if line.lower().startswith("host:")]
            if method != "GET" or len(hosts) != 1:
                raise LittlebirdError("Invalid callback request.")
            result = self.parse_callback(target, hosts[0])
            if self.result is not None and not self.result.done():
                self.result.set_result(result)
                status = "200 OK"
                body = b"Login received. You may close this tab and return to the terminal."
        except AuthenticationRequired as exc:
            if self.result is not None and not self.result.done():
                self.result.set_exception(exc)
            body = b"Authorization declined. Return to the terminal."
        except (LittlebirdError, ValueError, UnicodeError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError, TimeoutError):
            pass
        try:
            headers = (f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                       f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                       "Content-Security-Policy: default-src 'none'\r\nConnection: close\r\n\r\n")
            writer.write(headers.encode("ascii") + body)
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def redirect(self, url: str) -> None:
        parsed = urlsplit(url)
        states = parse_qs(parsed.query).get("state", [])
        if parsed.scheme != "https" or parsed.hostname != "mcp.littlebird.ai" or len(states) != 1:
            raise LittlebirdError("Refusing an unexpected Littlebird authorization address.")
        self.expected_state = states[0]
        if not await asyncio.to_thread(webbrowser.open, url, new=1):
            raise LittlebirdError("Could not open the browser; configure a default browser and retry login.")

    async def callback(self) -> AuthorizationCodeResult:
        if self.result is None:
            raise LittlebirdError("OAuth callback is not running.")
        try:
            return await asyncio.wait_for(self.result, CALLBACK_TIMEOUT)
        except TimeoutError as exc:
            raise AuthenticationRequired("Littlebird login timed out; start login again.") from exc


async def _noninteractive_redirect(_: str) -> None:
    raise AuthenticationRequired("Littlebird login is required; run the explicit Littlebird auth command.")


def _provider(storage: FileTokenStorage, callback: _CallbackReceiver | None = None) -> OAuthClientProvider:
    metadata = OAuthClientMetadata(
        client_name="AIK Session Search MCP",
        redirect_uris=[_CallbackReceiver.redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="littlebird:mcp offline_access",
        token_endpoint_auth_method="client_secret_post",
    )
    return OAuthClientProvider(
        server_url=SERVER_URL, client_metadata=metadata, storage=storage,
        redirect_handler=callback.redirect if callback else _noninteractive_redirect,
        callback_handler=callback.callback if callback else None,
    )


@asynccontextmanager
async def _connected_session(data_dir: Path, callback: _CallbackReceiver | None = None) -> AsyncIterator[ClientSession]:
    storage = FileTokenStorage(data_dir)
    token = os.environ.get("LITTLEBIRD_ACCESS_TOKEN") if callback is None else None
    headers: dict[str, str] = {}
    provider = None
    if token:
        if "\r" in token or "\n" in token:
            raise LittlebirdError("LITTLEBIRD_ACCESS_TOKEN contains invalid header characters.")
        headers["Authorization"] = f"Bearer {token}"
    else:
        if callback is None and await storage.get_tokens() is None:
            raise AuthenticationRequired("Littlebird login is required; run the explicit Littlebird auth command.")
        provider = _provider(storage, callback)
    async with httpx2.AsyncClient(
        auth=provider, headers=headers, timeout=httpx2.Timeout(30.0, read=60.0),
        follow_redirects=False, trust_env=False,
    ) as http:
        async with streamable_http_client(SERVER_URL, http_client=http) as streams:
            async with ClientSession(
                streams[0], streams[1],
                read_timeout_seconds=CALLBACK_TIMEOUT + 60.0 if callback else 60.0,
            ) as session:
                await session.initialize()
                yield session


def _field(value: Any, snake: str, camel: str | None = None, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(snake, value.get(camel, default) if camel else default)
    return getattr(value, snake, getattr(value, camel, default) if camel else default)


def select_ingest_tool(tools: list[Any]) -> Any:
    exact = [tool for tool in tools if _field(tool, "name") == "ingest"]
    matches = exact or [tool for tool in tools if str(_field(tool, "name", default="")).endswith("_ingest")]
    if len(matches) != 1:
        raise LittlebirdError("Littlebird must advertise exactly one compatible ingest tool.")
    tool = matches[0]
    schema = _field(tool, "input_schema", "inputSchema", {})
    if not isinstance(schema, dict):
        raise LittlebirdError("Littlebird ingest schema is incompatible with content-only text ingestion.")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if (schema.get("type") != "object" or not isinstance(properties, dict)
            or not isinstance(properties.get("content"), dict)
            or properties["content"].get("type") != "string"
            or not isinstance(required, list) or any(item != "content" for item in required)):
        raise LittlebirdError("Littlebird ingest schema is incompatible with content-only text ingestion.")
    return tool


async def _ingest_tool(session: ClientSession) -> Any:
    tools: list[Any] = []
    cursor = None
    seen: set[str] = set()
    for _ in range(100):
        page = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor) if cursor else None)
        tools.extend(page.tools)
        cursor = _field(page, "next_cursor", "nextCursor")
        if not cursor:
            return select_ingest_tool(tools)
        if cursor in seen:
            raise LittlebirdError("Littlebird tool discovery returned a repeated cursor.")
        seen.add(cursor)
    raise LittlebirdError("Littlebird tool discovery exceeded its page bound.")


async def authenticate(data_dir: Path) -> dict[str, Any]:
    """Explicit, interactive browser OAuth. Does not ingest any session content."""
    try:
        async with _CallbackReceiver() as callback:
            async with _connected_session(Path(data_dir), callback) as session:
                tool = await _ingest_tool(session)
        return {"authenticated": True, "server": SERVER_URL, "ingest_tool": _field(tool, "name")}
    except LittlebirdError as exc:
        return {"authenticated": False, "error": str(exc)}
    except Exception:
        return {"authenticated": False, "error": "Littlebird authentication failed; no session text was sent."}


@contextmanager
def _ingest_lock(data_dir: Path):
    data_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = data_dir / "littlebird-ingest.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise LittlebirdError("An ingestion lock already exists; verify no uploader is running before removing a stale lock.") from exc
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


async def send_batch(index: DeliveryIndex, documents: list[dict[str, Any]],
                     sender: Callable[[str], Awaitable[Any]]) -> dict[str, Any]:
    """Dispatch once per document, preserving unknown outcomes for manual review."""
    report: dict[str, Any] = {"sent": 0, "skipped": 0, "failed": 0, "uncertain": 0, "error": None}
    # Preflight the entire batch before sending any of it.
    for document in documents:
        if index.delivery_status(document["id"], document["content_hash"]) == "uncertain":
            report.update(uncertain=1, error="A prior delivery is uncertain; resolve its ledger entry before sending again.",
                          blocked_document_id=document["id"],
                          blocked_content_hash=document["content_hash"])
            return report
    for document in documents:
        identity, digest = document["id"], document["content_hash"]
        if index.delivered(identity, digest):
            report["skipped"] += 1
            continue
        # A crash after dispatch but before the success checkpoint remains uncertain.
        index.mark_delivery(identity, digest, "uncertain", "Delivery reserved; completion has not been acknowledged.")
        try:
            result = await sender(document["content"])
        except BaseException as exc:
            message = "Connection interrupted after dispatch; delivery may have succeeded. Resolve before resending."
            index.mark_delivery(identity, digest, "uncertain", message)
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            report.update(uncertain=1, error=message, blocked_document_id=identity,
                          blocked_content_hash=digest)
            return report
        if _field(result, "is_error", "isError", False):
            message = "Littlebird returned a tool error after dispatch; delivery may have succeeded. Resolve before resending."
            index.mark_delivery(identity, digest, "uncertain", message)
            report.update(uncertain=1, error=message, blocked_document_id=identity,
                          blocked_content_hash=digest)
            return report
        if _field(result, "result_type", "resultType", "complete") != "complete":
            message = "Littlebird did not return a completed ingestion result; resolve delivery before resending."
            index.mark_delivery(identity, digest, "uncertain", message)
            report.update(uncertain=1, error=message, blocked_document_id=identity,
                          blocked_content_hash=digest)
            return report
        if not isinstance(_field(result, "content"), list):
            message = "Littlebird returned no valid tool acknowledgement; resolve delivery before resending."
            index.mark_delivery(identity, digest, "uncertain", message)
            report.update(uncertain=1, error=message, blocked_document_id=identity,
                          blocked_content_hash=digest)
            return report
        index.mark_delivery(identity, digest, "sent")
        report["sent"] += 1
    return report


async def ingest(index: DeliveryIndex, *, data_dir: Path, source: str | None = None,
                 max_documents: int = 100, send: bool = False,
                 after: str | None = None) -> dict[str, Any]:
    """Preview a bounded export, or explicitly send it to Littlebird through MCP."""
    if isinstance(max_documents, bool) or not isinstance(max_documents, int) or not 1 <= max_documents <= 100_000:
        raise ValueError("max_documents must be an integer from 1 to 100000")
    report: dict[str, Any] = {
        "dry_run": not send, "documents": 0, "sent": 0, "skipped": 0,
        "failed": 0, "uncertain": 0, "error": None,
        "next_cursor": after, "done": False, "preview": [],
    }
    cursor = after
    remaining = max_documents
    try:
        with _ingest_lock(Path(data_dir)) if send else nullcontext():
            async with AsyncExitStack() as stack:
                session = None
                tool = None
                while remaining > 0:
                    batch = index.export_batch(source=source, after=cursor,
                                               limit=min(100, remaining), max_chars=MAX_DOCUMENT_CHARS)
                    documents = batch["documents"]
                    report["documents"] += len(documents)
                    remaining -= len(documents)
                    for doc in documents[:max(0, 3 - len(report["preview"]))]:
                        report["preview"].append({
                            "id": doc["id"], "session_id": doc["session_id"],
                            "characters": len(doc["content"]), "content_hash": doc["content_hash"],
                            "text": doc["content"][:MAX_PREVIEW_CHARS],
                        })
                    uncertain = next((doc for doc in documents if index.delivery_status(doc["id"], doc["content_hash"]) == "uncertain"), None)
                    if uncertain:
                        report.update(uncertain=1, error="A prior delivery is uncertain; resolve its ledger entry before sending again.",
                                      blocked_document_id=uncertain["id"],
                                      blocked_content_hash=uncertain["content_hash"],
                                      next_cursor=cursor, done=False)
                        return report
                    pending = [doc for doc in documents if not index.delivered(doc["id"], doc["content_hash"])]
                    if send and pending:
                        if session is None:
                            session = await stack.enter_async_context(_connected_session(Path(data_dir)))
                            tool = await _ingest_tool(session)
                        content_schema = _field(tool, "input_schema", "inputSchema")["properties"]["content"]
                        limit = content_schema.get("maxLength")
                        if isinstance(limit, int) and any(len(doc["content"]) > limit for doc in pending):
                            raise LittlebirdError("An exported document exceeds Littlebird's advertised content limit; export smaller chunks.")
                        async def sender(content: str) -> Any:
                            return await session.call_tool(_field(tool, "name"), {"content": content}, read_timeout_seconds=60.0)
                        progress = await send_batch(index, documents, sender)
                        for name in ("sent", "skipped", "failed", "uncertain"):
                            report[name] += progress[name]
                        if progress["error"]:
                            report.update(error=progress["error"], blocked_document_id=progress.get("blocked_document_id"),
                                          blocked_content_hash=progress.get("blocked_content_hash"),
                                          next_cursor=cursor, done=False)
                            return report
                    else:
                        report["skipped"] += len(documents) - len(pending)
                    next_cursor = batch.get("next_cursor")
                    done = bool(batch.get("done", False))
                    report.update(next_cursor=next_cursor, done=done)
                    if done:
                        return report
                    if not documents or next_cursor is None or next_cursor == cursor:
                        raise LittlebirdError("Local export made no progress; ingestion stopped.")
                    cursor = next_cursor
    except LittlebirdError as exc:
        report.update(error=str(exc), next_cursor=cursor, done=False)
    except Exception:
        report.update(error="Littlebird connection or discovery failed; review the delivery ledger before resuming.",
                      next_cursor=cursor, done=False)
    return report
