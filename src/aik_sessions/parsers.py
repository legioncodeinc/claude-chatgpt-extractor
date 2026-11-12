"""Normalize visible conversation text from coding logs and chat exports.

Source files are never changed. JSONL is streamed, ZIP members stay in memory,
and all diagnostic messages exclude conversation content.
"""

from __future__ import annotations

from collections.abc import Iterator
from compression import zstd
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import stat
from typing import BinaryIO
import zipfile

from .models import DiscoveredFile, Message, ParseResult, Session, Source

MAX_EXPORT_BYTES = 256 * 1024 * 1024
MAX_JSONL_BYTES = 512 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 16 * 1024 * 1024
MAX_NATIVE_JSONL_BYTES = 4 * 1024 * 1024 * 1024
MAX_NATIVE_JSONL_LINE_BYTES = 64 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ZIP_MEMBERS = 10_000
MAX_MESSAGES = 100_000
_UUID = re.compile(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}")


@dataclass
class _Budget:
    remaining: int

    def consume(self, size: int, label: str, result: ParseResult) -> bool:
        if size > self.remaining:
            self.remaining = 0
            result.warnings.append(f"ZIP expanded size limit exceeded: {label}")
            return False
        self.remaining -= size
        return True


def parse_file(file: DiscoveredFile) -> ParseResult:
    """Read one discovered file; failures become content-free warnings."""
    result = ParseResult()
    path = Path(file.path)
    if path.is_symlink():
        result.warnings.append(f"Skipped symlinked input: {path}")
        return result
    try:
        if path.suffix.lower() == ".zip":
            _parse_zip(path, file, result)
        else:
            with path.open("rb") as stream:
                _parse_stream(stream, str(path), path.name, file.source, result)
    except (OSError, ValueError, UnicodeError, NotImplementedError, RecursionError,
            zstd.ZstdError, zipfile.BadZipFile) as error:
        result.warnings.append(f"Could not read {path} ({type(error).__name__}).")
    for session in result.sessions:
        session.metadata["archived"] = file.archived
    return result


def _parse_zip(path: Path, file: DiscoveredFile, result: ParseResult) -> None:
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > MAX_ZIP_MEMBERS:
            result.warnings.append(f"ZIP member limit exceeded: {path}")
            return
        total = 0
        budget = _Budget(MAX_ZIP_TOTAL_BYTES)
        skipped = 0
        for member in members:
            if member.is_dir():
                continue
            name = member.filename
            normalized = PurePosixPath(name.replace("\\", "/"))
            if not normalized.parts or normalized.is_absolute() or ".." in normalized.parts or ":" in normalized.parts[0]:
                result.warnings.append(f"Skipped unsafe ZIP member name in {path}.")
                continue
            if stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1:
                result.warnings.append(f"Skipped symlinked or encrypted ZIP member in {path}.")
                continue
            if not name.lower().endswith((".json", ".jsonl", ".jsonl.zst")):
                skipped += 1
                continue
            if member.file_size > MAX_EXPORT_BYTES:
                result.warnings.append(f"ZIP member size limit exceeded: {path}!/{name}")
                continue
            total += member.file_size
            if total > MAX_ZIP_TOTAL_BYTES:
                result.warnings.append(f"ZIP expanded size limit exceeded: {path}")
                break
            try:
                with archive.open(member) as stream:
                    _parse_stream(stream, f"{path}!/{name}", name, file.source, result, budget)
            except (OSError, ValueError, UnicodeError, NotImplementedError, RecursionError,
                    zstd.ZstdError, zipfile.BadZipFile) as error:
                result.warnings.append(
                    f"Could not read ZIP member {path}!/{name} ({type(error).__name__})."
                )
            if budget.remaining == 0:
                break
        if skipped:
            result.warnings.append(f"Skipped {skipped} attachment or unsupported ZIP members: {path}")


