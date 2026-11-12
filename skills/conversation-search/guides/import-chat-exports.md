# Import ChatGPT and Claude chat exports

Claude Code and Codex sessions are found automatically. Chats from the ChatGPT and Claude web or desktop apps are not on disk in a readable form, so the user has to request an export from the provider first. This tool never reads browser data, cookies, app databases or cloud accounts.

## 1. The user downloads an export

This is a step for the user, in their own account settings. The provider prepares the export and sends a download link, which is not instant. Point them at the provider's current instructions rather than describing menus from memory:

- [Export your ChatGPT history](https://help.openai.com/en/articles/7260999-exporting-your-chatgpt-history-and-data)
- [Export your Claude data](https://support.claude.com/en/articles/9450526-how-can-i-export-my-claude-data)

The download is a ZIP file. Leave it zipped: the tool reads the conversation files inside it without extracting anything to disk. A single conversations JSON file also works.

## 2. Put the export where the server looks

The server indexes anything in the `imports` folder inside its data directory. On macOS and Linux:

```bash
mkdir -p ~/.local/share/aik-session-mcp/imports && chmod 700 ~/.local/share/aik-session-mcp
```

```bash
mv ~/Downloads/your-export.zip ~/.local/share/aik-session-mcp/imports/
```

The `chmod 700` matters. The tool refuses to use a data directory that other accounts on the machine can read.

On Windows the folder is `%LOCALAPPDATA%\aik-session-mcp\imports`. If `XDG_DATA_HOME` or `AIK_SESSIONS_DATA_DIR` is set, the data directory is there instead.

Ask before moving files for the user, and name the file you are about to move.

## 3. Refresh and confirm

1. Call `sessions_refresh`.
2. Call `sessions_status` and check that `stats.by_source` now lists `chatgpt-export` or `claude-export`.
3. Report the session count, and any `warnings` from the refresh.

## Things to know

- **Re-importing is safe.** A newer export of the same account updates existing conversations instead of duplicating them. An older snapshot never overwrites newer history.
- **ChatGPT branches.** When a ChatGPT conversation was edited and regenerated, only the branch that was active at export time is indexed.
- **Attachments are skipped.** Images and files inside the ZIP are ignored, and the refresh reports how many members it skipped. That warning is expected.
- **Size limits.** A single JSON file over 256 MiB, or a ZIP that expands past 512 MiB of conversation data, is skipped with a warning. Split very large exports.
- **Removing an export file does not remove its conversations** from the index.
- **A different folder.** Someone who runs the server from their own configuration can pass `--import-path /absolute/path` before the command instead of using the `imports` folder. The MCP tools cannot change which paths are read.
