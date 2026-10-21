"""Turn the extractions of one conversation into what wiki-mcp's POST /file writes.

Deterministic apart from one optional small call: entities are matched to existing pages
(the id the model picked, else the same title), entities that only nearly match a page
("Jane" and "Jane Doe") are decided together in one call, claims only the assistant made
are dropped, and every kept claim becomes a dated line citing the conversation. Pages the
conversation mentions together are linked: relations both ways, decisions to their
subject, and the first mention of another page's title in a line.

Private pages (`private: true`, only for clients with the wiki:private scope) never leak into
the others: a page is private when its entity is, when it is a life event, when a private
claim lands on it or when it already was. The owner's private facts go on their private page,
not the profile. A non-private page links no private page, and a relation or decision between
a private and a non-private page is only written on the private side.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from wiki_mcp.wiki.paths import slugify

from portal.processing.catalogue import OWNER_PAGE, PERSONAL_PAGE, Catalogue, Page, norm
from portal.processing.extract import LIFE_EVENT, Decision, Entity, Extraction
from portal.processing.llm import LLM, LLMError

OWNER_TITLE = "Owner profile"
OWNER_DESCRIPTION = "The owner: preferences, background and goals, as learned from conversations."
PERSONAL_TITLE = "Personal life"
PERSONAL_DESCRIPTION = (
    "The owner's personal life: relationships, feelings, values and reflections, "
    "as learned from conversations."
)


@dataclass
class Draft:
    """One page to file: existing (only sections are added) or new."""

    page: Page
    new: bool
    sections: dict[str, list[str]] = field(default_factory=dict)
    related: list[str] = field(default_factory=list)
    private: bool = False

    def add(self, heading: str, line: str) -> None:
        lines = self.sections.setdefault(heading, [])
        if line not in lines:
            lines.append(line)

    def relate(self, other: str) -> None:
        if other != self.page.id and other not in self.related:
            self.related.append(other)

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.page.id,
            "sections": [{"heading": h, "lines": ls} for h, ls in self.sections.items()],
            "related": self.related,
        }
        if self.new:
            out |= {
                "type": self.page.type,
                "title": self.page.title,
                "description": self.page.description or self.page.title,
            }
        if self.private:
            out["private"] = True  # a new page is created private, an existing one marked
        return out


@dataclass
class Conversation:
    sources: list[str]  # the raw source ids of its parts, in order
    title: str
    started: str  # YYYY-MM-DD or ""


@dataclass
class Plan:
    payload: dict[str, Any]
    drafts: list[Draft]
    dropped: int = 0  # assistant-only claims left out


def merge(chunks: list[Extraction]) -> Extraction:
    """One extraction from the chunks of a long transcript (keys made unique per chunk)."""
    if len(chunks) == 1:
        return chunks[0]
    out = Extraction()
    skips = [c.skip for c in chunks if c.skip]
    out.private_conversation = any(c.private_conversation for c in chunks)
    for i, c in enumerate(chunks):
        p = f"c{i}:"
        out.entities += [replace(e, key=p + e.key) for e in c.entities]
        out.facts += [replace(f, entity=p + f.entity) for f in c.facts]
        for d in c.decisions:
            d.entity = p + d.entity if d.entity else None
            out.decisions.append(d)
        for r in c.relations:
            r.source, r.target = p + r.source, p + r.target
            out.relations.append(r)
    out.skip = skips[0] if len(skips) == len(chunks) else None
    return out


class Resolver:
    """Where each extracted entity is filed: an existing page or a new one."""

    def __init__(self, catalogue: Catalogue) -> None:
        self.cat = catalogue
        self.taken: set[str] = set()

    def existing(self, e: Entity) -> Page | None:
        if e.ref == PERSONAL_PAGE:
            return self.personal()
        # The owner named as a person (their profile's title or an alias, exactly) is the
        # profile, never a new page under wiki/people (wiki-mcp refuses one too).
        owner_named = e.type == "person" and self.cat.is_owner(e.title)
        if e.ref == OWNER_PAGE or e.type == "owner" or owner_named:
            return self.cat.pages.get(OWNER_PAGE) or Page(
                OWNER_PAGE, "owner", OWNER_TITLE, OWNER_DESCRIPTION
            )
        if (page := self.cat.page(e.ref)) is not None:
            return page  # a merged duplicate's id leads to its canonical page
        return self.cat.by_title(e.title, e.type)

    def personal(self) -> Page:
        return self.cat.pages.get(PERSONAL_PAGE) or Page(
            PERSONAL_PAGE, "owner", PERSONAL_TITLE, PERSONAL_DESCRIPTION, private=True
        )

    def new(self, e: Entity) -> Page | None:
        folder = self.cat.folders.get(e.type)
        if not folder:
            return None
        base = f"{folder}/{slugify(norm(e.title))}"
        path, n = base, 2
        while path in self.cat.pages or path in self.cat.redirects or path in self.taken:
            path, n = f"{base}-{n}", n + 1
        self.taken.add(path)
        return Page(path, e.type, e.title, e.description)


async def _same_as(llm: LLM, model: str, pairs: list[tuple[Entity, Page]]) -> set[int]:
    """Which near matches are the same entity (numbers into `pairs`), in one call."""
    listed = "\n".join(
        f"{i}. New: {e.title} ({e.type}): {e.description}\n   Existing: {p.title} ({p.id}): {p.description}"
        for i, (e, p) in enumerate(pairs, 1)
    )
    prompt = (
        "For each numbered pair, decide whether the new entity from a conversation is the same "
        "real-world person, organization or thing as the existing wiki page. Answer JSON: "
        '{"same": [numbers of the pairs that are the same]}. When unsure, leave it out.\n\n'
        + listed
    )
    try:
        data = json.loads(await llm.json_chat(model, [{"role": "user", "content": prompt}]))
    except (LLMError, ValueError):
        return set()
    same = data.get("same") if isinstance(data, dict) else None
    return {i - 1 for i in same or [] if isinstance(i, int) and 1 <= i <= len(pairs)}


def _link(text: str, targets: dict[str, Page], own: str) -> str:
    """The first mention of each other page's title in `text` as a markdown link."""
    for page in sorted(targets.values(), key=lambda p: -len(p.title)):
        if page.id == own or len(page.title) < 3 or f"(/{page.id}.md)" in text:
            continue
        pattern = re.compile(rf"(?<![\w\[/]){re.escape(page.title)}(?![\w\]])", re.I)
        text = pattern.sub(_linker(f"/{page.id}.md"), text, count=1)
    return text