def _parse_stream(
    stream: BinaryIO,
    label: str,
    name: str,
    source: Source | None,
    result: ParseResult,
    budget: _Budget | None = None,
) -> None:
    lowered = name.lower()
    native = source in ("claude-code", "codex") and budget is None
    max_total = MAX_NATIVE_JSONL_BYTES if native else MAX_JSONL_BYTES
    max_line = MAX_NATIVE_JSONL_LINE_BYTES if native else MAX_JSONL_LINE_BYTES
    if lowered.endswith(".jsonl.zst"):
        with zstd.ZstdFile(stream, "rb") as decompressed:
            records = _records(decompressed, label, result, budget, max_total=max_total, max_line=max_line)
            _parse_jsonl(records, label, source, result)
    elif lowered.endswith(".jsonl"):
        records = _records(stream, label, result, budget, max_total=max_total, max_line=max_line)
        _parse_jsonl(records, label, source, result)
    elif lowered.endswith(".json"):
        limit = min(MAX_EXPORT_BYTES + 1, budget.remaining + 1) if budget else MAX_EXPORT_BYTES + 1
        raw = stream.read(limit)
        if budget is not None and not budget.consume(len(raw), label, result):
            return
        if len(raw) > MAX_EXPORT_BYTES:
            result.warnings.append(f"JSON export size limit exceeded: {label}")
            return
        try:
            document: object = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            # RecursionError: pathologically nested JSON. Skip the file, keep indexing.
            result.warnings.append(f"Invalid JSON export: {label}")
            return
        _parse_export(document, label, result)
    else:
        result.warnings.append(f"Unsupported input file: {label}")


def _records(
    stream: BinaryIO,
    label: str,
    result: ParseResult,
    budget: _Budget | None = None,
    *,
    max_total: int | None = None,
    max_line: int | None = None,
) -> Iterator[dict[str, object]]:
    max_total = MAX_JSONL_BYTES if max_total is None else max_total
    max_line = MAX_JSONL_LINE_BYTES if max_line is None else max_line
    total = 0
    line_number = 0
    diagnostic_count = 0

    def warn(message: str) -> None:
        nonlocal diagnostic_count
        if diagnostic_count < 1000:
            result.warnings.append(message)
        elif diagnostic_count == 1000:
            result.warnings.append(f"Further malformed-record warnings suppressed: {label}")
        diagnostic_count += 1

    while True:
        limit = min(max_line + 1, budget.remaining + 1) if budget else max_line + 1
        raw = stream.readline(limit)
        if not raw:
            return
        if budget is not None and not budget.consume(len(raw), label, result):
            return
        line_number += 1
        total += len(raw)
        if total > max_total:
            result.warnings.append(f"JSONL expanded size limit exceeded: {label}")
            return
        if len(raw) > max_line:
            while raw and not raw.endswith(b"\n"):
                limit = min(max_line + 1, budget.remaining + 1) if budget else max_line + 1
                raw = stream.readline(limit)
                if budget is not None and not budget.consume(len(raw), label, result):
                    return
                total += len(raw)
                if total > max_total:
                    result.warnings.append(f"JSONL expanded size limit exceeded: {label}")
                    return
            warn(f"Skipped oversized JSONL record at line {line_number}: {label}")
            continue
        if not raw.strip():
            continue
        try:
            value: object = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            kind = "Incomplete final JSONL record" if not raw.endswith(b"\n") else "Invalid JSONL record"
            warn(f"{kind} at line {line_number}: {label}")
            continue
        if isinstance(value, dict):
            yield value
        else:
            warn(f"Skipped non-object JSONL record at line {line_number}: {label}")


