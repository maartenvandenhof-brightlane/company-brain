"""Ideas as cards, for the Ideas tab and the home page.

Idea text is model output: the text, the counterargument, why now and the next step are
rendered through portal.render like wiki content, and each citation becomes a link into the
wiki reader (a page the wiki no longer has is marked broken).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from portal.ideas.store import Idea
from portal.render import render_markdown
from portal.wiki import page_href, wiki_resolver
from portal.wikiclient import WikiClient, WikiUnavailable

KIND_LABELS = {
    "follow-up": "Follow-up",
    "continue": "Open question",
    "connect": "Connection",
    "challenge": "Challenge",
}
SERVES_LABELS = {"goal": "Goal", "loop": "Open loop"}


@dataclass
class Cite:
    id: str
    title: str
    type: str
    href: str
    known: bool


@dataclass
class Card:
    idea: Idea
    label: str
    html: str  # sanitised (portal.render)
    counter: str
    cites: list[Cite]
    serves: str = ""  # "Goal" or "Open loop", or "" for none
    serves_text: str = ""  # plain text, escaped by the template
    why_now: str = ""  # sanitised
    next_step: str = ""  # sanitised


async def titles(wiki: WikiClient) -> dict[str, tuple[str, str]] | None:
    """Page id -> (title, type) from the wiki's index; None when the wiki does not answer."""
    try:
        index = await wiki.cached_index()
    except (WikiUnavailable, ValueError):
        return None
    return {
        str(d["id"]): (str(d.get("title") or d["id"]), str(d.get("type") or ""))
        for d in index.get("docs", [])
    }


def cards(ideas: list[Idea], known: dict[str, tuple[str, str]]) -> list[Card]:
    resolve = wiki_resolver(PurePosixPath("index.md"), set(known))

    def md(text: str) -> str:
        return render_markdown(text, resolve) if text else ""

    out = []
    for idea in ideas:
        cites = []
        for cid in idea.cites:
            title, kind = known.get(cid, (cid, ""))
            cites.append(Cite(cid, title, kind, page_href(cid), cid in known))
        serves = idea.serves.get("type", "")
        out.append(
            Card(
                idea,
                KIND_LABELS.get(idea.kind, idea.kind),
                md(idea.text),
                md(idea.counter),
                cites,
                SERVES_LABELS.get(serves, ""),
                idea.serves.get("text", "") if serves in SERVES_LABELS else "",
                md(idea.why_now),
                md(idea.next_step),
            )
        )
    return out
