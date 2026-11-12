"""Discovery and indexing orchestration. Sources are always opened read-only."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aik_sessions.discovery import discover
from aik_sessions.index import SessionIndex
from aik_sessions.parsers import parse_file
from aik_sessions.privacy import redact_session

_PARSER_REVISION = 2
_MAX_WARNING_CHARS = 300


def _safe_warning(text: str) -> str:
    """Bound a diagnostic and drop control characters.

    Warnings can quote file names and session IDs, which come from the files being
    indexed. They are shown to a calling model, so keep them short and single-line.
    """
    cleaned = "".join(character if character.isprintable() else " " for character in text)
    if len(cleaned) > _MAX_WARNING_CHARS:
        cleaned = cleaned[:_MAX_WARNING_CHARS] + " [truncated]"
    return cleaned


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    home: Path
    import_paths: list[Path] = field(default_factory=list)
    claude_root: Path | None = None
    codex_root: Path | None = None

    @property
    def database(self) -> Path:
        return self.data_dir / "sessions.sqlite3"

    @property
    def imports(self) -> list[Path]:
        default = self.data_dir / "imports"
        return ([default] if default.exists() else []) + self.import_paths


def default_data_dir() -> Path:
    if os.environ.get("AIK_SESSIONS_DATA_DIR"):
        return Path(os.environ["AIK_SESSIONS_DATA_DIR"]).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "aik-session-mcp"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "aik-session-mcp"


def read_page(index: SessionIndex, session_id: str, *, offset: int = 0, limit: int = 30,
              text_offset: int = 0, max_chars: int = 20000) -> dict[str, Any]:
    """Bound returned text while preserving a continuation through oversized messages."""
    if not 1000 <= max_chars <= 50000 or text_offset < 0:
        raise ValueError("max_chars must be 1000..50000 and text_offset must be nonnegative")
    page = index.read(session_id, offset=offset, limit=limit)
    messages = []
    remaining = max_chars
    next_offset = offset
    next_text_offset = 0
    for number, message in enumerate(page["messages"]):
        start = text_offset if number == 0 else 0
        text = message["text"]
        if start > len(text):
            raise ValueError("text_offset exceeds the first message's text length")
        end = min(len(text), start + remaining)
        messages.append({**message, "text": text[start:end], "text_start": start,
                         "text_end": end, "text_total": len(text),
                         "text_truncated": end < len(text)})
        remaining -= end - start
        if end < len(text):
            next_offset = offset + number
            next_text_offset = end
            break
        next_offset = offset + number + 1
        if remaining <= 0:
            break
    if text_offset and not page["messages"]:
        raise ValueError("text_offset needs an existing message at this offset")
    return {**page, "messages": messages, "next_offset": next_offset,
            "next_text_offset": next_text_offset,
            "done": next_offset >= page["total"] and next_text_offset == 0}


class SessionService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._progress: dict[str, Any] = {"state": "never_run", "running": False,
                                          "indexed_files": 0}

    def discovery(self) -> dict[str, Any]:
        result = discover(home=self.settings.home, import_paths=self.settings.imports,
                          claude_root=self.settings.claude_root,
                          codex_root=self.settings.codex_root)
        counts: dict[str, int] = {}
        for file in result.files:
            key = file.source or "export"
            counts[key] = counts.get(key, 0) + 1
        return {"file_count": len(result.files), "by_source": counts,
                "files": [f.model_dump() for f in result.files[:100]],
                "files_truncated": len(result.files) > 100, "warnings": result.warnings}

    def progress(self) -> dict[str, Any]:
        return dict(self._progress)

    def _manifest(self) -> dict[str, list[int]]:
        try:
            data = json.loads((self.settings.data_dir / "manifest.json").read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_manifest(self, manifest: dict[str, list[int]]) -> None:
        directory = self.settings.data_dir
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="manifest-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False)
            os.replace(name, directory / "manifest.json")
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def index_sources(self, *, force: bool = False) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {"state": "running", "running": True,
                    "message": "An index refresh is already running. Check sessions_status "
                               "for progress instead of starting another."}
        index: SessionIndex | None = None
        manifest: dict[str, list[int]] | None = None
        try:
            self._progress = {"state": "running", "running": True, "indexed_files": 0,
                              "skipped_files": 0, "ignored_older": 0, "ignored_ambiguous": 0}
            found = discover(home=self.settings.home, import_paths=self.settings.imports,
                             claude_root=self.settings.claude_root,
                             codex_root=self.settings.codex_root)
            manifest = self._manifest()
            index = SessionIndex(self.settings.database)
            if index.stats()["sessions"] == 0:
                # The manifest says which files are already indexed. If the index is
                # empty (deleted, recreated, first run) that record is stale; trusting
                # it would skip every file and leave the index empty for good.
                manifest = {}
            warnings = [_safe_warning(item) for item in found.warnings]
            session_count = 0
            self._progress["total_files"] = len(found.files)
            for item in found.files:
                try:
                    stat = Path(item.path).stat()
                    signature = [stat.st_size, stat.st_mtime_ns, _PARSER_REVISION]
                    if not force and manifest.get(item.path) == signature:
                        self._progress["skipped_files"] += 1
                        continue
                    parsed = parse_file(item)
                    warnings.extend(_safe_warning(item) for item in parsed.warnings)
                    for session in parsed.sessions:
                        outcome = index.upsert(redact_session(session))
                        status = outcome.get("status")
                        if status in ("ignored_older", "ignored_ambiguous"):
                            self._progress[status] += 1
                            warnings.append(_safe_warning(
                                f"{status}: retained indexed history for {session.id[:120]!r}"))
                        session_count += 1
                    if not parsed.warnings:
                        manifest[item.path] = signature
                    else:
                        # A warning is not a clean cache hit. Recheck on the next refresh
                        # so unresolved partial/corrupt records never disappear from status.
                        manifest.pop(item.path, None)
                    self._progress["indexed_files"] += 1
                except sqlite3.Error:
                    # The index itself is unavailable; continuing would fail every file.
                    raise
                except Exception as exc:
                    # Any other failure belongs to this one file. Record it and move on:
                    # a single malformed file must never stop the rest being indexed.
                    manifest.pop(item.path, None)
                    warnings.append(_safe_warning(
                        f"Could not index {item.path}: {type(exc).__name__}"))
            self._save_manifest(manifest)
            result = {**self._progress, "state": "complete", "running": False,
                      "sessions_processed": session_count,
                      "warnings": warnings[:100], "warning_count": len(warnings),
                      "stats": index.stats()}
            self._progress = result
            return result
        except Exception as exc:
            # Keep the files that did finish, so an interrupted first index of a large
            # archive resumes instead of starting over. Only the exception type is
            # recorded: messages could carry paths or transcript fragments.
            if manifest is not None:
                try:
                    self._save_manifest(manifest)
                except OSError:
                    pass
            self._progress = {**self._progress, "state": "failed", "running": False,
                              "failed": True, "error": type(exc).__name__}
            raise
        finally:
            if index is not None:
                index.close()
            self._lock.release()

    def start_background_index(self) -> None:
        def run() -> None:
            try:
                self.index_sources()
            except Exception as exc:
                # Tool status exposes failure; never log transcript fragments or credentials.
                self._progress = {**self._progress, "state": "failed", "running": False,
                                  "failed": True, "error": type(exc).__name__}
        threading.Thread(target=run, name="aik-session-index", daemon=True).start()
