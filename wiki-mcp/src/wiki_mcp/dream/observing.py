"""Observing chats from Claude.ai or ChatGPT: the observe tools (mcp/pattern_tools.py) let the
assistant's model do what the portal's backfill does with the portal's model
(portal/patterns/backfill.py), with the same contract (dream/observe.py) and the same write
(WikiStore.write_observations), so a chat observed either way is done for both.

  material  observe_material: the next imported chat without an observation page (or the one
            asked for), condensed and cut into windows as the portal reads it (wiki_mcp.condense:
            the [mN · Role · when] labels the store's checks hold the citations to), with the
            extraction prompt, the extractor to submit and how many chats are still pending
  submit    observe_submit: one window's answer, clamped with the shared parse (an unparsable
            answer is refused, not counted). A chat of one window is written at once. A longer
            chat is answered a window at a time, as the portal asks its model a chunk at a
            time: each window's answer is kept (DreamSessions.keep_window) until the last one
            is in, then they are merged as the portal merges its chunks and written once. The
            store checks every observation against the whole transcript (only the owner's
            messages are evidence, quotes are verbatim) and writes one commit per chat,
            idempotent per extractor: a chat observed already answers `exists`, nothing new.

No session: the owner asks the assistant to observe his chats (prompt: run-observe). The tools
need wiki:write and wiki:private, and are refused before anything is read without them. What
they answer names the chat and its page, counts, and reasons by position only: never a quote
or the text of another chat. The page records the assistant (`observed_by`).
"""

from __future__ import annotations

import json
from typing import Any

from wiki_mcp import condense
from wiki_mcp.dream import Unparsable
from wiki_mcp.dream.observe import (
    EXTRACTOR,
    SYSTEM,
    Observed,
    merge,
    parse,
    part_label,
    payload,
    user_prompt,
)
from wiki_mcp.dream.passes import Refused, _text, _window
from wiki_mcp.dream.sessions import DreamSessions
from wiki_mcp.wiki.actor import Actor
from wiki_mcp.wiki.observations import base_source
from wiki_mcp.wiki.store import WikiStore

INSTRUCTIONS = (
    "Answer the prompt for this window with only the JSON object it asks for, then call "
    "observe_submit with it (the same source and window)."
)


def _source(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Refused("'source' must be an imported chat's id, as observe_material gives it")
    return base_source(value.strip())


def _chunks(store: WikiStore, source: str) -> tuple[list[str], str]:
    """The chat's windows, condensed as the portal's calls read it, and its start day."""
    _, _, bodies = store.chat_parts(source)
    transcript = condense.Transcript()
    for body in bodies:
        condense.parse(body, transcript)
    return condense.condense(transcript) or [""], transcript.started


def _pending(store: WikiStore) -> tuple[int, dict[str, Any] | str]:
    """How many chats are not observed yet, and what to call next."""
    found = store.observations_pending(limit=1)
    sources = found.get("sources") or []
    if not sources:
        return 0, "done: every imported chat is observed"
    return int(found["total"]), {"source": str(sources[0]["id"]), "window": 0}


def _client(client: str) -> str:
    """The calling app's name as the page keeps it (observed_by)."""
    return " ".join(str(client).split())[:40].strip() or "unknown"


async def material(store: WikiStore, source: Any, window: Any) -> dict[str, Any]:
    """observe_material: one window of a chat to observe, with the prompt."""
    window = _window(window)
    if source is None or source == "":
        _, following = _pending(store)
        if not isinstance(following, dict):
            return {"pending": 0, "next": following, "instructions": "Nothing to observe."}
        source = following["source"]
    source = _source(source)
    if store.observed_with(source, EXTRACTOR):
        pending, following = _pending(store)
        return {
            "source": source,
            "status": "exists",
            "pending": pending,
            "next": following,
            "instructions": "This chat is observed already: go on with `next`.",
        }
    chunks, started = _chunks(store, source)
    if window >= len(chunks):
        raise Refused(f"this chat has {len(chunks)} window(s): 0 to {len(chunks) - 1}")
    pending, _ = _pending(store)
    return {
        "source": source,
        "window": window,
        "windows": len(chunks),
        "extractor": EXTRACTOR,
        "prompt": SYSTEM,
        "material": user_prompt(started, chunks[window], part_label(window + 1, len(chunks))),
        "pending": pending,
        "instructions": INSTRUCTIONS
        + (
            " Answer each window on its own: the chat is written once its last window is in."
            if len(chunks) > 1
            else ""
        ),
    }


def _shape_drops(text: str, observed: Observed) -> list[str]:
    """What the shared parse left out of the answer, counted (it drops without saying)."""
    try:
        items = json.loads(text).get("observations")
    except (ValueError, AttributeError):
        return []
    left = (len(items) if isinstance(items, list) else 0) - len(observed.observations)
    if left <= 0:
        return []
    return [
        f"{left} observation(s) not in the contract's shape (a known kind and basis, a text, "
        "msgs and a quote) left out"
    ]


async def submit(
    store: WikiStore,
    sessions: DreamSessions | None,
    source: Any,
    window: Any,
    answer: Any,
    actor: Actor,
) -> dict[str, Any]:
    """observe_submit: keep or write one window's answer (see the module docstring)."""
    source, window = _source(source), _window(window)
    text = _text(answer)
    try:
        observed = parse(text)
    except Unparsable as exc:
        raise Refused(
            f"the answer is not what the prompt asks for ({exc}); it was not counted: call "
            "observe_submit again with only the JSON object"
        ) from exc
    dropped = _shape_drops(text, observed)
    if store.observed_with(source, EXTRACTOR):
        pending, following = _pending(store)
        return {"source": source, "status": "exists", "pending": pending, "next": following}
    chunks, _ = _chunks(store, source)
    windows = len(chunks)
    if window >= windows:
        raise Refused(f"this chat has {windows} window(s): 0 to {windows - 1}")
    if windows == 1:
        merged = merge([observed])
    else:
        if sessions is None:
            raise Refused("long chats cannot be observed here: this server keeps no windows")
        held = sessions.keep_window(source, window, windows, observed.as_dict())
        missing = [w for w in range(windows) if w not in held]
        if missing:
            return {
                "source": source,
                "status": "window kept",
                "window": window,
                "windows": windows,
                "windows_to_do": missing,
                "dropped": dropped,
                "next": {"source": source, "window": missing[0]},
                "instructions": "Call observe_material for that window and answer it too: the "
                "chat is written once every window is in.",
            }
        merged = merge([Observed(**kept) for kept in sessions.windows_of(source)])
    result = await store.write_observations(
        source, EXTRACTOR, payload(source, merged), actor, observed_by=_client(actor.client)
    )
    if windows > 1 and sessions is not None:
        sessions.drop_windows(source)
    pending, following = _pending(store)
    if result["status"] == "exists":
        return {"source": source, "status": "exists", "pending": pending, "next": following}
    return {
        "source": source,
        "status": result["status"],
        "page": result["id"],
        "kept": result["observations"],
        "dropped": dropped + list(result.get("dropped") or []),
        "adjusted": list(result.get("adjusted") or []),
        "pending": pending,
        "next": following,
    }
