"""Claude.ai export: conversations.json (and projects.json) from Settings → Privacy → Export.

    [{uuid, name, created_at, updated_at,
      chat_messages: [{uuid, sender: human|assistant, created_at, text,
                       content: [{type, ...}], attachments: [{file_name, extracted_content}],
                       files: [{file_name}]}]}]

`content` blocks are preferred over `text`: text is kept, thinking is dropped, tool use
and results become short placeholders, anything unknown a placeholder plus a warning.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from portal.imports.model import (
    Attachment,
    Conversation,
    Message,
    Project,
    Role,
    clean_text,
    clean_title,
    parse_iso,
)

_ROLES: dict[str, Role] = {"human": "user", "user": "user", "assistant": "assistant"}
_SILENT = {"thinking", "redacted_thinking"}
_PLACEHOLDERS = {"image": "[image]", "document": "[document]"}
_PASTED = "Pasted text"


def looks_like_conversation(item: object) -> bool:
    return isinstance(item, dict) and "chat_messages" in item and "uuid" in item


def looks_like_project(item: object) -> bool:
    return (
        isinstance(item, dict)
        and "uuid" in item
        and "name" in item
        and "chat_messages" not in item
        and ("docs" in item or "prompt_template" in item)
    )


def _block_text(block: Any, warnings: Counter[str]) -> str | None:
    if not isinstance(block, dict):
        warnings["unreadable content block"] += 1
        return None
    kind = str(block.get("type", ""))
    if kind == "text":
        return str(block.get("text") or "")
    if kind in _SILENT:
        return None
    if kind == "tool_use":
        return f"[tool call: {str(block.get('name') or 'tool')[:60]}]"
    if kind == "tool_result":
        name = block.get("name")
        return f"[tool result: {str(name)[:60]}]" if name else "[tool result]"
    if kind in _PLACEHOLDERS:
        return _PLACEHOLDERS[kind]
    warnings[f"unknown Claude content type '{kind[:40]}'"] += 1
    return f"[{kind[:40] or 'content'}]"


def _text(msg: dict[str, Any], warnings: Counter[str]) -> str:
    blocks = msg.get("content")
    if isinstance(blocks, list) and blocks:
        parts = [t for b in blocks if (t := _block_text(b, warnings))]
        return "\n\n".join(p.strip("\n") for p in parts if p.strip())
    return str(msg.get("text") or "")


def _attachments(msg: dict[str, Any]) -> tuple[Attachment, ...]:
    """Named attachments and files. Pasted text arrives as an attachment without a name
    (plus an empty-named file entry, which carries nothing and is dropped)."""
    out: dict[str, Attachment] = {}
    pasted = 0
    for att in msg.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        raw = att.get("extracted_content")
        text = clean_text(raw) if isinstance(raw, str) and raw.strip() else None
        name = clean_text(str(att.get("file_name") or "")).strip()[:200]
        if name:
            out[name] = Attachment(name, text)
        elif text is not None:
            pasted += 1
            name = _PASTED if pasted == 1 else f"{_PASTED} {pasted}"
            out[name] = Attachment(name, text, pasted=True)
    for key in ("files", "files_v2"):
        for f in msg.get(key) or []:
            if isinstance(f, dict) and f.get("file_name"):
                name = clean_text(str(f["file_name"]))[:200]
                out.setdefault(name, Attachment(name))
    return tuple(out.values())


def parse_conversation(raw: dict[str, Any], warnings: Counter[str]) -> Conversation | None:
    """One conversation; None if it has no usable messages. Raises on malformed input."""
    cid = raw["uuid"]
    if not isinstance(cid, str) or not cid:
        raise ValueError("missing uuid")
    chat = raw["chat_messages"]
    if not isinstance(chat, list):
        raise TypeError("chat_messages is not a list")
    messages: list[Message] = []
    for msg in chat:
        if not isinstance(msg, dict):
            warnings["unreadable message skipped"] += 1
            continue
        role = _ROLES.get(str(msg.get("sender", "")))
        if role is None:
            warnings["message from an unknown sender skipped"] += 1
            continue
        text = clean_text(_text(msg, warnings)).strip()
        attachments = _attachments(msg)
        if not text and not attachments:
            continue
        messages.append(Message(role, text, parse_iso(msg.get("created_at")), attachments))
    if not messages:
        warnings["empty conversation skipped"] += 1
        return None
    created = parse_iso(raw.get("created_at")) or messages[0].created_at
    updated = parse_iso(raw.get("updated_at")) or messages[-1].created_at or created
    return Conversation(
        source="claude",
        id=cid,
        title=clean_title(raw.get("name"), tuple(messages)),
        created_at=created,
        updated_at=updated,
        messages=tuple(messages),
    )


def parse_project(raw: dict[str, Any]) -> Project:
    return Project(
        id=str(raw["uuid"]),
        name=clean_text(str(raw.get("name") or ""))[:200],
        description=clean_text(str(raw.get("description") or ""))[:2000],
    )
