"""The normalised chat model both export formats are parsed into."""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

Source = Literal["claude", "chatgpt"]
Role = Literal["user", "assistant"]

TITLE_MAX = 80
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE = re.compile(r"\s+")
# Where the owner can open the original conversation again.
_LINKS: dict[str, str] = {
    "claude": "https://claude.ai/chat/{id}",
    "chatgpt": "https://chatgpt.com/c/{id}",
}
SOURCE_LABELS: dict[str, str] = {"claude": "Claude.ai", "chatgpt": "ChatGPT"}


class ExportError(ValueError):
    """The upload cannot be read as an export; the message is shown to the owner."""


def clean_text(text: str) -> str:
    """Normalise newlines and drop control characters (the wiki store rejects NUL)."""
    return _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


@dataclass(frozen=True)
class Attachment:
    name: str
    text: str | None = None  # extracted text, when the export carries it
    pasted: bool = False  # text the owner pasted into the chat, not an uploaded file


@dataclass(frozen=True)
class Message:
    role: Role
    text: str
    created_at: dt.datetime | None = None
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True)
class Conversation:
    source: Source
    id: str
    title: str
    created_at: dt.datetime | None
    updated_at: dt.datetime | None
    messages: tuple[Message, ...]

    @property
    def key(self) -> str:
        """Dedupe key and the raw source's `resource`, e.g. claude-chat:<uuid>."""
        return f"{self.source}-chat:{self.id}"

    @property
    def url(self) -> str:
        return _LINKS[self.source].format(id=self.id)

    @property
    def char_count(self) -> int:
        return sum(
            len(m.text) + sum(len(a.text or "") for a in m.attachments) for m in self.messages
        )

    @property
    def exchanges(self) -> int:
        """Number of user turns."""
        return sum(1 for m in self.messages if m.role == "user")


@dataclass(frozen=True)
class Project:
    """A Claude.ai project: recorded as optional context, its docs are not imported."""

    id: str
    name: str
    description: str = ""


@dataclass
class ParseResult:
    conversations: list[Conversation] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)
    # Counts per kind of problem (never content): shown to the owner.
    warnings: Counter[str] = field(default_factory=Counter)
    sources: set[str] = field(default_factory=set)

    @property
    def format(self) -> str:
        if len(self.sources) > 1:
            return "mixed"
        return next(iter(self.sources), "unknown")

    @property
    def date_range(self) -> tuple[dt.datetime, dt.datetime] | None:
        dates = [c.created_at for c in self.conversations if c.created_at]
        return (min(dates), max(dates)) if dates else None


def derive_title(messages: tuple[Message, ...] | list[Message]) -> str:
    """A title from the first user message, for conversations exported without one."""
    first = next((m.text for m in messages if m.role == "user" and m.text.strip()), "")
    line = _SPACE.sub(" ", first.strip().split("\n", 1)[0]).strip()
    if not line:
        return "Untitled conversation"
    if len(line) <= TITLE_MAX:
        return line
    cut = line[:TITLE_MAX].rsplit(" ", 1)[0] or line[:TITLE_MAX]
    return cut.rstrip(" ,.;:") + "…"


def clean_title(title: object, messages: tuple[Message, ...]) -> str:
    text = _SPACE.sub(" ", clean_text(title)).strip() if isinstance(title, str) else ""
    return text[:200] if text else derive_title(messages)


def parse_iso(value: object) -> dt.datetime | None:
    """Claude's ISO timestamps; naive values are taken as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def parse_epoch(value: object) -> dt.datetime | None:
    """ChatGPT's float seconds since the epoch."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return dt.datetime.fromtimestamp(float(value), dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None
