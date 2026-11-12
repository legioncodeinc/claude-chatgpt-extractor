"""Normalized visible conversation records shared by parsers and the local index."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Source = Literal["claude-code", "codex", "claude-export", "chatgpt-export"]
SOURCES: tuple[Source, ...] = (
    "claude-code", "codex", "claude-export", "chatgpt-export",
)


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    role: Literal["user", "assistant"]
    text: str
    timestamp: str | None = None


class Session(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    source: Source
    title: str
    path: str
    project: str | None = None
    started_at: str | None = None
    updated_at: str | None = None
    messages: list[Message] = Field(default_factory=list)
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)


class DiscoveredFile(BaseModel):
    path: str
    source: Source | None = None
    archived: bool = False
    size: int = 0


class Discovery(BaseModel):
    files: list[DiscoveredFile] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ParseResult(BaseModel):
    sessions: list[Session] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
