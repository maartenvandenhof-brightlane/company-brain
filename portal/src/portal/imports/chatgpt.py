"""ChatGPT export: conversations.json (Settings → Data controls → Export).

    [{id | conversation_id, title, create_time, update_time, current_node,
      mapping: {node_id: {message, parent, children}}}]

A conversation is a tree (every edit or regeneration starts a branch). Only the
active branch is kept: from `current_node` up through `parent`, reversed. System and
tool messages, hidden messages, reasoning and custom instructions are dropped.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from portal.imports.model import (
    Attachment,
    Conversation,
    Message,
    Role,
    clean_text,
    clean_title,
    parse_epoch,
)

_ROLES: dict[str, Role] = {"user": "user", "assistant": "assistant"}
_SILENT = {
    "thoughts",
    "reasoning_recap",
    "user_editable_context",
    "model_editable_context",
}
_BACKTICKS = re.compile(r"`{3,}")


def looks_like_conversation(item: object) -> bool:
    return isinstance(item, dict) and isinstance(item.get("mapping"), dict)


def fenced(code: str, language: str = "") -> str:
    """A fence longer than any backtick run inside, so the block cannot be broken out of."""
    longest = max((len(m) for m in _BACKTICKS.findall(code)), default=2)
    fence = "`" * max(3, longest + 1)
    lang = re.sub(r"[^A-Za-z0-9_+.-]", "", language)[:20]
    return f"{fence}{lang}\n{code.strip(chr(10))}\n{fence}"


def _multimodal(parts: list[Any], warnings: Counter[str]) -> str:
    out: list[str] = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict):
            kind = str(part.get("content_type", ""))
            if kind == "audio_transcription":
                out.append(str(part.get("text") or ""))
            elif "audio" in kind:
                out.append("[audio]")
            elif "image" in kind or "asset_pointer" in part:
                out.append("[image]")
            else:
                warnings[f"unknown ChatGPT content part '{kind[:40]}'"] += 1
                out.append(f"[{kind[:40] or 'attachment'}]")
    return "\n\n".join(p for p in out if p.strip())


def _content_text(content: Any, warnings: Counter[str]) -> str | None:
    """Text of one message; None when the content type is dropped on purpose."""
    if not isinstance(content, dict):
        warnings["unreadable message skipped"] += 1
        return None
    kind = str(content.get("content_type", ""))
    if kind in _SILENT:
        return None
    parts = content.get("parts")
    if kind == "text":
        return "\n".join(p for p in parts or [] if isinstance(p, str))
    if kind == "multimodal_text":
        return _multimodal(parts if isinstance(parts, list) else [], warnings)
    if kind == "code":
        lang = str(content.get("language") or "")
        return fenced(str(content.get("text") or ""), "" if lang == "unknown" else lang)
    if kind == "execution_output":
        return "Output:\n" + fenced(str(content.get("text") or ""))
    warnings[f"unknown ChatGPT content type '{kind[:40]}'"] += 1
    return f"[{kind[:40] or 'content'}]"


def _branch(raw: dict[str, Any], mapping: dict[str, Any], warnings: Counter[str]) -> list[Any]:
    """Nodes of the active branch, root first."""
    current = raw.get("current_node")
    if not isinstance(current, str) or current not in mapping:
        # No usable pointer: follow the most recently written leaf instead.
        warnings["conversation without an active branch: newest branch used"] += 1
        leaves = [k for k, n in mapping.items() if isinstance(n, dict) and not n.get("children")]
        if not leaves:
            raise ValueError("no leaf node")
        current = max(leaves, key=lambda k: _node_time(mapping[k]))
    chain: list[Any] = []
    seen: set[str] = set()
    node_id: Any = current
    while isinstance(node_id, str) and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        node = mapping[node_id]
        if not isinstance(node, dict):
            raise TypeError("mapping node is not an object")
        chain.append(node)
        node_id = node.get("parent")
    return chain[::-1]


def _node_time(node: Any) -> float:
    msg = node.get("message") if isinstance(node, dict) else None
    value = msg.get("create_time") if isinstance(msg, dict) else None
    return float(value) if isinstance(value, int | float) else 0.0


def _message(msg: dict[str, Any], warnings: Counter[str]) -> Message | None:
    author = msg.get("author")
    role = _ROLES.get(str(author.get("role", ""))) if isinstance(author, dict) else None
    raw_meta = msg.get("metadata")
    metadata: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    if role is None or metadata.get("is_visually_hidden_from_conversation"):
        return None
    text = _content_text(msg.get("content"), warnings)
    if text is None:
        return None
    attachments = tuple(
        Attachment(clean_text(str(a["name"]))[:200])
        for a in metadata.get("attachments") or []
        if isinstance(a, dict) and a.get("name")
    )
    text = clean_text(text).strip()
    if not text and not attachments:
        return None
    return Message(role, text, parse_epoch(msg.get("create_time")), attachments)


def parse_conversation(raw: dict[str, Any], warnings: Counter[str]) -> Conversation | None:
    """One conversation; None if it has no usable messages. Raises on malformed input."""
    cid = raw.get("conversation_id") or raw.get("id")
    if not isinstance(cid, str) or not cid:
        raise ValueError("missing id")
    mapping = raw["mapping"]
    messages: list[Message] = []
    for node in _branch(raw, mapping, warnings):
        msg = node.get("message")
        if isinstance(msg, dict) and (m := _message(msg, warnings)):
            messages.append(m)
    if not messages:
        warnings["empty conversation skipped"] += 1
        return None
    created = parse_epoch(raw.get("create_time")) or messages[0].created_at
    updated = parse_epoch(raw.get("update_time")) or messages[-1].created_at or created
    return Conversation(
        source="chatgpt",
        id=cid,
        title=clean_title(raw.get("title"), tuple(messages)),
        created_at=created,
        updated_at=updated,
        messages=tuple(messages),
    )
