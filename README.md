# claude-chatgpt-extractor

Search every conversation you have had with Claude Code, Codex, Claude and ChatGPT from any MCP client, using a private index that stays on your computer.

<!-- TODO: readme-writing: add CI, release and license badges once a CI workflow and a tagged release exist. None are shown now because none would be live. -->

`aik-sessions` is a local MCP server and command-line tool. It builds a private full-text index of your AI conversations, then lets Littlebird, Claude Code, Codex or any other MCP client search and read them on demand. By Legion Code Inc.

## Quickstart

You need [uv](https://docs.astral.sh/uv/getting-started/installation/). It installs Python 3.14 for you if you do not have it.

```sh
git clone https://github.com/legioncodeinc/claude-chatgpt-extractor
cd claude-chatgpt-extractor
uv tool install .
aik-sessions index
aik-sessions search 'webhook forwarding'
```

`index` finds your Claude Code and Codex sessions and reports what it indexed. `search` prints ranked matches (trimmed here):

```json
{
  "results": [
    {
      "session_id": "codex:0199a2b4-7c1e-7d30-9f3e-2b6f8d1c4a55",
      "position": 47,
      "role": "assistant",
      "source": "codex",
      "project": "/Users/you/projects/billing",
      "timestamp": "2026-11-03T15:22:10+00:00",
      "snippet": "... the webhook forwarding rule now retries on 5xx ..."
    }
  ],
  "total": 1,
  "has_more": false
}
```

Read the conversation around a hit with `aik-sessions read 'codex:0199a2b4-...' --offset 44`.

## Features

- **Four sources, one index.** Claude Code and Codex sessions are found automatically. Claude and ChatGPT chats are added from the providers' own data exports.
- **Local and private.** One SQLite file with owner-only permissions. No embedding service, no hosted server, no account. Only the excerpts a tool call returns go to the client you connect.
- **Built for models to use.** Six MCP tools whose descriptions, parameters and error messages tell a calling model what to do next.
- **Search that composes with reading.** Every hit carries the message position, so a client jumps straight to the surrounding context.
- **Only what was said.** Visible user and assistant text is indexed. Tool calls, tool results, system prompts and reasoning are left out.
- **Secret masking.** Common API key, token, password and private-key formats are masked before they reach the index. This is best effort.
- **Safe re-imports.** A newer export updates a conversation. An older snapshot never overwrites newer history.
- **Optional Littlebird ingestion.** An explicit, resumable upload with a local delivery ledger. Nothing is sent unless you pass `--send`.

## Install

### Claude Code plugin

The plugin bundles the MCP server, a skill that teaches Claude how to use it, and a hook that opens a getting-started page once. The hook does nothing until a page address is set in `hooks/welcome-url.txt`, and it needs a POSIX `sh` (on Windows, Git Bash or WSL).

```text
/plugin marketplace add legioncodeinc/claude-chatgpt-extractor
/plugin install aik-sessions@legion-code
```

The server starts through `uv`, so `uv` must be on your PATH. The first start downloads dependencies and can take a minute. If the tools are missing after that first start, reconnect the server with `/mcp` or restart Claude Code.

### Littlebird

Install the command (see Quickstart), then in the Littlebird Mac app: **Integrations**, **Connectors**, **Add custom**, **Add a local server instead**. Choose **Program**, select the executable printed by `command -v aik-sessions`, add the argument `serve`, then **Test connection** and **Add server**.

A first request to try:

> Check the session index status, then search my past conversations for webhook forwarding. Read only the relevant pages and cite the session ID and date.

See [docs/littlebird.md](docs/littlebird.md) for copying conversations into Littlebird's own context.

### Other MCP clients

Any client that launches a stdio server works. Start from [examples/littlebird-local.json](examples/littlebird-local.json) for JSON `mcpServers` configuration, or [examples/codex.toml](examples/codex.toml) for Codex. Use the absolute path of the executable. This package never edits a client's configuration for you.

## Usage

### Add Claude and ChatGPT chats

Chats from the web and desktop apps come from each provider's data export ([ChatGPT](https://help.openai.com/en/articles/7260999-exporting-your-chatgpt-history-and-data), [Claude](https://support.claude.com/en/articles/9450526-how-can-i-export-my-claude-data)). Put the downloaded ZIP in the `imports` folder of the data directory and index again:

```sh
mkdir -p ~/.local/share/aik-session-mcp/imports && chmod 700 ~/.local/share/aik-session-mcp
mv ~/Downloads/your-export.zip ~/.local/share/aik-session-mcp/imports/
aik-sessions index
```

Or point at a file or folder for one run: `aik-sessions --import-path /absolute/path/export.zip index`. Nothing scans your home folder or Downloads on its own.

### Search

```sh
aik-sessions search '"payment failed"' --source codex --start-date 2026-11-01
aik-sessions search stripe --project billing --limit 5
```

Search is keyword based. Every word must appear in one message or in its conversation title, and double quotes match an exact phrase. There are no operators and no semantic matching.

### MCP tools

| Tool | What it does |
| --- | --- |
| `sessions_search` | Ranked keyword search with source, project and date filters |
| `sessions_read` | Read part of one conversation, with lossless continuation through long messages |
| `sessions_status` | Index size, refresh state and upload ledger counts |
| `sessions_discover` | List the files the server can see, without reading them |
| `sessions_refresh` | Index new or changed conversations. Never uploads |
| `sessions_export_batch` | Return attributed text in resumable batches. Never uploads |

Full schemas, outputs, errors and examples are in [docs/tools.md](docs/tools.md).

### Commands

`discover`, `index`, `status`, `search`, `read`, `export`, `serve`, `littlebird-auth`, `ingest` and `resolve-delivery`. Run `aik-sessions <command> --help` for flags. Global options go before the command.

## Configuration

| Option | Environment variable | Default | Purpose |
| --- | --- | --- | --- |
| `--data-dir` | `AIK_SESSIONS_DATA_DIR` | `~/.local/share/aik-session-mcp` (respects `XDG_DATA_HOME`), `%LOCALAPPDATA%\aik-session-mcp` on Windows | Index, import folder and upload state |
| `--claude-root` | `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code configuration root |
| `--codex-root` | `CODEX_HOME` | `~/.codex` | Codex configuration root |
| `--import-path` | | none | Export file or folder. Repeatable |
| `--home` | | your home folder | Where `.claude` and `.codex` are looked up |
| `serve --no-auto-index` | | off | Skip the background refresh at startup |
| `serve --transport http` | `AIK_SESSIONS_TOKEN` (required) | stdio | Loopback HTTP on `127.0.0.1` with a bearer token |
| | `LITTLEBIRD_ACCESS_TOKEN` | none | Use an existing token instead of browser sign-in |
| | `AIK_SESSIONS_NO_WELCOME=1` | unset | Stop the plugin's getting-started page from opening |

Limits, masking rules, what is excluded and the HTTP transport are documented in [docs/data-handling.md](docs/data-handling.md).

## Contributing

Issues and pull requests are welcome at [legioncodeinc/claude-chatgpt-extractor](https://github.com/legioncodeinc/claude-chatgpt-extractor). Before opening one, run:

```sh
uv run --no-editable python -m unittest discover -s tests
claude plugin validate . --strict
```

Tests use synthetic conversations only. After changing the version in `pyproject.toml`, update `src/aik_sessions/__init__.py` and `.claude-plugin/plugin.json` to match and run `uv lock`. A test fails if they disagree.

## License

Licensed under the GNU Affero General Public License v3.0. See [LICENSE.md](LICENSE.md).