def _linker(href: str) -> Callable[[re.Match[str]], str]:
    def link(m: re.Match[str]) -> str:
        return f"[{m.group(0)}]({href})"

    return link


def _fact_line(section: str, as_of: str, text: str, cite: str) -> str:
    if section == "Follow-ups":
        return f"- [ ] {text} (as of {as_of}) {cite}"
    return f"- {as_of}: {text} {cite}"


async def plan(
    conv: Conversation,
    ex: Extraction,
    cat: Catalogue,
    llm: LLM,
    model: str,
    reason: str,
) -> Plan:
    first = conv.sources[0]
    fid = first.rsplit("/", 1)[-1]
    cite = f"[^{fid}]"
    footnote = f"[^{fid}]: [{conv.title}](/{first}.md)" + (
        f", conversation of {conv.started}" if conv.started else ""
    )
    # A private conversation's sources become private: a non-private page citing it must not
    # show its title (as the server's skip line does not).
    neutral = f"[^{fid}]: [Private conversation](/{first}.md)"
    kept = [f for f in ex.facts if f.basis != "assistant"]
    decisions = [d for d in ex.decisions if d.basis != "assistant"]
    dropped = len(ex.facts) - len(kept) + len(ex.decisions) - len(decisions)
    base: dict[str, Any] = {"sources": conv.sources, "reason": reason}
    if ex.private_conversation:
        base["private_sources"] = True
    if not kept and not decisions:
        why = ex.skip or ("only assistant suggestions" if dropped else "nothing to file")
        return Plan({**base, "skip": why, "pages": []}, [], dropped)

    # Which entities are used, and where each is filed.
    used = {f.entity for f in kept} | {d.entity for d in decisions if d.entity}
    used |= {k for r in ex.relations for k in (r.source, r.target)}
    resolver = Resolver(cat)
    target: dict[str, Page] = {}
    by_title: dict[str, Page] = {}
    pending: list[tuple[Entity, Page]] = []
    for e in ex.entities:
        if e.key not in used:
            continue
        same_here = by_title.get(norm(e.title))
        page = same_here or resolver.existing(e)
        if page is None and (near := cat.near(e.title, e.type)):
            pending.append((e, near[0]))
            continue
        page = page or resolver.new(e)
        if page is not None:
            target[e.key] = by_title.setdefault(norm(e.title), page)
    if pending:
        same = await _same_as(llm, model, pending)
        for i, (e, near_page) in enumerate(pending):
            page = near_page if i in same else by_title.get(norm(e.title)) or resolver.new(e)
            if page is not None:
                target[e.key] = by_title.setdefault(norm(e.title), page)

    # Which pages are private, before any line is written (lines link only what they may).
    entity = {e.key: e for e in ex.entities}
    marked: set[str] = set()

    def private(page: Page) -> bool:
        return (
            page.private or page.id in marked or page.type == LIFE_EVENT or page.id == PERSONAL_PAGE
        )

    def secret(key: str | None) -> bool:
        return key is not None and key in entity and entity[key].private

    def home(key: str | None, is_private: bool) -> Page | None:
        """Where a claim about this entity goes: the owner's private claims on their private page."""
        page = target.get(key) if key else None
        if page is not None and page.id == OWNER_PAGE and is_private:
            return resolver.personal()
        return page

    for e in ex.entities:
        if e.private and (page := target.get(e.key)) is not None and page.id != OWNER_PAGE:
            marked.add(page.id)
    facts: list[tuple[Page, str, str, str]] = []  # page, section, text, as_of
    for f in kept:
        hidden = f.private or secret(f.entity)
        if (page := home(f.entity, hidden)) is not None:
            if hidden:
                marked.add(page.id)
            facts.append((page, f.section, f.text, f.as_of))
    decision_type = cat.folders.get("decision")
    filed: list[tuple[Decision, Page | None, Page | None]] = []  # decision, its page, subject
    for d in decisions:
        hidden = d.private or secret(d.entity)
        subject = home(d.entity, hidden)
        hidden = hidden or (subject is not None and private(subject))
        if decision_type is None:
            if subject is not None and hidden:
                marked.add(subject.id)  # its line lands on the subject
            filed.append((d, None, subject))
            continue
        page = cat.by_title(d.title, "decision") or resolver.new(
            Entity("", None, "decision", d.title, d.outcome or d.title)
        )
        assert page is not None
        if hidden:
            marked.add(page.id)
        filed.append((d, page, subject))

    drafts: dict[str, Draft] = {}

    def draft(page: Page) -> Draft:
        if page.id not in drafts:
            drafts[page.id] = Draft(page, new=page.id not in cat.pages)
        return drafts[page.id]

    everyone = {p.id: p for p in target.values()}
    public = {i: p for i, p in everyone.items() if not private(p)}

    def link(text: str, page: Page) -> str:
        return _link(text, everyone if private(page) else public, page.id)

    for page, section, text, as_of in facts:
        draft(page).add(section, _fact_line(section, as_of, link(text, page), cite))
    for d, dpage, subject in filed:
        if dpage is None:
            if subject is not None:
                draft(subject).add("Decisions", f"- {d.as_of}: {d.title}: {d.outcome} {cite}")
            continue
        dd = draft(dpage)
        if d.context:
            dd.add("Context", f"{link(d.context, dpage)} {cite}")
        for option in d.options:
            dd.add("Options considered", f"- {option}")
        if d.outcome:
            dd.add("Outcome", f"- {d.as_of}: {link(d.outcome, dpage)} {cite}")
        if d.rationale:
            dd.add("Rationale", f"- {link(d.rationale, dpage)} {cite}")
        if subject is not None:
            if private(dpage) or not private(subject):
                dd.relate(subject.id)
            if private(subject) or not private(dpage):
                sd = draft(subject)
                sd.relate(dpage.id)
                sd.add("Decisions", f"- {d.as_of}: [{d.title}](/{dpage.id}.md) {cite}")
    for r in ex.relations:
        a, b = target.get(r.source), target.get(r.target)
        if a is None or b is None or a.id == b.id:
            continue
        # The owner's side of a private relation is their private page.
        if a.id == OWNER_PAGE and private(b):
            a = resolver.personal()
        if b.id == OWNER_PAGE and private(a):
            b = resolver.personal()
        if private(a) or not private(b):
            draft(a).relate(b.id)
            draft(a).add("Related", f"- {r.relation}: [{b.title}](/{b.id}.md)")
        if private(b) or not private(a):
            draft(b).relate(a.id)
            draft(b).add("Related", f"- [{a.title}](/{a.id}.md) ({r.relation})")
    for each in drafts.values():
        each.private = private(each.page)
    # Anything filed privately makes the transcript private too: its private parts are
    # readable there verbatim otherwise.
    if any(each.private for each in drafts.values()):
        base["private_sources"] = True
    for each in drafts.values():
        hide = "private_sources" in base and not each.private
        each.add("Sources", neutral if hide else footnote)
    return Plan(
        {**base, "skip": None, "pages": [d.payload() for d in drafts.values()]},
        list(drafts.values()),
        dropped,
    )
