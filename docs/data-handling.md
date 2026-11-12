# Data handling, limits and transports

## What is read

| Source | Location |
| --- | --- |
| Claude Code | `~/.claude/projects/**/*.jsonl`, including nested subagents |
| Codex CLI and desktop | `~/.codex/sessions/` and `~/.codex/archived_sessions/`, including `.jsonl.zst` |
| Claude web and desktop chats | Claude conversation JSON exports, individually or inside an export ZIP |
| ChatGPT web and desktop chats | ChatGPT conversation JSON exports, individually or inside an export ZIP |

Coding clients keep local transcripts. Ordinary browser and desktop chat histories come from the providers' exports. This package does not read browser cookies, passwords, app databases or cloud accounts.

Discovery respects `CLAUDE_CONFIG_DIR` and `CODEX_HOME`, and both roots can be overridden with flags. Overrides are configuration roots such as `.claude` or `.codex`, not their nested `projects` or `sessions` folders. Explicit import folders are searched recursively. Symlinked roots, folders and files are skipped. Nothing searches your whole home folder or Downloads.

## What is indexed

- Only visible user and assistant text. System and developer instructions, reasoning blocks, tool calls and results, binary attachments and replay request blobs are excluded.
- ChatGPT exports follow the active `current_node` ancestry. A missing pointer selects the latest leaf with a warning. Broken or cyclic ancestry fails closed.
- Claude Code subagents and Codex subagents keep a separate identity and their parent's provenance. Codex inherited prefixes are excluded when an explicit boundary is available.
- An incomplete final JSONL line is skipped with a warning and revisited after the file changes.

## Secret masking

Common API-token, bearer-token, credential-assignment, database-URI and private-key patterns are replaced with `[REDACTED]` before indexing. This is best-effort masking. It is not a guarantee that every secret or any personal information is removed. Review material before uploading it anywhere.

## Storage

The index is one SQLite file, `sessions.sqlite3`, in the data directory. It is protected by filesystem permissions and is not encrypted by this package. New directories and files are created owner-only. An existing data directory or database that other accounts can read, or a database path that is a symlink, is rejected with a message naming the `chmod` command to run. It is never silently changed.

Search results are sent to whichever MCP client called the tool. That is the only way conversation text leaves the index during search.

## Indexing behaviour

- Indexing is incremental by file size and modification time. `index --force` reparses unchanged files.
- Sessions deduplicate by source and native session ID. When duplicate snapshots come from different paths, an older snapshot cannot overwrite newer history. Equal-date or undated alternatives must be a strict extension of the existing messages. Ambiguous alternatives are skipped with a warning. Updates to the same file still reflect truncation or rotation.
- Deleting a source file does not delete its conversations from the index.
- The server refreshes in the background when it starts. Use `sessions_refresh` or `aik-sessions index` after new chats arrive.
- If a refresh is interrupted, files that finished are remembered and the next refresh continues from there.
- Several server processes can share one data directory. Each client that launches the server gets its own process over the same index.

## Limits

| Limit | Value |
| --- | --- |
| Discovered files | 50,000 |
| Folder depth | 32 |
| One JSON export | 256 MiB |
| Messages per session | 100,000 |
| ZIP members | 10,000 |
| Expanded ZIP content | 512 MiB |
| Native Claude Code or Codex file | 4 GiB expanded, 64 MiB per record |
| Other JSONL, including inside a ZIP | 512 MiB per file, 16 MiB per record |

Oversized or unsupported inputs produce warnings instead of failing the run. Files with parse warnings are rechecked on the next refresh. Search excerpts, reads and export responses are bounded separately.

## Search

Search uses SQLite FTS5 ranking. Terms are literal and combined with AND, and each term may match a message's text or its conversation title. Double quotes form a phrase. Raw FTS syntax is never accepted, so query text cannot inject operators. No embedding service is called.

## Optional loopback HTTP

For a client that needs a local URL instead of a stdio process, set a long random `AIK_SESSIONS_TOKEN` privately and run:

```sh
aik-sessions serve --transport http --port 8765
```

Connect to `http://127.0.0.1:8765/mcp` and send `Authorization: Bearer <your token>`. Every request must authenticate. The token must be at least 32 characters with no whitespace, and the server refuses to start without one. The listener binds only to `127.0.0.1`, and the SDK also checks Host and Origin to prevent DNS rebinding.

This is a private static-token interface. No public exposure, tunnel or OAuth authorization server is configured.

## Not included

No provider cloud synchronization, scheduled background uploader, account creation, hosted server or billing operation.

## Requirements

Python 3.14 or later, for standard-library Zstandard decompression. The package pins the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v2.3.0) to the tested 2.3.0 version and uses its typed tool schemas and transports.