def _parse_jsonl(
    records: Iterator[dict[str, object]],
    label: str,
    source: Source | None,
    result: ParseResult,
) -> None:
    messages: list[Message] = []
    legacy_messages: list[Message] = []
    seen: set[str] = set()
    metadata: dict[str, str | int | bool | None] = {}
    native_id: str | None = None
    project: str | None = None
    title: str | None = None
    started_at: str | None = None
    own_meta_found = False
    missing_ordinal_warned = False
    ordinal_boundary: int | None = None
    filename_id = _UUID.search(Path(label).name)
    filename_native = filename_id.group(0) if filename_id else None
    found_records = False

    for row in records:
        found_records = True
        kind = row.get("type")
        if source is None:
            if kind == "session_meta" or kind in ("response_item", "event_msg"):
                source = "codex"
            elif "sessionId" in row or kind in ("user", "assistant") and "message" in row:
                source = "claude-code"
            elif "mapping" in row or "chat_messages" in row:
                _parse_export(row, label, result)
                continue
        if source == "codex":
            payload = _mapping(row.get("payload"))
            if kind == "session_meta":
                candidate = _string(payload.get("id")) or _string(payload.get("session_id"))
                is_own = candidate == filename_native if filename_native else not own_meta_found
                if is_own:
                    native_id = candidate
                    own_meta_found = True
                    project = _string(payload.get("cwd")) or project
                    started_at = _timestamp(payload.get("timestamp")) or _timestamp(row.get("timestamp"))
                    for key in ("parent_thread_id", "forked_from_id", "agent_path", "agent_role", "agent_nickname"):
                        if isinstance(payload.get(key), str):
                            metadata[key] = str(payload[key])
                    boundary = payload.get("subagent_history_start_ordinal")
                    if isinstance(boundary, int) and not isinstance(boundary, bool):
                        ordinal_boundary = boundary
                        metadata["subagent_history_start_ordinal"] = boundary
                continue
            if ordinal_boundary is not None:
                ordinal = row.get("ordinal")
                if not isinstance(ordinal, int) or isinstance(ordinal, bool):
                    if not missing_ordinal_warned:
                        result.warnings.append(f"Excluded child-session records missing an ordinal: {label}")
                        missing_ordinal_warned = True
                    continue
                if isinstance(ordinal, int) and ordinal < ordinal_boundary:
                    continue
            role = payload.get("role")
            if kind == "response_item" and payload.get("type") == "message":
                if role not in ("user", "assistant") or payload.get("phase") == "analysis" or payload.get("channel") == "analysis":
                    continue
                text = _text_blocks(payload.get("content"), allowed=("input_text", "output_text", "text"))
                _append(messages, seen, source, native_id or filename_native, payload.get("id"), role, text, row.get("timestamp"))
            elif kind == "event_msg" and payload.get("type") in ("user_message", "agent_message"):
                legacy_role = "user" if payload.get("type") == "user_message" else "assistant"
                _append(legacy_messages, set(), source, native_id or filename_native, payload.get("id"), legacy_role, _string(payload.get("message")) or "", row.get("timestamp"))
        elif source == "claude-code":
            native_id = native_id or _string(row.get("sessionId")) or filename_native
            project = project or _string(row.get("cwd"))
            if isinstance(row.get("agentId"), str):
                metadata["agent_id"] = str(row["agentId"])
                metadata["parent_session_id"] = _string(row.get("sessionId"))
            elif Path(label).name.startswith("agent-"):
                metadata["agent_id"] = Path(label).name.removeprefix("agent-").removesuffix(".jsonl")
                metadata["parent_session_id"] = _string(row.get("sessionId"))
            if kind == "summary":
                title = title or _string(row.get("summary"))
            if kind not in ("user", "assistant") or row.get("isCompactSummary") or row.get("isMeta"):
                continue
            message = _mapping(row.get("message"))
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _text_blocks(message.get("content"), allowed=("text",))
            message_id = _string(row.get("uuid")) or _string(message.get("id"))
            _append(messages, seen, source, native_id, message_id, role, text, row.get("timestamp"))
        if len(messages) + len(legacy_messages) >= MAX_MESSAGES:
            result.warnings.append(f"Message limit reached: {label}")
            break

    if source == "codex" and not messages:
        messages = list({message.id: message for message in legacy_messages}.values())
    if source not in ("claude-code", "codex"):
        if found_records:
            result.warnings.append(f"No supported coding-session records found: {label}")
        return
    if not messages:
        return
    # Claude child files share their parent's sessionId; the filename separates them.
    if source == "claude-code" and "agent_id" in metadata:
        native_id = f"{native_id or 'unknown'}/agent/{metadata['agent_id']}"
    if source == "codex":
        native_id = filename_native or native_id
    _finish_session(source, native_id, title, label, project, started_at, messages, metadata, result)


def _parse_export(document: object, label: str, result: ParseResult) -> None:
    if isinstance(document, list):
        conversations = document
    elif isinstance(document, dict) and isinstance(document.get("conversations"), list):
        conversations = document["conversations"]
    elif isinstance(document, dict):
        conversations = [document]
    else:
        result.warnings.append(f"Unsupported export structure: {label}")
        return
    recognized = 0
    for raw in conversations:
        if not isinstance(raw, dict):
            continue
        if isinstance(raw.get("mapping"), dict):
            _chatgpt_export(raw, label, result)
            recognized += 1
        elif isinstance(raw.get("chat_messages"), list):
            _claude_export(raw, label, result)
            recognized += 1
    if conversations and not recognized:
        result.warnings.append(f"No supported conversations found in export: {label}")


