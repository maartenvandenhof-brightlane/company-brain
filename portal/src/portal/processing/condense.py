"""Condense an imported transcript into what an extraction call needs to read: wiki-mcp's
(wiki_mcp.condense), shared with the observe tools for assistants so that every model reads a
transcript condensed, chunked and numbered the same way. Batch processing, observing chats
and ideas use it from here."""

from __future__ import annotations

from wiki_mcp.condense import (
    ANSWERED_CHARS,
    ASSISTANT_CHARS,
    CHUNK_CHARS,
    SHORT_REPLY,
    Transcript,
    condense,
    parse,
)

__all__ = [
    "ANSWERED_CHARS",
    "ASSISTANT_CHARS",
    "CHUNK_CHARS",
    "SHORT_REPLY",
    "Transcript",
    "condense",
    "parse",
]
