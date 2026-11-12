# Littlebird

There are two separate ways to use this tool with Littlebird.

| | Live search | Bulk ingestion |
| --- | --- | --- |
| What happens | Littlebird calls the MCP tools on demand | Conversation text is copied into Littlebird's context |
| What leaves your computer | The excerpts each tool call returns | Every document you select |
| Started by | Asking Littlebird a question | Running `ingest --send` yourself |

A live search returns selected excerpts to the calling client. It does not populate Littlebird's long-term context by itself.

## Live search

The Littlebird Mac app was inspected on November 11, 2026. It offers:

1. **Integrations**, **Connectors**, **Add custom**.
2. **Add a local server instead**.
3. **Program**, **Python**, **Node** and **Local URL** runtime choices, plus **Paste config**.
4. **Test connection**, then **Add server**.

Select **Program** and the installed `aik-sessions` executable with the argument `serve`, or adapt [examples/littlebird-local.json](../examples/littlebird-local.json) using the absolute executable path. If a desktop app's PATH differs from your terminal's, use the path printed by `command -v aik-sessions` (`where aik-sessions` on Windows).

The server uses stdio. It starts quickly, then refreshes the index in the background. Ask for `sessions_status` to see progress. No public server or tunnel is needed. Littlebird documents custom connectors under [Integrations](https://docs.littlebird.ai/docs/integrations/#bring-your-own-mcp-server).

Test the connection after installing. Inspecting the form does not establish that a server was registered.

## Bulk ingestion

This transfers conversation text to Littlebird, where it may become context for replies and routines. Littlebird's [MCP documentation](https://docs.littlebird.ai/docs/mcp/#adding-context-from-another-app) describes that behaviour. The upload uses Littlebird's official MCP endpoint at `https://mcp.littlebird.ai/mcp`.

### 1. Preview

The default is a local dry run. It makes no connection, reads no credentials and starts no sign-in.

```sh
aik-sessions ingest --source codex --max-documents 100
```

### 2. Sign in

```sh
aik-sessions littlebird-auth
```

This opens Littlebird's browser sign-in and listens for the result on a loopback port for up to five minutes. OAuth state is stored owner-only in the data directory. A locally supplied `LITTLEBIRD_ACCESS_TOKEN` also works. Never paste a token into a chat or commit it.

### 3. Send

```sh
aik-sessions ingest --source codex --max-documents 100 --send
```

Documents are bounded text chunks. Each carries its source, session and message IDs and original timestamps in the text. `--max-documents` caps a run. Continue with the returned `next_cursor`:

```sh
aik-sessions ingest --source codex --max-documents 100 --send --after NEXT-CURSOR
```

A local content-hash ledger skips documents already confirmed as delivered. If the index changes between runs the old cursor is rejected. Start again from the beginning and let the ledger skip what was already sent.

### Uncertain deliveries

Littlebird's ingestion schema accepts text only. It offers no external ID, no upsert and no documented exactly-once guarantee. A request that was dispatched but has no affirmative receipt is marked **uncertain**, and the run stops. It is never silently resent.

The report names the document (other fields omitted here):

```json
{
  "uncertain": 1,
  "error": "Connection interrupted after dispatch; delivery may have succeeded. Resolve before resending.",
  "blocked_document_id": "chunk:9f2c...",
  "blocked_content_hash": "3b0d..."
}
```

Check in Littlebird whether that content arrived, then record what you found using those two values:

```sh
aik-sessions resolve-delivery 'chunk:9f2c...' '3b0d...' --outcome sent --reviewed
```

Use `--outcome failed` if it did not arrive, then run the upload again. This updates the local ledger only. It does not delete or resend anything remotely.

### Before you send

- Masking covers common secret formats only. It does not remove personal information.
- Start with one `--source` and a small `--max-documents`.
- This tool cannot remove text from Littlebird once it is sent.
