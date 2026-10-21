"""Detect and parse an uploaded export.

Formats are recognised by structure, never by file name: any JSON member whose items
look like Claude.ai or ChatGPT conversations is parsed (newer exports split them over
several files), Claude project lists are recorded, and everything else in the archive
(chat.html, user.json, images, ...) is ignored. A malformed conversation is skipped
with a warning; it never fails the file.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from portal.imports import chatgpt, claude
from portal.imports.model import Conversation, ExportError, ParseResult
from portal.imports.safezip import DEFAULT_LIMITS, Limits, json_members

log = logging.getLogger(__name__)

Parser = Callable[[dict[str, Any], Counter[str]], Conversation | None]
_SAMPLE = 20
_EPOCH = dt.datetime.min.replace(tzinfo=dt.UTC)
_KINDS: dict[str, tuple[Callable[[object], bool], Parser | None]] = {
    "claude": (claude.looks_like_conversation, claude.parse_conversation),
    "chatgpt": (chatgpt.looks_like_conversation, chatgpt.parse_conversation),
    "claude-projects": (claude.looks_like_project, None),
}


def _items(data: Any) -> list[Any] | None:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("conversations"), list):
        return list(data["conversations"])
    return None


def classify(items: list[Any]) -> str | None:
    """The kind most of the first items look like, or None for unrelated JSON."""
    votes: Counter[str] = Counter()
    for item in items[:_SAMPLE]:
        for kind, (looks, _) in _KINDS.items():
            if looks(item):
                votes[kind] += 1
                break
    return votes.most_common(1)[0][0] if votes else None


def _parse_items(kind: str, items: list[Any], result: ParseResult) -> list[Conversation]:
    looks, parser = _KINDS[kind]
    if parser is None:  # Claude projects: context only
        result.projects += [claude.parse_project(p) for p in items if looks(p)]
        return []
    out: list[Conversation] = []
    for item in items:
        try:
            if not looks(item):
                raise TypeError("not a conversation object")
            conv = parser(item, result.warnings)
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as exc:
            result.warnings["malformed conversation skipped"] += 1
            log.info("skipped a malformed %s conversation (%s)", kind, type(exc).__name__)
            continue
        if conv is not None:
            out.append(conv)
    return out


def parse_export(path: Path, limits: Limits = DEFAULT_LIMITS) -> ParseResult:
    result = ParseResult()
    found: dict[str, Conversation] = {}
    for name, raw in json_members(path, result.warnings, limits):
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            result.warnings["unreadable JSON file skipped"] += 1
            continue
        del raw
        items = _items(data)
        kind = classify(items) if items else None
        if items is None or kind is None:
            continue
        log.info("export member %s: %d item(s) as %s", Path(name).name, len(items), kind)
        for conv in _parse_items(kind, items, result):
            result.sources.add(conv.source)
            known = found.get(conv.key)
            if known is not None:
                result.warnings["duplicate conversation: newest copy kept"] += 1
                if (known.updated_at or _EPOCH) >= (conv.updated_at or _EPOCH):
                    continue
            found[conv.key] = conv
    if not found:
        raise ExportError(
            "No Claude.ai or ChatGPT conversations were found in this file. Upload the zip "
            "you downloaded from the export email, or its conversations.json."
        )
    result.conversations = sorted(found.values(), key=lambda c: (c.created_at or _EPOCH, c.key))
    return result
