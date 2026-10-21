"""D4, duplicate pages: merges, proposed only (part of the hygiene pass, after its fixes).

  find      in code: pairs of reviewed pages of the same type whose slugs (the last path
            segment) or titles (lowercase, words only) nearly match: one contains the
            other as a run of words, or their word sets overlap by at least 0.7 (Jaccard);
            at most 20 pairs per dream
  ask       the cheap model, with each pair's ids, titles, descriptions and first 800
            characters of body: the same real thing or not, and which page to keep
  check     only candidate pairs count; `same` must be true and `keep` "a" or "b"; both pages
            still reviewed; a page already in a merge proposal of this dream is left out
  propose   one owner-only identity merge per pair: a complete deterministic consolidated
            page preview, including prose, citations, metadata and unresolved claims. Approval
            requires both snapshots still match, consolidates content and leaves a minimal
            canonical redirect. Git and merge_history preserve originals. Private/non-private
            pairs and redirect chains are refused by the store.

The asking and proposing are the caller's (the portal's merges() in portal/dreams/merge.py):
this module finds the candidates, parses the answer, checks each pair and makes its operations.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any

from wiki_mcp.dream import Unparsable
from wiki_mcp.dream.deduction import REASON_CHARS, Invalid, one_line, page_ref
from wiki_mcp.dream.pages import Page, read
from wiki_mcp.dream.reader import WikiReader
from wiki_mcp.wiki.dreamops import MAX_OPS
from wiki_mcp.wiki.facts import normalized

NAME = "hygiene"  # merges are part of the hygiene pass
MAX_PAIRS = 20
MOST = MAX_PAIRS * 2  # pairs of an answer considered
JACCARD = 0.7
BODY_CHARS = 800
MERGED = "Merged"  # the section for lines under no heading
NOTES_CHARS = 300
SECTION_CHARS = 120

_WORD = re.compile(r"[a-z0-9à-ÿ]+")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*[-*+]\s")
_FENCE = re.compile(r"^\s*(```|~~~)")


# --- candidates ----------------------------------------------------------------------------


def words(text: str) -> list[str]:
    """Lowercase words only: `Garden-Shed (2026)` gives garden, shed, 2026."""
    return _WORD.findall(text.lower())


def near(a: list[str], b: list[str]) -> bool:
    """One run of words contains the other, or the word sets overlap by at least JACCARD."""
    if not a or not b:
        return False
    x, y = f" {' '.join(a)} ", f" {' '.join(b)} "
    if x in y or y in x:
        return True
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) >= JACCARD


def slug(page: Page) -> list[str]:
    return words(page.id.rsplit("/", 1)[-1])


def candidates(pages: list[Page], most: int = MAX_PAIRS) -> list[tuple[Page, Page]]:
    """Pairs of pages of the same type whose slugs or titles nearly match, in page order."""
    out: list[tuple[Page, Page]] = []
    for i, a in enumerate(pages):
        for b in pages[i + 1 :]:
            if a.type != b.type or a.id == b.id or a.private != b.private:
                continue
            if near(slug(a), slug(b)) or near(words(a.title), words(b.title)):
                out.append((a, b))
                if len(out) >= most:
                    return out
    return out


def material(pairs: list[tuple[Page, Page]]) -> str:
    out = ["<material>"]
    for a, b in pairs:
        out.append(f'<pair a="{a.id}" b="{b.id}">')
        for page in (a, b):
            description = " ".join(str(page.frontmatter.get("description") or "").split())
            out += [
                f'<page id="{page.id}" type="{page.type}">',
                f"title: {page.title}",
                f"description: {description}",
                page.body[:BODY_CHARS],
                "</page>",
            ]
        out.append("</pair>")
    out.append("</material>")
    return "\n".join(out)


# --- the answer ----------------------------------------------------------------------------


@dataclass
class Answer:
    pairs: list[Any]
    notes: str = ""


def parse(answer: str) -> Answer:
    """The answer's pairs; Unparsable unless it is a JSON object with a `pairs` list."""
    try:
        data = json.loads(answer)
    except ValueError as exc:
        raise Unparsable(f"not JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("pairs"), list):
        raise Unparsable("the answer must be a JSON object with a pairs list")
    notes = data.get("notes")
    text = "; ".join(str(n) for n in notes) if isinstance(notes, list) else ""
    return Answer(data["pairs"], " ".join(text.split())[:NOTES_CHARS])


# --- operations ----------------------------------------------------------------------------


def sectioned(body: str) -> list[tuple[str, str]]:
    """Every bullet line of a body with the heading of its section ("" under none or only
    the title), outside code fences."""
    out: list[tuple[str, str]] = []
    heading, fenced = "", False
    for line in body.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
        elif fenced:
            continue
        elif m := _HEADING.match(line):
            heading = m.group(2).strip() if len(m.group(1)) > 1 else ""
        elif _BULLET.match(line):
            out.append((heading, line.rstrip("\r")))
    return out


def _ids(values: Any) -> list[str]:
    return [
        v.strip().strip("/").removesuffix(".md")
        for v in (values if isinstance(values, list) else [])
        if isinstance(v, str) and v.strip()
    ]


def merge_ops(
    kept: Page,
    other: Page,
    known: set[str],
    dream: int,
    at: dt.datetime,
    private: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """The operations merging `other` into `kept`: what `kept` lacks, then the deprecation.
    `private`: ids of private pages, never added to the `related` of a kept page that is not."""
    ops: list[dict[str, Any]] = []
    have = {normalized(line) for _, line in sectioned(kept.body)}
    for heading, line in sectioned(other.body):
        key = normalized(line)
        if not key or key in have:
            continue
        have.add(key)
        section = (heading or MERGED)[:SECTION_CHARS]
        ops.append({"op": "append", "page": kept.id, "section": section, "line": line.strip()})
    sources = [s for s in other.sources if s not in kept.sources and s in known]
    if sources:
        ops.append({"op": "add_sources", "page": kept.id, "ids": sources})
    had = set(_ids(kept.frontmatter.get("related")))
    related = [
        r
        for r in dict.fromkeys(_ids(other.frontmatter.get("related")))
        if r not in had
        and r not in (kept.id, other.id)
        and r in known
        and (kept.private or r not in private)
    ]
    if related:
        ops.append({"op": "add_related", "page": kept.id, "ids": related[:50]})
    title = kept.title.replace("[", "(").replace("]", ")")
    note = f"Merged into [{title}](/{kept.id}.md) on {at:%Y-%m-%d} by dream {dream}."
    ops.append({"op": "deprecate", "page": other.id, "note": note})
    if len(ops) > MAX_OPS:
        raise Invalid(f"{other.id} into {kept.id}: over {MAX_OPS} changes for one proposal")
    return ops


# --- checks ------------------------------------------------------------------------------


class LeftOut(Invalid):
    """A pair left out: one of its pages is already in another merge proposal of this dream."""


@dataclass
class Merge:
    """A pair that passed the checks, as one proposal."""

    kept: Page
    other: Page
    reason: str  # the model's
    ops: list[dict[str, Any]]

    @property
    def summary(self) -> str:
        return f"Merge {self.other.title} into {self.kept.title}"

    def why(self, dream: int) -> str:
        """The proposal's reason."""
        return (
            f"Dream {dream} hygiene: {self.other.id} and {self.kept.id} are the same: {self.reason}"
        )


@dataclass
class Duplicates:
    """What the checks need across one dream's answer."""

    pairs: list[tuple[Page, Page]]  # the candidates asked about
    known: set[str]  # every indexed id
    private: frozenset[str]  # ids of private pages
    dream: int
    at: dt.datetime
    used: set[str] = field(default_factory=set)  # pages in a merge proposal of this dream
    not_same: int = 0  # pairs judged not the same
    by_pair: dict[frozenset[str], tuple[Page, Page]] = field(init=False)

    def __post_init__(self) -> None:
        self.by_pair = {frozenset((a.id, b.id)): (a, b) for a, b in self.pairs}

    async def check(self, item: Any, wiki: WikiReader) -> Merge | None:
        """The merge a pair of the answer makes (its pages read again); None when it is
        judged not the same (counted). Invalid saying why not; LeftOut when one of its pages
        is already in a merge proposal of this dream."""
        if not isinstance(item, dict):
            raise Invalid("not an object")
        a, b = page_ref(item.get("a")), page_ref(item.get("b"))
        if frozenset((a, b)) not in self.by_pair or a == b:
            raise Invalid(f"{a[:100]} and {b[:100]} are not a candidate pair")
        same = item.get("same")
        if same is False:
            self.not_same += 1
            return None
        if same is not True:
            raise Invalid(f"{a} and {b}: 'same' is not true or false")
        keep = item.get("keep")
        if keep not in ("a", "b"):
            raise Invalid(f'{a} and {b}: \'keep\' is not "a" or "b"')
        reason = one_line(item.get("reason"), "reason", REASON_CHARS)
        kept_id, other_id = (a, b) if keep == "a" else (b, a)
        if kept_id in self.used or other_id in self.used:
            raise LeftOut(
                f"{other_id} into {kept_id}: a page is already in another merge proposal of "
                "this dream"
            )
        kept, other = await read(wiki, kept_id), await read(wiki, other_id)
        if kept is None or not (kept.reviewed and kept.status == "stable"):
            raise Invalid(f"{kept_id} is no longer a reviewed page")
        if other is None or not (other.reviewed and other.status == "stable"):
            raise Invalid(f"{other_id} is no longer a reviewed page")
        if kept.private != other.private:
            raise Invalid(f"{other_id} and {kept_id}: one is private, the other not")
        ops = [{"op": "merge_identity", "page": other.id, "canonical": kept.id}]
        return Merge(kept, other, reason, ops)

    def proposed(self, merge: Merge) -> None:
        """Its pages are in a merge proposal now: another pair with one of them is left out."""
        self.used |= {merge.kept.id, merge.other.id}
