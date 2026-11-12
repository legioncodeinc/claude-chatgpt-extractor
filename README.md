# claude-chatgpt-extractor

Search Claude Code, Codex, Claude chat exports, and ChatGPT chat exports from a local MCP server. Connect the server to Littlebird for live searches, or explicitly copy selected indexed conversations into Littlebird's context.

By Legion Code Inc. This package is independent of the AIK.dev application runtime.

## Sources

| Source | Discovery and import |
| --- | --- |
| Claude Code | `~/.claude/projects/**/*.jsonl`, including nested subagents |
| Codex CLI and desktop | `~/.codex/sessions/` and `~/.codex/archived_sessions/`, including `.jsonl.zst` |
| Regular Claude web/desktop chats | Claude conversation JSON exports, individually or inside an export ZIP |
| Regular ChatGPT web/desktop chats | ChatGPT conversation JSON exports, individually or inside an export ZIP |

Coding clients provide local transcripts. Ordinary browser and desktop chat histories use the providers' exports; this package does not scrape browser cookies, passwords, app databases, or cloud accounts. An export must be downloaded before its conversations can be indexed. See the official [ChatGPT export instructions](https://help.openai.com/en/articles/7260999-exporting-your-chatgpt-history-and-data) and [Claude export instructions](https://support.claude.com/en/articles/9450526-how-can-i-export-my-claude-data).

Default discovery respects `CLAUDE_CONFIG_DIR` and `CODEX_HOME`. You can override both roots with command-line flags. Explicit import folders are searched recursively. Nothing searches your entire home directory or Downloads automatically.

## Install locally

Requires Python **3.14 or later** for standard-library Zstandard decompression. The package pins the official MCP Python SDK to the tested **2.3.0** version.

From the repository root:

```sh
uv tool install .
aik-sessions --version
```

This installs the command and its declared dependencies into an isolated local tool environment. It does not register a client or upload conversations. If a desktop application's PATH differs from your terminal's, use the full executable path reported by `command -v aik-sessions` (or `where aik-sessions` on Windows).

## First local search

```sh
aik-sessions discover
aik-sessions index
aik-sessions search 'webhook forwarding'
aik-sessions search '"payment failed"' --source codex --start-date 2026-11-01
aik-sessions read 'codex:SESSION-ID'
```

Search uses SQLite FTS5 ranking, literal terms combined with AND, and quoted phrases. It is lexical search; no embedding service is called. Results include original message dates, source, project, session ID, and bounded excerpts.

The default data directory is `~/.local/share/aik-session-mcp` on macOS/Linux, respecting `XDG_DATA_HOME`; Windows uses `%LOCALAPPDATA%/aik-session-mcp`. `AIK_SESSIONS_DATA_DIR` or `--data-dir` can select another dedicated private directory. New state directories and files use private permissions. Existing permissive or symlinked database paths are rejected rather than silently changed.

### Import browser and desktop chat exports

Pass export files or a dedicated export folder explicitly:

```sh
aik-sessions --import-path /absolute/path/chatgpt-export.zip index
aik-sessions --import-path /absolute/path/claude-conversations.json index
aik-sessions --import-path /absolute/path/exports index
```

Alternatively, put exports in the `imports` subdirectory of your data directory. Files remain where you put them. ZIP members are read in place without extracting files to disk.

Global options such as `--data-dir`, `--home`, `--claude-root`, `--codex-root`, and `--import-path` go **before** the command. Overrides are configuration roots such as `.claude` or `.codex`, not their nested `projects` or `sessions` folders.

Indexing is incremental by file size/mtime; `index --force` reparses unchanged files. Reimported sessions deduplicate by source and native session ID. When duplicate snapshots come from different paths, older snapshots cannot overwrite newer history. Equal-date or undated alternatives must be a strict extension of the existing messages; ambiguous alternatives are skipped with warnings. Same-file updates still reflect truncation or rotation. Source deletion does not automatically delete the local index. Run `sessions_refresh` after new chats arrive, or restart the server to refresh in the background.

## Connect to Littlebird

The installed Littlebird Mac app was inspected on November 11, 2026. It exposes:

1. **Integrations → Connectors → Add custom**.
2. **Add a local server instead**.
3. **Program**, **Python**, **Node**, and **Local URL** runtime choices, plus **Paste config**.
4. **Test connection**, then **Add server**.

Select **Program** and the installed `aik-sessions` executable, with argument `serve`, or adapt [the local configuration example](examples/littlebird-local.json) using its absolute executable path. The JSON `mcpServers` shape is also usable by local clients that accept that format. The actual Littlebird connection must be tested after installation; inspecting the form does not establish that a server was registered.

The server uses stdio by default. It starts quickly, then refreshes the index in the background. Ask for `sessions_status` to see progress and any failure, and search after the initial index is ready. There is no public server or tunnel required for this local connection.

Suggested first request in Littlebird:

> Use the AIK session tools to check indexing status, then search my Claude and Codex conversations for webhook forwarding. Read only the relevant message pages and cite their original dates and session IDs.

