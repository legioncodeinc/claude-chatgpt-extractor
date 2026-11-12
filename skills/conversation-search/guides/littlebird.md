# Littlebird

There are two separate ways to use this tool with Littlebird. Find out which one the user means before doing anything.

| | Live search | Bulk ingestion |
| --- | --- | --- |
| What happens | Littlebird calls the same MCP tools on demand | Conversation text is copied into Littlebird's context |
| Data that leaves the computer | Only the excerpts each tool call returns | Every selected conversation |
| Who starts it | Littlebird, when the user asks it something | The user, by running a command with `--send` |
| Reversible | Nothing is stored | No. This tool cannot delete what Littlebird received |

## Live search: connect the server to Littlebird

In the Littlebird Mac app: **Integrations**, **Connectors**, **Add custom**, **Add a local server instead**. Choose **Program**, select the `aik-sessions` executable, and give it the argument `serve`. Use **Test connection**, then **Add server**.

Littlebird needs the full path of the executable, which the user gets from:

```bash
command -v aik-sessions
```

If that prints nothing, the standalone command is not installed. The plugin's own copy runs through `uv` and is not on the PATH, so for Littlebird the user installs the command once, from a clone of the repository:

```bash
uv tool install .
```

Both copies share one data directory and one index, so nothing is indexed twice.

## Bulk ingestion: copy conversations into Littlebird

**Do not run a command containing `--send` unless the user has asked, in this conversation, for that specific upload.** It transmits the user's conversation text to a third-party service. A general request such as "set up Littlebird" is not that permission. Preview first, show the user what would go, and let them decide.

### Preview (local, sends nothing)

```bash
aik-sessions ingest --source codex --max-documents 100
```

The report shows how many documents would be sent and a short preview of the first three. No credentials are read and no connection is made.

### Sign in (the user does this)

```bash
aik-sessions littlebird-auth
```

This opens Littlebird's sign-in page in the browser. The user completes it themselves. Never ask for a token in chat.

### Send

```bash
aik-sessions ingest --source codex --max-documents 100 --send
```

If the report has `"done": false` and a `next_cursor`, continue with `--after <next_cursor>` and the same `--source`. Documents already confirmed as delivered are skipped, so restarting from the beginning is also safe.

### An "uncertain" delivery stops the run

If a document was sent but no confirmation came back, the run stops and reports a `blocked_document_id` and its `blocked_content_hash`. This is deliberate. Littlebird offers no way to make a resend safe, so the tool will not guess.

1. Ask the user to check in Littlebird whether that content arrived.
2. Record what they found, using the two values from the report:

```bash
aik-sessions resolve-delivery BLOCKED-DOCUMENT-ID BLOCKED-CONTENT-HASH --outcome sent --reviewed
```

Use `--outcome failed` if it did not arrive. Then run the same `ingest ... --send` command again.

### What to tell the user before they send

- Common secret formats are masked before indexing, but this is best effort. It does not remove personal information, names, or unusual secret formats.
- Choose a `--source` and a small `--max-documents` for the first run rather than sending everything.
- Sent text stays in Littlebird until they remove it there.