def _chatgpt_export(raw: dict[str, object], label: str, result: ParseResult) -> None:
    mapping = _mapping(raw.get("mapping"))
    node = _string(raw.get("current_node"))
    if node not in mapping:
        parents = {
            parent for item in mapping.values()
            if (parent := _string(_mapping(item).get("parent")))
        }
        leaves = [key for key in mapping if key not in parents]
        if not leaves:
            result.warnings.append(f"No usable conversation branch: {label}")
            return
        def leaf_time(key: str) -> tuple[str, str]:
            message = _mapping(_mapping(mapping.get(key)).get("message"))
            return (_timestamp(message.get("create_time")) or "", key)
        node = max(leaves, key=leaf_time)
        result.warnings.append(f"Missing current_node; selected latest export branch: {label}")
    branch: list[dict[str, object]] = []
    visited: set[str] = set()
    while node is not None:
        if node in visited:
            result.warnings.append(f"Cycle in ChatGPT conversation branch: {label}")
            return
        visited.add(node)
        entry = _mapping(mapping.get(node))
        if not entry:
            result.warnings.append(f"Broken ChatGPT conversation branch: {label}")
            return
        branch.append(entry)
        node = _string(entry.get("parent"))
    native_id = _string(raw.get("id")) or _string(raw.get("conversation_id"))
    messages: list[Message] = []
    seen: set[str] = set()
    for entry in reversed(branch):
        if len(messages) >= MAX_MESSAGES:
            result.warnings.append(f"Message limit reached: {label}")
            break
        message = _mapping(entry.get("message"))
        author = _mapping(message.get("author"))
        metadata = _mapping(message.get("metadata"))
        if metadata.get("is_visually_hidden_from_conversation") or message.get("channel") == "analysis":
            continue
        role = author.get("role")
        if role not in ("user", "assistant"):
            continue
        content = _mapping(message.get("content"))
        parts = content.get("parts")
        text = "\n".join(item for item in parts if isinstance(item, str)) if isinstance(parts, list) else ""
        _append(messages, seen, "chatgpt-export", native_id, message.get("id") or entry.get("id"), role, text, message.get("create_time"))
    if messages:
        _finish_session("chatgpt-export", native_id, _string(raw.get("title")), label, None, _timestamp(raw.get("create_time")), messages, {"current_node": _string(raw.get("current_node"))}, result, _timestamp(raw.get("update_time")))


def _claude_export(raw: dict[str, object], label: str, result: ParseResult) -> None:
    native_id = _string(raw.get("uuid")) or _string(raw.get("id"))
    messages: list[Message] = []
    seen: set[str] = set()
    rows = raw.get("chat_messages")
    if not isinstance(rows, list):
        return
    for value in rows:
        if len(messages) >= MAX_MESSAGES:
            result.warnings.append(f"Message limit reached: {label}")
            break
        row = _mapping(value)
        sender = row.get("sender")
        role = "user" if sender in ("human", "user") else "assistant" if sender == "assistant" else None
        if role is None:
            continue
        text = _string(row.get("text")) or _text_blocks(row.get("content"), allowed=("text",))
        _append(messages, seen, "claude-export", native_id, row.get("uuid") or row.get("id"), role, text, row.get("created_at"))
    if messages:
        _finish_session("claude-export", native_id, _string(raw.get("name")) or _string(raw.get("title")), label, None, _timestamp(raw.get("created_at")), messages, {}, result, _timestamp(raw.get("updated_at")))


def _finish_session(
    source: Source,
    native_id: str | None,
    title: str | None,
    path: str,
    project: str | None,
    started_at: str | None,
    messages: list[Message],
    metadata: dict[str, str | int | bool | None],
    result: ParseResult,
    updated_at: str | None = None,
) -> None:
    if not native_id:
        native_id = "sha256-" + _digest(source, [(m.role, m.text, m.timestamp) for m in messages])
    metadata["native_id"] = native_id
    prefix = f"{source}:"
    session_id = native_id if native_id.startswith(prefix) else prefix + native_id
    times = [m.timestamp for m in messages if m.timestamp]
    result.sessions.append(Session(
        id=session_id,
        source=source,
        title=(title or next((m.text.strip().splitlines()[0] for m in messages if m.role == "user"), "Untitled conversation"))[:160],
        path=path,
        project=project,
        started_at=started_at or (min(times) if times else None),
        updated_at=updated_at or (max(times) if times else started_at),
        messages=messages,
        metadata=metadata,
    ))


def _append(
    messages: list[Message],
    seen: set[str],
    source: str,
    session_id: str | None,
    native_id: object,
    role: object,
    text: str,
    timestamp: object,
) -> None:
    if not text.strip() or role not in ("user", "assistant"):
        return
    identifier = _string(native_id) or "sha256-" + _digest(source, session_id, role, text, timestamp, len(messages))
    if identifier in seen:
        return
    seen.add(identifier)
    messages.append(Message(id=identifier, role=role, text=text, timestamp=_timestamp(timestamp)))


def _text_blocks(value: object, *, allowed: tuple[str, ...]) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "\n".join(
        str(item["text"]) for item in value
        if isinstance(item, dict) and item.get("type") in allowed and isinstance(item.get("text"), str)
    )


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _timestamp(value: object) -> str | None:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        if isinstance(value, str) and value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _digest(*values: object) -> str:
    return sha256(json.dumps(values, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
