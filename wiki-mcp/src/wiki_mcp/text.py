"""Text helpers for what a model reads."""

from __future__ import annotations


def shorten(text: str, limit: int) -> str:
    """Beginning and end of `text` within about `limit` characters."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return f"{text[:head].rstrip()}\n[… {len(text) - limit} characters left out …]\n{text[-tail:].lstrip()}"
