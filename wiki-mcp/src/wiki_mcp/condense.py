"""Condense an imported transcript into what an extraction call needs to read.

Measured on real imports, assistant text is over half of a transcript while the facts to
file come from the owner. So the owner's messages are kept whole, and an assistant
message is cut to its beginning and end, with code replaced by a short placeholder. An
assistant message the owner answers briefly ("yes, option 2") keeps much more, so what
was accepted stays readable. Messages keep their numbers, so claims can point at them: the
numbers are wiki.observations.parse's, the ones its checks hold observations to.

The portal's model calls (batch processing, observing chats, ideas) and the observe tools for
assistants (dream/observing.py) read transcripts condensed and chunked here, the same way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from wiki_mcp.text import shorten
from wiki_mcp.wiki.observations import Message, parse_part

ASSISTANT_CHARS = 600  # an assistant message, head and tail
ANSWERED_CHARS = 3000  # an assistant message followed by a short reply
SHORT_REPLY = 300  # a reply this short usually accepts or picks from the message before
CHUNK_CHARS = 60_000  # condensed text per extraction call (about 15k tokens)

_MESSAGE = re.compile(r"^## (User|Assistant) · (.*)$", re.M)
_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})([^\n`]*)\n(.*?)^\1\2[ \t]*$", re.M | re.S)
_TITLE = re.compile(r"^# (.+)$", re.M)
_STARTED = re.compile(r"^- Started: (\d{4}-\d{2}-\d{2})", re.M)


@dataclass
class Transcript:
    title: str = ""
    started: str = ""  # YYYY-MM-DD
    messages: list[Message] = field(default_factory=list)


def parse(body: str, transcript: Transcript | None = None) -> Transcript:
    """Messages of one transcript part (appended to `transcript` for later parts)."""
    t = transcript or Transcript()
    head = _MESSAGE.split(body, maxsplit=1)[0]
    if not t.title and (m := _TITLE.search(head)):
        t.title = m.group(1).strip()
    if not t.started and (m := _STARTED.search(head)):
        t.started = m.group(1)
    parse_part(body, t.messages)
    return t


def _code_placeholders(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        lines = m.group(4).count("\n")
        lang = m.group(3).strip()
        return f"[code: {lines} lines{' ' + lang if lang else ''}]"

    return _FENCE.sub(repl, text)


def condense(t: Transcript) -> list[str]:
    """The condensed transcript, split at message boundaries into chunks of CHUNK_CHARS."""
    rendered: list[str] = []
    for i, msg in enumerate(t.messages):
        text = msg.text
        if msg.role == "Assistant":
            nxt = t.messages[i + 1] if i + 1 < len(t.messages) else None
            answered = nxt is not None and nxt.role == "User" and len(nxt.text) <= SHORT_REPLY
            text = shorten(
                _code_placeholders(text), ANSWERED_CHARS if answered else ASSISTANT_CHARS
            )
        rendered.append(f"[m{msg.number} · {msg.role} · {msg.when}]\n{text.strip()}\n")
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for piece in rendered:
        if current and size + len(piece) > CHUNK_CHARS:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(piece)
        size += len(piece)
    if current:
        chunks.append("\n".join(current))
    return chunks
