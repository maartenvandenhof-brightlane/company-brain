"""Inference pages as the dream passes write them (deduction and induction): one
`write_inference` operation for a proposal, in the shape wiki-mcp checks (store.py,
_inference_change):

  id            wiki/inferred/<slug of the title>, made unique with -2, -3, ...
  frontmatter   title, description, kind, confidence (the rubric's ceiling for the supports;
                always low when speculative), speculative, about, supports and
                counterexamples ({page, quote, source}: a stated line, stripped, and the raw
                source it cites), dream, stale_after (90 days on), status
  body          the title, the banner and the confidence, the statement, the supporting
                facts and counterexamples as links, and the history

The supports must be stated lines the caller has checked; wiki-mcp checks them again.

An update (induction's update and downgrade) writes the existing page whole again, as wiki-mcp
requires, keeping every line it had: new supporting facts, counterexamples and a History line
are added at the end of their sections; only the banner line (not a bullet) is rewritten for
the new confidence. The frontmatter gets the old entries plus the new ones and a new
stale_after; the other fields stay (wiki-mcp records the updating dream in `dream`).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from wiki_mcp.wiki.facts import (
    BANNER,
    CONFIDENCES,
    INFERRED_FOLDER,
    SPECULATIVE_BANNER,
    above,
    rubric,
)
from wiki_mcp.wiki.paths import slugify

STALE_DAYS = 90
DESCRIPTION_CHARS = 500
SPECULATIVE = "Speculative:"

_BULLET = re.compile(r"^[-*+]\s+(?:\[[ xX]\]\s+)?")
_FOOTNOTE = re.compile(r"\s*\[\^[^\]\s]+\](?!:)")


@dataclass(frozen=True)
class Evidence:
    """A stated line an inference rests on (or that contradicts it)."""

    page: str  # page id
    title: str  # the page's title, for the link
    line: str  # the line as it is on the page
    source: str  # the raw source id its footnote cites

    @property
    def quote(self) -> str:
        return self.line.strip()

    def entry(self) -> dict[str, str]:
        return {"page": self.page, "quote": self.quote, "source": self.source}

    def item(self) -> str:
        """The body's list item: a link to the page, the fact, its source's footnote."""
        text = " ".join(_FOOTNOTE.sub("", _BULLET.sub("", self.quote)).split())
        title = self.title.replace("[", "(").replace("]", ")")
        stem = self.source.rsplit("/", 1)[-1]
        return f'- [{title}](/{self.page}.md): "{text}" [^{stem}]'


def page_id(title: str, taken: set[str]) -> str:
    """A new inference page id for the title, not in `taken` (which it is added to)."""
    base = f"{INFERRED_FOLDER}/{slugify(title)}"
    pid, n = base, 1
    while pid in taken:
        n += 1
        pid = f"{base}-{n}"
    taken.add(pid)
    return pid


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def write_op(
    *,
    title: str,
    statement: str,
    kind: str,
    supports: list[Evidence],
    counterexamples: list[Evidence] | None = None,
    about: list[str],
    dream: int,
    taken: set[str],
    at: dt.datetime,
    speculative: bool = False,
    confidence: str | None = None,
) -> dict[str, Any]:
    """The write_inference operation creating a new inference page, with `confidence` (the
    rubric's ceiling when None or above it; always low when speculative). ValueError when a
    speculative statement does not say so or there are fewer than two supports."""
    counter = counterexamples or []
    title = " ".join(title.split())
    statement = statement.strip()
    if len(supports) < 2:
        raise ValueError("an inference needs at least two supporting stated lines")
    if speculative and not statement.startswith(SPECULATIVE):
        raise ValueError(f'a speculative statement starts with "{SPECULATIVE}"')
    entries = [s.entry() for s in supports]
    confidence = capped(confidence, entries, [c.entry() for c in counter], speculative)
    day = at.strftime("%Y-%m-%d")
    body = "\n".join(
        [
            f"# {title}",
            "",
            banner_line(confidence, entries, speculative),
            "",
            statement,
            "",
            "## Supporting facts",
            *(s.item() for s in supports),
            "",
            "## Counterexamples",
            *(c.item() for c in counter),
            "",
            "## History",
            f"- {day}: created by dream {dream} (confidence {confidence}).",
        ]
    )
    description = " ".join(statement.split())
    if len(description) > DESCRIPTION_CHARS:
        description = description[: DESCRIPTION_CHARS - 1].rstrip() + "…"
    frontmatter = {
        "title": title,
        "description": description,
        "kind": kind,
        "confidence": confidence,
        "speculative": speculative,
        "about": about,
        "supports": entries,
        "counterexamples": [c.entry() for c in counter],
        "dream": dream,
        "stale_after": _stale_after(at),
        "status": "stable",
    }
    return {
        "op": "write_inference",
        "page": page_id(title, taken),
        "frontmatter": frontmatter,
        "body": body + "\n",
    }


def _stale_after(at: dt.datetime) -> str:
    return (at + dt.timedelta(days=STALE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")


def capped(
    confidence: str | None,
    supports: list[dict[str, str]],
    counterexamples: list[dict[str, str]],
    speculative: bool = False,
) -> str:
    """`confidence`, lowered to what the rubric allows for these entries (the ceiling itself
    when None or unknown); low when speculative."""
    ceiling = rubric(supports, counterexamples)
    if speculative:
        return "low"
    if confidence is None or above(confidence, ceiling):
        return ceiling
    return confidence


def banner_line(confidence: str, supports: list[dict[str, str]], speculative: bool) -> str:
    """The line under the title: what the page is, and how confident, on what."""
    sources = len({s.get("source") for s in supports if s.get("source")})
    banner = SPECULATIVE_BANNER if speculative else BANNER
    return (
        f"{banner} Confidence: {confidence} ({_count(len(supports), 'supporting fact')} "
        f"from {_count(sources, 'source')})."
    )


def merged(old: list[dict[str, str]], new: list[Evidence]) -> list[dict[str, str]]:
    """The old entries, then the new ones they lack (the same page and quote)."""
    out = [dict(e) for e in old]
    have = {(e.get("page"), e.get("quote")) for e in out}
    for ev in new:
        entry = ev.entry()
        if (entry["page"], entry["quote"]) not in have:
            have.add((entry["page"], entry["quote"]))
            out.append(entry)
    return out


def _add_items(lines: list[str], heading: str, items: list[str]) -> None:
    """Add list items at the end of the `## heading` section (made at the end when there is
    none), in place; an item the section already has is not added twice."""
    target = heading.strip().lower()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith("## ")
         and line[3:].strip().lower() == target),
        None,
    )  # fmt: skip
    if start is None:
        new = list(dict.fromkeys(items))
        if new:
            lines.extend(["", f"## {heading}", *new])
        return
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("#")), len(lines))
    have = set(lines[start + 1 : end])
    new = [i for i in dict.fromkeys(items) if i not in have]
    at = end
    while at > start + 1 and not lines[at - 1].strip():
        at -= 1
    lines[at:at] = new


def update_op(
    *,
    page: str,
    frontmatter: dict[str, Any],
    body: str,
    supports: list[dict[str, str]],
    counterexamples: list[dict[str, str]],
    added: list[Evidence],
    added_counter: list[Evidence],
    confidence: str,
    history: str,
    at: dt.datetime,
) -> dict[str, Any]:
    """The write_inference operation updating an existing inference page: `supports` and
    `counterexamples` are its entries afterwards (old and new), `added` and `added_counter`
    the new ones for the body, `history` the History line's text after the date. ValueError
    when the confidence is unknown, above the rubric, or not low on a speculative page."""
    speculative = frontmatter.get("speculative") is True
    if confidence not in CONFIDENCES:
        raise ValueError(f"unknown confidence {confidence!r}")
    if capped(confidence, supports, counterexamples, speculative) != confidence:
        raise ValueError(f"confidence {confidence} is above what the supports allow")
    if len(supports) < 2:
        raise ValueError("an inference needs at least two supporting stated lines")
    lines = body.rstrip("\n").split("\n")
    for i, line in enumerate(lines):
        if line.startswith((BANNER, SPECULATIVE_BANNER)):
            lines[i] = banner_line(confidence, supports, speculative)
            break
    _add_items(lines, "Supporting facts", [e.item() for e in added])
    _add_items(lines, "Counterexamples", [e.item() for e in added_counter])
    _add_items(lines, "History", [f"- {at:%Y-%m-%d}: {history}"])
    fm: dict[str, Any] = {k: frontmatter[k] for k in ("title", "description", "kind", "about", "dream")
          if k in frontmatter}  # fmt: skip
    fm |= {
        "confidence": confidence,
        "speculative": speculative,
        "supports": supports,
        "counterexamples": counterexamples,
        "stale_after": _stale_after(at),
        "status": "stable",
    }
    return {
        "op": "write_inference",
        "page": page,
        "frontmatter": fm,
        "body": "\n".join(lines) + "\n",
    }
