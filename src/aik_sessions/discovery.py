"""Discover local coding transcripts and explicitly supplied export files."""

from __future__ import annotations

import os
from pathlib import Path

from .models import DiscoveredFile, Discovery, Source

MAX_DISCOVERED_FILES = 50_000
MAX_DIRECTORY_DEPTH = 32


def discover(
    *,
    home: Path,
    import_paths: list[Path],
    claude_root: Path | None = None,
    codex_root: Path | None = None,
) -> Discovery:
    """Read known session roots and explicit imports without following symlinks.

    Overrides are configuration roots, not the nested ``projects`` or
    ``sessions`` directories. No browser databases or Downloads are scanned.
    """
    result = Discovery()
    seen: set[Path] = set()

    def add(path: Path, source: Source | None, archived: bool) -> None:
        if len(result.files) >= MAX_DISCOVERED_FILES:
            return
        try:
            if path.is_symlink() or not path.is_file():
                return
            resolved = path.resolve()
            if resolved in seen:
                return
            size = path.stat().st_size
        except OSError:
            result.warnings.append(f"Could not inspect file: {path}")
            return
        seen.add(resolved)
        result.files.append(
            DiscoveredFile(path=str(resolved), source=source, archived=archived, size=size)
        )

    def walk(root: Path, source: Source | None, archived: bool, *, explicit: bool) -> None:
        if root.is_symlink():
            result.warnings.append(f"Skipped symlinked discovery root: {root}")
            return
        if not root.exists():
            if explicit:
                result.warnings.append(f"Import path does not exist: {root}")
            return
        if root.is_file():
            if _supported(root.name, exports=explicit):
                add(root, source, archived)
            elif explicit:
                result.warnings.append(f"Unsupported import file: {root}")
            return

        def on_error(error: OSError) -> None:
            result.warnings.append(f"Could not scan directory: {error.filename or root}")

        for directory, dirs, filenames in os.walk(root, followlinks=False, onerror=on_error):
            current = Path(directory)
            depth = len(current.relative_to(root).parts)
            dirs[:] = sorted(
                name for name in dirs if not (current / name).is_symlink()
            )
            if depth >= MAX_DIRECTORY_DEPTH:
                if dirs:
                    result.warnings.append(f"Discovery depth limit reached: {current}")
                dirs.clear()
            for filename in sorted(filenames):
                if _supported(filename, exports=explicit):
                    add(current / filename, source, archived)
            if len(result.files) >= MAX_DISCOVERED_FILES:
                result.warnings.append(
                    f"Discovery stopped at {MAX_DISCOVERED_FILES} files; narrow the roots."
                )
                return

    claude_config = (claude_root or home / ".claude").expanduser()
    codex_config = (codex_root or home / ".codex").expanduser()
    walk(claude_config / "projects", "claude-code", False, explicit=False)
    walk(codex_config / "sessions", "codex", False, explicit=False)
    walk(codex_config / "archived_sessions", "codex", True, explicit=False)
    for import_path in import_paths:
        walk(import_path.expanduser(), None, False, explicit=True)
    result.files.sort(key=lambda item: item.path)
    return result


def _supported(name: str, *, exports: bool) -> bool:
    lowered = name.lower()
    return lowered.endswith((".jsonl", ".jsonl.zst")) or (
        exports and lowered.endswith((".json", ".zip"))
    )
