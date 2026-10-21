"""Render a conversation as the clean markdown transcript that is ingested as a raw source.

    # <title>
    - Source, conversation id (the dedupe key), link, dates, message count
    - (a continuation: which messages it holds, and where the earlier ones are)
    ## User · 2026-03-01 10:02
    ...

Headings inside messages are demoted below the speaker headings, huge code blocks and
attachment texts are truncated, and secrets are redacted. A transcript larger than the
wiki's source limit is split into parts at message boundaries.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass, field

from portal.imports.model import SOURCE_LABELS, Conversation, Message
from portal.imports.redact import redact

MAX_CODE_LINES = 60
KEEP_CODE_LINES = 30
MAX_CODE_CHARS = 6000
MAX_ATTACHMENT_CHARS = 4000
# A paste is usually the substance of the question (a document, a log), so it keeps more.
MAX_PASTED_CHARS = 50_000
# wiki-mcp refuses sources over 1 MiB including frontmatter and its notice.
MAX_PART_BYTES = 900 * 1024
_BODY_BYTES = MAX_PART_BYTES - 2048  # room for the header
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^ {0,3}(#{1,6})(?=\s|$)")
_ROLE = {"user": "User", "assistant": "Assistant"}


@dataclass
class Transcript:
    parts: list[str]
    title: str = ""  # the conversation title, redacted like the body
    redactions: Counter[str] = field(default_factory=Counter)
    truncated_blocks: int = 0


def _when(value: dt.datetime | None) -> str:
    return value.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M UTC") if value else "unknown"


def _truncate_block(lines: list[str]) -> tuple[list[str], bool]:
    """Body lines of one code block, cut down if it is huge."""
    size = sum(len(line) + 1 for line in lines)
    if len(lines) <= MAX_CODE_LINES and size <= MAX_CODE_CHARS:
        return lines, False
    kept: list[str] = []
    budget = MAX_CODE_CHARS // 2
    for line in lines[:KEEP_CODE_LINES]:
        if budget - len(line) < 0:
            break
        kept.append(line)
        budget -= len(line) + 1
    return [*kept, f"… [{len(lines) - len(kept)} more lines truncated on import]"], True


def tidy_markdown(text: str) -> tuple[str, int]:
    """Demote headings outside code, truncate huge code blocks, close an unclosed fence."""
    out: list[str] = []
    block: list[str] = []
    fence: str | None = None
    truncated = 0
    for line in text.split("\n"):
        m = _FENCE.match(line)
        if fence is None:
            if m:
                fence = m.group(1)
                out.append(line)
            else:
                out.append(_HEADING.sub(lambda h: "#" * min(6, len(h.group(1)) + 3), line))
            continue
        if (
            m
            and m.group(1)[0] == fence[0]
            and len(m.group(1)) >= len(fence)
            and not line.strip(" `~")
        ):
            kept, cut = _truncate_block(block)
            truncated += cut
            out += [*kept, line]
            block, fence = [], None
        else:
            block.append(line)
    if fence is not None:
        kept, cut = _truncate_block(block)
        truncated += cut
        out += [*kept, fence]
    return "\n".join(out), truncated


def _message_md(msg: Message) -> tuple[str, int]:
    body, truncated = tidy_markdown(msg.text) if msg.text else ("", 0)
    lines = [f"## {_ROLE[msg.role]} · {_when(msg.created_at)}", ""]
    if body:
        lines += [body, ""]
    for att in msg.attachments:
        lines.append(f"**Attachment:** {att.name}")
        if att.text:
            text = att.text.strip()
            limit = MAX_PASTED_CHARS if att.pasted else MAX_ATTACHMENT_CHARS
            if len(text) > limit:
                text = text[:limit] + "\n… [attachment text truncated on import]"
                truncated += 1
            fence = "`" * max([3, *(len(r) + 1 for r in re.findall(r"`{3,}", text))])
            lines += ["", f"{fence}text", text, fence]
        lines.append("")
    return "\n".join(lines), truncated


def timestamp(value: dt.datetime | None) -> str | None:
    """A message time as wiki-mcp records it (`last_message_at`), to the second."""
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def _header(conv: Conversation, part: int, parts: int, start: int) -> str:
    lines = [
        f"# {conv.title}",
        "",
        f"- Source: {SOURCE_LABELS[conv.source]} conversation",
        f"- Conversation id: `{conv.key}`",
        f"- Original: <{conv.url}>",
        f"- Started: {_when(conv.created_at)}",
        f"- Last updated: {_when(conv.updated_at)}",
        f"- Messages: {len(conv.messages)}",
    ]
    if start:
        lines.append(
            f"- Continuation: messages {start + 1} to {len(conv.messages)}; the first {start} "
            f"are in `raw/chats/{conv.source}/{conv.id.lower()}`"
        )
    if parts > 1:
        lines.append(f"- Part {part} of {parts}")
    return "\n".join(lines) + "\n\n"


def _cap(chunk: str) -> str:
    """A single message larger than a whole part: keep its beginning."""
    raw = chunk.encode("utf-8")
    if len(raw) <= _BODY_BYTES:
        return chunk
    cut = raw[: _BODY_BYTES - 200].decode("utf-8", errors="ignore")
    return cut + "\n\n… [message truncated on import: too long for one source]\n"


def render(conv: Conversation, start: int = 0) -> Transcript:
    """The transcript of `conv`; with `start`, only the messages after the first `start`
    (a continuation of a conversation imported before)."""
    result = Transcript(parts=[])
    chunks: list[str] = []
    for msg in conv.messages[start:]:
        md, truncated = _message_md(msg)
        result.truncated_blocks += truncated
        red = redact(md)
        result.redactions.update(red.counts)
        chunks.append(_cap(red.text))
    groups: list[list[str]] = [[]]
    size = 0
    for chunk in chunks:
        n = len(chunk.encode("utf-8"))
        if groups[-1] and size + n > _BODY_BYTES:
            groups.append([])
            size = 0
        groups[-1].append(chunk)
        size += n
    title = redact(conv.title)
    result.redactions.update(title.counts)
    safe = Conversation(
        conv.source, conv.id, title.text, conv.created_at, conv.updated_at, conv.messages
    )
    result.title = title.text
    result.parts = [
        _header(safe, i, len(groups), start) + "\n".join(group).rstrip("\n") + "\n"
        for i, group in enumerate(groups, 1)
    ]
    return result
