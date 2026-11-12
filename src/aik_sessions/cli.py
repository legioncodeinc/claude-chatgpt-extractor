"""Installable command line and MCP entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from aik_sessions import __version__
from aik_sessions.index import SessionIndex
from aik_sessions.models import SOURCES
from aik_sessions.service import SessionService, Settings, default_data_dir, read_page


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="aik-sessions", description=(
        "Local Claude/Codex/chat-export search and selected Littlebird ingestion."))
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--data-dir", type=Path, default=default_data_dir())
    root.add_argument("--home", type=Path, default=Path.home(),
                      help="Home to discover .claude and .codex under.")
    root.add_argument("--import-path", type=Path, action="append", default=[],
                      help="An export JSON/ZIP file or a dedicated export folder. Repeatable.")
    root.add_argument("--claude-root", type=Path,
                      help="Claude configuration root; defaults to CLAUDE_CONFIG_DIR or ~/.claude.")
    root.add_argument("--codex-root", type=Path,
                      help="Codex configuration root; defaults to CODEX_HOME or ~/.codex.")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("discover", help="List source locations and counts without reading messages.")
    commands.add_parser("status", help="Read index and delivery counts.")
    index = commands.add_parser("index", help="Index visible chats locally; never upload.")
    index.add_argument("--force", action="store_true", help="Reparse unchanged source files too.")
    search = commands.add_parser("search", help="Ranked local full-text search.")
    search.add_argument("query")
    search.add_argument("--source", choices=SOURCES)
    search.add_argument("--project")
    search.add_argument("--start-date", help="Inclusive YYYY-MM-DD.")
    search.add_argument("--end-date", help="Inclusive YYYY-MM-DD.")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--offset", type=int, default=0)
    read = commands.add_parser("read", help="Read a bounded page of a session.")
    read.add_argument("session_id")
    read.add_argument("--offset", type=int, default=0)
    read.add_argument("--limit", type=int, default=30)
    read.add_argument("--text-offset", type=int, default=0)
    read.add_argument("--max-chars", type=int, default=20000)
    export = commands.add_parser("export", help="Emit a resumable text batch as JSON, with no upload.")
    export.add_argument("--source", choices=SOURCES)
    export.add_argument("--after")
    export.add_argument("--limit", type=int, default=20)
    export.add_argument("--max-chars", type=int, default=20000)
    run = commands.add_parser("serve", help="Start MCP; stdio by default.")
    run.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    run.add_argument("--port", type=int, default=8765)
    run.add_argument("--no-auto-index", action="store_true",
                     help="Do not refresh sources in the background at startup.")
    commands.add_parser("littlebird-auth", help="Explicitly start Littlebird browser OAuth.")
    ingest = commands.add_parser("ingest", help="Preview or send selected indexed text to Littlebird.")
    ingest.add_argument("--source", choices=SOURCES)
    ingest.add_argument("--after")
    ingest.add_argument("--max-documents", type=int, default=100)
    ingest.add_argument("--send", action="store_true",
                        help="Actually transfer text to Littlebird. Omit for a local dry run.")
    resolve = commands.add_parser("resolve-delivery", help=(
        "Record the result of manually reviewing an uncertain Littlebird delivery."))
    resolve.add_argument("document_id")
    resolve.add_argument("content_hash")
    resolve.add_argument("--outcome", choices=["sent", "failed"], required=True)
    resolve.add_argument("--reviewed", action="store_true", required=True,
                         help="Confirm you checked whether Littlebird received this document.")
    return root


def _settings(args: argparse.Namespace) -> Settings:
    claude = args.claude_root
    codex = args.codex_root
    if claude is None and os.environ.get("CLAUDE_CONFIG_DIR"):
        claude = Path(os.environ["CLAUDE_CONFIG_DIR"])
    if codex is None and os.environ.get("CODEX_HOME"):
        codex = Path(os.environ["CODEX_HOME"])
    return Settings(data_dir=args.data_dir.expanduser().resolve(),
                    home=args.home.expanduser().resolve(),
                    import_paths=[p.expanduser().resolve() for p in args.import_path],
                    claude_root=claude.expanduser().resolve() if claude is not None else None,
                    codex_root=codex.expanduser().resolve() if codex is not None else None)


def run(args: argparse.Namespace) -> dict[str, Any] | None:
    settings = _settings(args)
    service = SessionService(settings)
    if args.command == "discover":
        return service.discovery()
    if args.command == "index":
        return service.index_sources(force=args.force)
    if args.command == "serve":
        from aik_sessions.server import serve
        serve(settings, transport=args.transport, port=args.port,
              auto_index=not args.no_auto_index)
        return None
    if args.command == "littlebird-auth":
        from aik_sessions.littlebird import authenticate
        return asyncio.run(authenticate(settings.data_dir))
    index = SessionIndex(settings.database)
    try:
        if args.command == "status":
            return {"stats": index.stats(), "ingestion": index.ingestion_status()}
        if args.command == "search":
            from aik_sessions.server import _dates
            _dates(args.start_date, args.end_date)
            return index.search(args.query, source=args.source, project=args.project,
                                start_date=args.start_date, end_date=args.end_date,
                                limit=args.limit, offset=args.offset)
        if args.command == "read":
            return read_page(index, args.session_id, offset=args.offset, limit=args.limit,
                             text_offset=args.text_offset, max_chars=args.max_chars)
        if args.command == "export":
            return index.export_batch(source=args.source, after=args.after, limit=args.limit,
                                      max_chars=args.max_chars)
        if args.command == "ingest":
            if not 1 <= args.max_documents <= 100000:
                raise ValueError("max-documents must be between 1 and 100000")
            from aik_sessions.littlebird import ingest
            return asyncio.run(ingest(index, data_dir=settings.data_dir, source=args.source,
                                     max_documents=args.max_documents, send=args.send,
                                     after=args.after))
        if args.command == "resolve-delivery":
            if index.delivery_status(args.document_id, args.content_hash) != "uncertain":
                raise ValueError("Only an uncertain delivery can be resolved with this command")
            index.mark_delivery(args.document_id, args.content_hash, args.outcome,
                                error="Manually reviewed by the local operator.")
            return {"document_id": args.document_id, "status": args.outcome}
        raise ValueError("Unsupported command")
    finally:
        index.close()


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    args = parser().parse_args()
    try:
        result = run(args)
        if result is not None:
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            if result.get("error") or result.get("failed"):
                raise SystemExit(1)
    except (OSError, ValueError, RuntimeError) as exc:
        # Error text is local metadata or a controlled message, not a transcript fragment.
        sys.stderr.write(f"aik-sessions: {exc}\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
