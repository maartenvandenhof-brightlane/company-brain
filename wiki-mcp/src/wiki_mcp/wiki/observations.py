"""Observations: what the owner's own messages in one imported chat show about him, the
evidence personal patterns rest on (wiki/patterns.py, dream/patterns.py). They are never facts
and never go onto entity pages; filing facts stays batch processing's (POST /file).

  page      wiki/observations/<source>-<conversation stem> (type observation, always private,
            status draft so no dream takes it for a reviewed page): one per imported transcript,
            its -part-K files together (message numbers run on across them); a continuation
            (-after-N) is a transcript of its own. The server writes it whole, in one commit
            (WikiStore.write_observations), and again only for another extractor version. A chat
            with nothing personal gets a page with no observations, so it counts as done. The
            portal's model or an assistant's (the observe tools: `observed_by`) observes it.
  numbers   messages are numbered as the models read them (parse; wiki_mcp.condense, which
            the portal's calls and the observe tools condense transcripts with, numbers them
            here too): from 1, or from the first message a continuation holds
            ("- Continuation: messages N to M"), running on across parts
  checks    the store has the raw text, so provenance is checked here, never trusted (check):
              - every cited message is a User message of the transcript: an observation or
                emotion citing an Assistant message, or none, is dropped. What the assistant
                said is never evidence.
              - the quote is a verbatim excerpt of one of the cited user messages (spacing,
                case, quote marks and emphasis aside); else dropped
              - endorsed (the observation rests on an assistant suggestion the owner accepted in
                the quoted message) needs an assistant message right before the first cited one;
                else the flag is taken off
              - cited user messages that are all questions (ending in "?") show behaviour in the
                chat only: basis chat and never an action, outcome or value, else dropped.
                Questions alone never imply actions, non-execution, traits or diagnoses.
              - a tendency needs the owner saying it of himself (basis statement); a single
                report is a state
              - fixed vocabularies (TYPES, CONTEXTS, KINDS, ...): unknown values are dropped or
                fall back
            Every drop and change is reported by position and reason only, never with the text:
            the reports go into logs.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from wiki_mcp.wiki.paths import slugify

FOLDER = "wiki/observations"
EXTRACTOR = re.compile(r"^[a-z][a-z0-9.-]{0,39}$")
TYPES = (
    "practical-task",
    "work",
    "learning",
    "planning",
    "decision",
    "reflection",
    "emotional-processing",
    "relationships",
    "health",
    "creative",
    "venting",
    "other",
)
CONTEXTS = (
    "work",
    "business",
    "family",
    "partner",
    "friends",
    "health",
    "finances",
    "home",
    "learning",
    "hobbies",
    "self",
    "other",
)
KINDS = (
    "value",
    "motivation",
    "action",
    "intention",
    "outcome",
    "trigger",
    "response",
    "tension",
    "change",
)
BASES = ("statement", "reported", "chat")
SPANS = ("state", "tendency", "unknown")
VALENCES = ("difficulty", "strength", "neutral")
EMOTION_KINDS = ("stated", "tone")
SENTIMENTS = ("negative", "mixed", "neutral", "positive")
# Too vague to say anything: an emotion is named specifically (frustrated, relieved, proud).
GENERIC_EMOTIONS = frozenset(
    {"positive", "negative", "neutral", "good", "bad", "fine", "ok", "okay", "mixed", "emotional"}
)
NOT_FROM_QUESTIONS = ("action", "outcome", "value")
MAX_OBSERVATIONS = 40
MAX_EMOTIONS = 20
MAX_INTENTS = 5
MAX_MSGS = 10
MIN_QUOTE = 3  # characters of a quote, normalised
QUOTE_CHARS = 300
TEXT_CHARS = 500

_MESSAGE = re.compile(r"^## (User|Assistant) · (.*)$", re.M)
_CONTINUATION = re.compile(r"^- Continuation: messages (\d+) to", re.M)
_WHEN = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})")
_EVENT_WHEN = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
_PART = re.compile(r"-part-\d+$")
_CHAT_ID = re.compile(r"^raw/chats/(claude|chatgpt)/([a-z0-9][a-z0-9-]{0,120})$")
# curly quotes and a no-break space become plain ones; emphasis and code marks go
_MARKS = str.maketrans(
    {chr(0x2018): "'", chr(0x2019): "'", chr(0x201C): '"', chr(0x201D): '"', chr(0xA0): " "}
    | {"*": "", "`": ""}
)


class ObservationError(ValueError):
    """An observations write is malformed as a whole (a single bad observation is dropped)."""


# --- transcripts --------------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    number: int
    role: str  # User | Assistant
    when: str  # as in the transcript, e.g. 2026-01-10 09:00 UTC
    text: str

    @property
    def at(self) -> str | None:
        """The message time as an ISO timestamp, or None when the transcript has none."""
        m = _WHEN.match(self.when)
        return f"{m.group(1)}T{m.group(2)}:00Z" if m else None

    @property
    def question(self) -> bool:
        return self.text.rstrip().endswith("?")


def parse(bodies: list[str]) -> list[Message]:
    """The messages of a transcript's parts (source bodies, in order), numbered as the models
    read them (wiki_mcp.condense uses parse_part too)."""
    messages: list[Message] = []
    for body in bodies:
        parse_part(body, messages)
    return messages


def parse_part(body: str, messages: list[Message]) -> None:
    """Append the messages of one transcript part to those of the parts before it: numbered
    from 1, or from the first message a continuation holds, running on across parts."""
    head = _MESSAGE.split(body, maxsplit=1)[0]
    m = _CONTINUATION.search(head)
    first = int(m.group(1)) if m else len(messages) + 1
    number = messages[-1].number + 1 if messages else first
    pieces = _MESSAGE.split(body)[1:]
    for i in range(0, len(pieces) - 2, 3):
        role, when, text = pieces[i], pieces[i + 1], pieces[i + 2]
        messages.append(Message(number, role, when.strip(), text.strip("\n")))
        number += 1


def norm(text: str) -> str:
    """Text as quotes are compared: NFKC, straight quote marks, no emphasis marks, one space,
    case-folded."""
    return " ".join(unicodedata.normalize("NFKC", text).translate(_MARKS).casefold().split())


def _quote_core(quote: str) -> str:
    """A quote without the quote marks and ellipses a model puts around an excerpt."""
    core = norm(quote)
    for _ in range(3):  # e.g. "... and then …"
        core = core.strip(" \"'…").removeprefix("...").removesuffix("...")
    return core.strip()


def quoted_in(quote: str, messages: list[Message]) -> bool:
    """Whether `quote` is an excerpt of one of `messages` (normalised)."""
    core = _quote_core(quote)
    return len(core) >= MIN_QUOTE and any(core in norm(m.text) for m in messages)


# --- the checks ---------------------------------------------------------------------------


@dataclass
class Checked:
    """What may be stored of one chat's observations, and what was left out or changed (by
    position and reason, never the text)."""

    classification: dict[str, Any]
    observations: list[dict[str, Any]]
    dropped: list[str] = field(default_factory=list)
    adjusted: list[str] = field(default_factory=list)


def _str(value: Any, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _ints(value: Any) -> list[int]:
    if not isinstance(value, list):
        value = [value]
    return [v for v in value if isinstance(v, int) and not isinstance(v, bool)][:MAX_MSGS]


def _vocab(value: Any, allowed: tuple[str, ...], most: int) -> list[str]:
    items = value if isinstance(value, list) else []
    return list(dict.fromkeys(v for v in items if isinstance(v, str) and v in allowed))[:most]


class _Transcript:
    def __init__(self, messages: list[Message]) -> None:
        self.messages = messages
        self.by_number = {m.number: m for m in messages}
        self.position = {m.number: i for i, m in enumerate(messages)}

    def user(self, numbers: list[int]) -> list[Message] | None:
        """The cited messages when every one is a User message of the transcript."""
        found = [self.by_number.get(n) for n in numbers]
        if not numbers or any(m is None or m.role != "User" for m in found):
            return None
        return [m for m in found if m is not None]

    def after_assistant(self, number: int) -> bool:
        i = self.position.get(number, 0)
        return i > 0 and self.messages[i - 1].role == "Assistant"


def _emotion(n: int, item: Any, t: _Transcript, out: Checked) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        out.dropped.append(f"emotion {n}: not an object")
        return None
    emotion = _str(item.get("emotion"), 40).lower()
    kind = item.get("kind")
    if not emotion or emotion in GENERIC_EMOTIONS:
        out.dropped.append(f"emotion {n}: not a specific emotion")
        return None
    if kind not in EMOTION_KINDS:
        out.dropped.append(f"emotion {n}: kind must be stated or tone")
        return None
    cited = t.user(_ints(item.get("msg"))[:1])
    if cited is None:
        out.dropped.append(f"emotion {n}: cites a message that is not the owner's")
        return None
    quote = _str(item.get("quote"), QUOTE_CHARS)
    if not quoted_in(quote, cited):
        out.dropped.append(f"emotion {n}: the quote is not in the cited message")
        return None
    return {
        "emotion": emotion,
        "target": _str(item.get("target"), 120),
        "kind": kind,
        "msg": cited[0].number,
        "quote": quote,
        "message_at": cited[0].at,
    }


def _event(item: Any, text: str) -> dict[str, str]:
    event = item if isinstance(item, dict) else {}
    summary = _str(event.get("summary"), 200) or text[:200]
    when = _str(event.get("when"), 10)
    key = slugify(_str(event.get("key"), 60) or summary, 40)
    return {"summary": summary, "when": when if _EVENT_WHEN.match(when) else "unknown", "key": key}


def _observation(n: int, item: Any, t: _Transcript, out: Checked) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        out.dropped.append(f"observation {n}: not an object")
        return None
    kind, basis = item.get("kind"), item.get("basis")
    text = _str(item.get("text"), TEXT_CHARS)
    if kind not in KINDS or basis not in BASES or not text:
        out.dropped.append(f"observation {n}: needs a known kind, a known basis and a text")
        return None
    cited = t.user(_ints(item.get("msgs")))
    if cited is None:
        out.dropped.append(f"observation {n}: cites a message that is not the owner's")
        return None
    quote = _str(item.get("quote"), QUOTE_CHARS)
    if not quoted_in(quote, cited):
        out.dropped.append(f"observation {n}: the quote is not in the cited user messages")
        return None
    if all(m.question for m in cited) and (basis != "chat" or kind in NOT_FROM_QUESTIONS):
        out.dropped.append(
            f"observation {n}: rests on questions only, which show behaviour in the chat "
            "(basis chat), never an action, outcome or value"
        )
        return None
    endorsed = item.get("endorsed") is True
    if endorsed and not t.after_assistant(cited[0].number):
        out.adjusted.append(f"observation {n}: endorsed taken off (no assistant message before)")
        endorsed = False
    span = item.get("span") if item.get("span") in SPANS else "unknown"
    if span == "tendency" and basis != "statement":
        out.adjusted.append(f"observation {n}: a single report is a state, not a tendency")
        span = "state"
    context = item.get("context") if item.get("context") in CONTEXTS else "other"
    valence = item.get("valence") if item.get("valence") in VALENCES else "neutral"
    return {
        "kind": kind,
        "basis": basis,
        "text": text,
        "msgs": [m.number for m in cited],
        "quote": quote,
        "message_at": cited[0].at,
        "event": _event(item.get("event"), text),
        "span": span,
        "context": context,
        "valence": valence,
        "endorsed": endorsed,
    }


def check(payload: Any, messages: list[Message]) -> Checked:
    """The classification and observations of one chat as they may be stored: checked against
    its transcript's messages (see the module docstring). ObservationError when the payload
    is not the expected shape at all."""
    if not isinstance(payload, dict):
        raise ObservationError("expected a JSON object")
    raw_class = payload.get("classification") or {}
    raw_obs = payload.get("observations") or []
    if not isinstance(raw_class, dict) or not isinstance(raw_obs, list):
        raise ObservationError("'classification' must be an object and 'observations' a list")
    t = _Transcript(messages)
    out = Checked({}, [])
    emotions = raw_class.get("emotions") or []
    kept_emotions = [
        e
        for n, item in enumerate(emotions[:MAX_EMOTIONS] if isinstance(emotions, list) else [], 1)
        if (e := _emotion(n, item, t, out)) is not None
    ]
    intents = raw_class.get("intents")
    classification: dict[str, Any] = {
        "types": _vocab(raw_class.get("types"), TYPES, len(TYPES)) or ["other"],
        "contexts": _vocab(raw_class.get("contexts"), CONTEXTS, len(CONTEXTS)),
        "intents": [
            s for s in (_str(i, 200) for i in (intents if isinstance(intents, list) else [])) if s
        ][:MAX_INTENTS],
        "emotions": kept_emotions,
    }
    if detail := _str(raw_class.get("context_detail"), 200):
        classification["context_detail"] = detail
    if raw_class.get("sentiment") in SENTIMENTS:
        classification["sentiment"] = raw_class["sentiment"]
    if len(raw_obs) > MAX_OBSERVATIONS:
        out.dropped.append(f"{len(raw_obs) - MAX_OBSERVATIONS} observations over the limit")
    for n, item in enumerate(raw_obs[:MAX_OBSERVATIONS], 1):
        kept = _observation(n, item, t, out)
        if kept is not None:
            out.observations.append({"id": f"o{len(out.observations) + 1}", **kept})
    out.classification = classification
    return out


# --- the page -----------------------------------------------------------------------------


def page_id(source_id: str) -> str:
    """The observation page of an imported transcript, from the id of any of its parts:
    raw/chats/claude/<id>[-after-N][-part-K] gives wiki/observations/claude-<id>[-after-N]."""
    m = _CHAT_ID.match(source_id.strip("/").removesuffix(".md"))
    if m is None:
        raise ObservationError(f"{source_id[:120]} is not an imported chat")
    return f"{FOLDER}/{m.group(1)}-{_PART.sub('', m.group(2))}"


def base_source(source_id: str) -> str:
    """The id of a transcript's first part (the -part-K suffix taken off)."""
    return _PART.sub("", source_id.strip("/").removesuffix(".md"))