Littlebird documents custom MCP connectors in [Integrations](https://docs.littlebird.ai/docs/integrations/#bring-your-own-mcp-server). A live search returns selected local excerpts to the calling client; it does not populate Littlebird's long-term context by itself.

### Other local MCP clients

Claude Desktop/Claude Code and other JSON-configured clients can use the `mcpServers` example above. For Codex, use its TOML format, shown in [examples/codex.toml](examples/codex.toml). Examples are supplied only; this package never edits your client configuration automatically.

### Optional loopback HTTP

For a client that needs a local URL, set a long random `AIK_SESSIONS_TOKEN` privately and run:

```sh
aik-sessions serve --transport http --port 8765
```

Connect to `http://127.0.0.1:8765/mcp` and supply `Authorization: Bearer <your token>`. Every request must authenticate. The token must contain at least 32 characters and no whitespace. The listener binds only `127.0.0.1`; the SDK also checks Host/Origin to prevent DNS rebinding. This is a private static-token interface, not a public OAuth deployment. No public exposure, tunnel or OAuth authorization server is configured.

## Bulk ingestion into Littlebird

This is separate from live search. It transfers conversation text to Littlebird, where it may become context for replies and routines. Littlebird's [official MCP documentation](https://docs.littlebird.ai/docs/mcp/#adding-context-from-another-app) describes that behavior.

Preview a selected source first. The default is a local dry run and makes no connection to Littlebird:

```sh
aik-sessions ingest --source codex --max-documents 100
```

For a real transfer, first authenticate explicitly through Littlebird's browser OAuth flow:

```sh
aik-sessions littlebird-auth
aik-sessions ingest --source codex --max-documents 100 --send
```

The login command opens a bounded callback listener on loopback and stores OAuth state privately. A locally supplied `LITTLEBIRD_ACCESS_TOKEN` is also supported; never paste a token into chat or commit it. The ingestion command uses Littlebird's official MCP endpoint at `https://mcp.littlebird.ai/mcp`, not an invented REST endpoint. Dry runs do not read credentials or start authentication.

Documents are bounded text chunks, with source, session/message IDs and historical timestamps carried in the text. `--max-documents` caps a run; use its returned `next_cursor` with `--after` to continue. The uploader streams bounded pages instead of loading an entire export into memory. A local content-hash ledger skips documents already confirmed delivered.

Littlebird's exposed ingestion schema accepts text only. It supplies no external-ID/upsert contract or documented exactly-once guarantee. A request that was dispatched but lacks an affirmative receipt is marked **uncertain**, and the uploader stops. It will not silently resend that document. Check Littlebird, then record what you found:

```sh
aik-sessions resolve-delivery DOCUMENT-ID CONTENT-HASH --outcome sent --reviewed
```

If you verified it was not received, use `--outcome failed` instead, then resume. This review is a local ledger update, not a remote deletion or retry. An index revision invalidates an old export cursor; restart from the beginning and let confirmed delivery hashes deduplicate the replay.

## Tools

| Tool | Behavior |
| --- | --- |
| `sessions_discover` | Read configured source locations and file counts, capped file metadata list |
| `sessions_status` | Read index counts, refresh progress, and delivery status |
| `sessions_refresh` | Refresh the local index without uploading |
| `sessions_search` | Search ranked excerpts, optionally filtered by source, project and dates |
| `sessions_read` | Read a bounded page, including lossless continuation within long messages |
| `sessions_export_batch` | Return bounded, attributed text and a resume cursor; no upload |

Search/read/export tools cannot select arbitrary filesystem paths. The process's startup options select roots. Source and message content are untrusted historical data, not instructions for the client to follow.

## Data handling and limits

- Only visible human/user and assistant text is indexed. System/developer instructions, reasoning blocks, tool calls/results, binary attachments, and replay request blobs are excluded.
- ChatGPT exports follow the active `current_node` ancestry. A missing pointer selects the latest leaf with a warning. Broken or cyclic ancestry fails closed.
- Claude subagents and Codex subagents retain separate identity and parent provenance. Codex inherited prefixes are excluded when an explicit boundary is available.
- Incomplete final JSONL lines are skipped with warnings and revisited after the source changes.
- Common API-token, bearer-token, credential-assignment, database-URI and private-key patterns are masked before indexing. This is best-effort secret masking, not a guarantee that all secrets or personal information are removed. Review the selected material before upload.
- SQLite is local and protected by filesystem permissions, not encrypted by this package. Search results are transmitted to whichever MCP client invoked the tool.
- Bounds: 50,000 discovered files; depth 32; 256 MiB per JSON export; 100,000 messages per session; 10,000 ZIP members and 512 MiB actual expanded ZIP content. Native Claude Code/Codex files stream up to 4 GiB expanded per file and 64 MiB per record; generic and ZIP JSONL retain the stricter 512 MiB/file and 16 MiB/record bounds. Oversized/unsupported inputs produce warnings. Files with parse warnings are rechecked on refresh. Search excerpts, reads and export responses are bounded separately.
- No provider cloud synchronization, scheduled background uploader, account creation, hosted server, or billing operation is included.

## Development checks

After installing the declared runtime dependencies into your chosen environment:

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src
```

Tests use synthetic temporary conversations. They cover parsers, compressed/ZIP imports, duplicate handling, filters, bounded continuation, secret masking, delivery checkpoints, real SDK stdio/subprocess calls, and authenticated in-process HTTP requests. Live Littlebird login, real ingestion, and cloud search retrieval are separate integration checks.

Protocol implementation uses the official [MCP Python SDK 2.3](https://github.com/modelcontextprotocol/python-sdk/tree/v2.3.0), including its typed tool schemas and transports.