def title(label: str, started: str | None) -> str:
    return f"Observations: {label} chat" + (f" of {started[:10]}" if started else "")


def render(fm: dict[str, Any]) -> str:
    """The page body: a short, neutral rendering of the frontmatter for a human reader."""
    c = fm.get("classification") or {}
    source = str(fm.get("source") or "")
    by = f" in {fm['observed_by']}" if fm.get("observed_by") else ""
    lines = [
        f"# {fm.get('title')}",
        "",
        "**Evidence, not facts.** What the owner's own messages in the imported chat "
        f"`{source}` show, as extracted by `{fm.get('extractor')}`{by} on {fm.get('observed')}. "
        "Each observation cites the owner's message by number and quotes it; what the assistant "
        "said is never evidence. Emotions marked *inferred from tone* were not stated.",
        "",
        "## Classification",
        "",
        f"- Types: {', '.join(c.get('types') or []) or 'none'}",
        f"- Contexts: {', '.join(c.get('contexts') or []) or 'none'}"
        + (f" ({c['context_detail']})" if c.get("context_detail") else ""),
    ]
    if c.get("intents"):
        lines.append(f"- Intents: {'; '.join(c['intents'])}")
    if c.get("sentiment"):
        lines.append(f"- Sentiment: {c['sentiment']}")
    for e in c.get("emotions") or []:
        how = "stated" if e.get("kind") == "stated" else "inferred from tone, not stated"
        about = f" about {e['target']}" if e.get("target") else ""
        lines.append(f'- Emotion: {e["emotion"]}{about} ({how}; m{e["msg"]}): "{e["quote"]}"')
    lines += ["", "## Observations", ""]
    observations = fm.get("observations") or []
    if not observations:
        lines.append("Nothing personal was observed in this chat.")
    for o in observations:
        ev = o.get("event") or {}
        when = str(o.get("message_at") or "")[:10] or "undated"
        msgs = ", ".join(f"m{n}" for n in o.get("msgs") or [])
        endorsed = ", endorsed" if o.get("endorsed") else ""
        lines.append(
            f"- {o['id']} ({msgs}, {when}; {o['basis']} {o['kind']}, {o['context']}, "
            f"{o['valence']}, {o['span']}{endorsed}): {o['text']} "
            f'"{o["quote"]}" (event: {ev.get("summary", "")}, {ev.get("when", "unknown")})'
        )
    return "\n".join(lines) + "\n"
