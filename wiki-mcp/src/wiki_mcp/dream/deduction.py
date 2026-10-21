"""D2, deduction: what follows from the stated lines of the pages that changed. Proposals only.

  read      the reviewed pages changed since the base commit of the last dream that could
            write (GET /recent, newest first down to that commit; every reviewed page when
            there was none), then the reviewed pages they link to, within 40 pages and
            60,000 characters; every body line numbered (pages.numbered)
  ask       the strong model (the cheap one when the budget says so) for lines a newer line
            supersedes, conflicts to confirm, and implications
  check     every reference (page id and L<n>) resolves to a stated line: a bullet with a
            footnote citing a raw source, on a reviewed page of the material, not marked
            superseded yet
              supersede     the newer line is dated strictly later (below); a one-line reason
              conflicts     on a page of the material, two or more stated lines, a follow-up
              implications  a title, a statement, two or more premises, `about` known pages
            at most 10 of each kind; whatever fails is dropped and why goes into the log
  propose   one proposal per page with all its superseded lines and follow-ups (a dream
            proposal is refused once its page changed, so one page is never in two), and
            one per implication: a new inference page of kind deduction (inference.py)

The asking and proposing are the caller's (the portal's pass in portal/dreams/deduction.py):
this module has the material, the answer's parse and the checks, and makes the proposals.

What comes from a private page stays on private pages: a line superseded by one on a private
page, or a follow-up for a conflict involving one, must be on a private page too. An
implication resting on a private page becomes a private inference (wiki-mcp marks it).

A line's date is its leading `YYYY-MM[-DD]:`, else the date of the raw source its footnote
cites (the transcript's last message, else its creation). Later means later at the precision
both dates have: 2026-03-15 is not later than 2026-03, and an undated line is never later.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any

from wiki_mcp.dream import Unparsable, inference
from wiki_mcp.dream.hygiene import raw_ids_by_stem
from wiki_mcp.dream.limits import MAX_CHARS, MAX_COMMITS, MAX_PAGES, changed_pages
from wiki_mcp.dream.pages import Page, numbered, reviewed_pages
from wiki_mcp.dream.reader import WikiReader
from wiki_mcp.wiki.facts import footnotes, is_inference_id, is_stated, line_date, superseded

NAME = "deduction"
KINDS = ("supersede", "conflicts", "implications")
MOST = 10  # items of each kind considered
REASON_CHARS = 300
FOLLOWUP_CHARS = 400
STATEMENT_CHARS = 1000
TITLE_CHARS = (3, 120)
NOTES_CHARS = 300
FOLLOW_UPS = "Follow-ups"

_DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
_NUMBER = re.compile(r"^L?(\d{1,5})$")


class Invalid(ValueError):
    """An item of the answer fails a check; the message says why."""


@dataclass(frozen=True)
class Stated:
    """A stated line the model referred to, resolved to its exact text."""

    page: Page
    number: str  # "L12"
    text: str  # the line as it is on the page
    source: str  # the raw source id its (first resolvable) footnote cites

    @property
    def ref(self) -> str:
        return f"{self.page.id} {self.number}"


@dataclass
class Material:
    pages: dict[str, Page] = field(default_factory=dict)  # id -> page, in the material
    lines: dict[str, dict[str, str]] = field(default_factory=dict)  # id -> {"L1": line}
    blocks: list[str] = field(default_factory=list)
    chars: int = 0
    max_pages: int = MAX_PAGES
    max_chars: int = MAX_CHARS

    def add(self, page: Page) -> bool:
        """Add a page if it fits the caps; False when it does not."""
        if page.id in self.pages:
            return True
        block, by_number = numbered(page)
        if len(self.pages) >= self.max_pages or self.chars + len(block) > self.max_chars:
            return False
        self.pages[page.id] = page
        self.lines[page.id] = by_number
        self.blocks.append(block)
        self.chars += len(block)
        return True

    @property
    def text(self) -> str:
        return "<material>\n" + "\n".join(self.blocks) + "\n</material>"


# --- dates ---------------------------------------------------------------------------


def later(a: str | None, b: str | None) -> bool:
    """Whether date `a` is strictly later than `b`, compared at the precision both have
    (YYYY-MM against YYYY-MM-DD compares the months). An undated side is never later."""
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return a[:n] > b[:n]


class Dates:
    """Line dates: the line's own, else its raw source's (each source read once)."""

    def __init__(self, wiki: WikiReader, raw: dict[str, str]) -> None:
        self.wiki = wiki
        self.raw = raw  # footnote id -> raw source id
        self._sources: dict[str, str | None] = {}

    def source(self, line: str) -> str | None:
        """The raw source id of the first footnote that names exactly one."""
        return next((self.raw[f] for f in footnotes(line) if f in self.raw), None)

    async def of(self, stated: Stated) -> str | None:
        own = line_date(stated.text)
        if own is not None:
            return own
        if stated.source not in self._sources:
            self._sources[stated.source] = await self._source_date(stated.source)
        return self._sources[stated.source]

    async def _source_date(self, rid: str) -> str | None:
        try:
            fm = (await self.wiki.doc(rid)).get("frontmatter") or {}
        except LookupError:  # no such source
            return None
        for key in ("last_message_at", "created"):
            value = str(fm.get(key) or "")[:10]
            if _DATE.match(value):
                return value
        return None


# --- references ----------------------------------------------------------------------


def page_ref(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Invalid("a reference without a page id")
    return value.strip().strip("/").removesuffix(".md")


def resolve(page: Any, line: Any, material: Material, dates: Dates) -> Stated:
    """The stated line a reference (page id, "L<n>") points at; Invalid saying why not."""
    pid = page_ref(page)
    if pid not in material.pages or is_inference_id(pid):
        raise Invalid(f"{pid[:100]} is not a reviewed page in the material")
    m = _NUMBER.match(str(line).strip().upper()) if isinstance(line, str | int) else None
    number = f"L{int(m.group(1))}" if m else ""
    text = material.lines[pid].get(number)
    if text is None:
        raise Invalid(f"{pid} has no line {str(line)[:20]}")
    if not is_stated(text):
        raise Invalid(f"{pid} {number} is not a stated line: {text.strip()[:100]}")
    if superseded(text) is not None:
        raise Invalid(f"{pid} {number} is already marked superseded")
    source = dates.source(text)
    if source is None:
        raise Invalid(f"{pid} {number} cites no raw source: {text.strip()[:100]}")
    return Stated(material.pages[pid], number, text, source)


def resolve_all(refs: Any, material: Material, dates: Dates, what: str) -> list[Stated]:
    """Two or more distinct stated lines from a list of {page, line}."""
    if not isinstance(refs, list):
        raise Invalid(f"{what} must be a list of line references")
    out: list[Stated] = []
    for ref in refs:
        if not isinstance(ref, dict):
            raise Invalid(f"{what}: each is {{page, line}}")
        stated = resolve(ref.get("page"), ref.get("line"), material, dates)
        if all(s.ref != stated.ref for s in out):
            out.append(stated)
    if len(out) < 2:
        raise Invalid(f"{what}: fewer than two distinct stated lines")
    return out


def one_line(value: Any, what: str, most: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Invalid(f"no {what}")
    if "\n" in value.strip():
        raise Invalid(f"the {what} is not a single line")
    text = " ".join(value.split())
    if len(text) > most:
        raise Invalid(f"the {what} is over {most} characters")
    return text


# --- the answer ------------------------------------------------------------------------


@dataclass
class Answer:
    items: dict[str, list[Any]]
    notes: str = ""


def parse(answer: str) -> Answer:
    """The answer's lists; Unparsable when it is not a JSON object with at least one of them."""
    try:
        data = json.loads(answer)
    except ValueError as exc:
        raise Unparsable(f"not JSON: {exc}") from exc
    if not isinstance(data, dict) or not any(isinstance(data.get(k), list) for k in KINDS):
        raise Unparsable("the answer must be a JSON object with supersede, conflicts, implications")
    items = {k: v if isinstance(v := data.get(k), list) else [] for k in KINDS}
    notes = data.get("notes")
    text = "; ".join(str(n) for n in notes) if isinstance(notes, list) else ""
    return Answer(items, " ".join(text.split())[:NOTES_CHARS])


@dataclass
class PageChange:
    """What one page's proposal holds."""

    page: Page
    ops: list[dict[str, Any]] = field(default_factory=list)
    why: list[str] = field(default_factory=list)
    superseded: int = 0
    conflicts: int = 0


@dataclass
class Proposal:
    summary: str
    reason: str
    ops: list[dict[str, Any]]


@dataclass
class Plan:
    pages: dict[str, PageChange] = field(default_factory=dict)
    inferences: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)  # "kind: why", for each item left out

    def change(self, page: Page) -> PageChange:
        return self.pages.setdefault(page.id, PageChange(page))

    def proposals(self, dream: int) -> list[Proposal]:
        """One proposal per page with all its changes, then one per implication."""
        out = []
        for change in self.pages.values():
            parts = []
            if change.superseded:
                parts.append(_count(change.superseded, "superseded line"))
            if change.conflicts:
                parts.append(_count(change.conflicts, "conflict") + " to confirm")
            summary = f"Deduction on {change.page.title}: {', '.join(parts)}"
            reason = f"Dream {dream} deduction on {change.page.id}: " + "; ".join(change.why)
            out.append(Proposal(summary, reason, change.ops))
        for title, why, op in self.inferences:
            reason = f"Dream {dream} deduction: {title} {why}"
            out.append(Proposal(f"Deduction: {title}", reason, [op]))
        return out


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


async def check(
    answer: Answer,
    material: Material,
    dates: Dates,
    known: set[str],
    taken: set[str],
    dream: int,
    at: dt.datetime,
) -> Plan:
    """The operations the answer's items pass the checks for; why the others were dropped."""
    plan = Plan()
    today, month = at.strftime("%Y-%m-%d"), at.strftime("%Y-%m")
    for kind in KINDS:
        items = answer.items[kind]
        if len(items) > MOST:
            plan.dropped.append(f"{kind}: {len(items) - MOST} over the limit of {MOST}")
        for item in items[:MOST]:
            try:
                if not isinstance(item, dict):
                    raise Invalid("not an object")
                if kind == "supersede":
                    await _supersede(item, material, dates, plan, today)
                elif kind == "conflicts":
                    _conflict(item, material, dates, plan, month)
                else:
                    _implication(item, material, dates, plan, known, taken, dream, at)
            except Invalid as exc:
                plan.dropped.append(f"{kind}: {exc}")
    return plan


async def review(
    wiki: WikiReader, answer: Answer, material: Material, dream: int, at: dt.datetime
) -> Plan:
    """check() with what it needs from the wiki's index: the pages `about` may name, the ids
    in use, and the raw sources footnotes cite."""
    docs = (await wiki.index()).get("docs", [])
    ids = {str(d.get("id", "")) for d in docs}
    known = {i for i in ids if i.startswith("wiki/") and not is_inference_id(i)}
    dates = Dates(wiki, raw_ids_by_stem(docs))
    return await check(answer, material, dates, known, set(ids), dream, at)


async def _supersede(
    item: dict[str, Any], material: Material, dates: Dates, plan: Plan, today: str
) -> None:
    old = resolve(item.get("page"), item.get("line"), material, dates)
    new = resolve(item.get("by_page"), item.get("by_line"), material, dates)
    if old.ref == new.ref:
        raise Invalid(f"{old.ref} would supersede itself")
    if new.page.private and not old.page.private:
        raise Invalid(f"{old.ref} is not private, {new.ref} is: the reason would show it")
    reason = one_line(item.get("reason"), "reason", REASON_CHARS)
    was, now = await dates.of(old), await dates.of(new)
    if not later(now, was):
        raise Invalid(
            f"{new.ref} ({now or 'undated'}) is not dated later than {old.ref} ({was or 'undated'})"
        )
    change = plan.change(old.page)
    if any(op["op"] == "supersede" and op["line"] == old.text for op in change.ops):
        raise Invalid(f"{old.ref} is superseded twice")
    change.ops.append(
        {"op": "supersede", "page": old.page.id, "line": old.text, "date": today, "reason": reason}
    )
    change.why.append(f"{old.number} superseded by {new.ref}: {reason}")
    change.superseded += 1


def _conflict(
    item: dict[str, Any], material: Material, dates: Dates, plan: Plan, month: str
) -> None:
    pid = page_ref(item.get("page"))
    if pid not in material.pages:
        raise Invalid(f"{pid[:100]} is not a reviewed page in the material")
    lines = resolve_all(item.get("lines"), material, dates, "a conflict's lines")
    page = material.pages[pid]
    if not page.private and any(s.page.private for s in lines):
        raise Invalid(f"{pid} is not private and the conflict involves a private page")
    followup = one_line(item.get("followup"), "follow-up", FOLLOWUP_CHARS)
    change = plan.change(page)
    change.ops.append(
        {
            "op": "append",
            "page": pid,
            "section": FOLLOW_UPS,
            "line": f"- [ ] {followup} (as of {month})",
        }
    )
    change.why.append(f"conflict between {', '.join(s.ref for s in lines)}")
    change.conflicts += 1


def _implication(
    item: dict[str, Any],
    material: Material,
    dates: Dates,
    plan: Plan,
    known: set[str],
    taken: set[str],
    dream: int,
    at: dt.datetime,
) -> None:
    title = one_line(item.get("title"), "title", TITLE_CHARS[1])
    if len(title) < TITLE_CHARS[0]:
        raise Invalid(f"the title is under {TITLE_CHARS[0]} characters")
    statement = item.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        raise Invalid(f"{title}: no statement")
    if len(statement.strip()) > STATEMENT_CHARS:
        raise Invalid(f"{title}: the statement is over {STATEMENT_CHARS} characters")
    try:
        premises = resolve_all(item.get("premises"), material, dates, "premises")
    except Invalid as exc:
        raise Invalid(f"{title}: {exc}") from exc
    listed = item.get("about")
    ids = [
        a.strip().strip("/").removesuffix(".md") if isinstance(a, str) else ""
        for a in (listed if isinstance(listed, list) else [])
    ]
    about = list(dict.fromkeys(i for i in ids if i in known))
    unknown = [i or "?" for i in ids if i not in known]
    if unknown:
        plan.dropped.append(
            f"implications: {title}: unknown pages left out of 'about': " + ", ".join(unknown)[:200]
        )
    op = inference.write_op(
        title=title,
        statement=statement,
        kind="deduction",
        supports=[inference.Evidence(p.page.id, p.page.title, p.text, p.source) for p in premises],
        about=about or list(dict.fromkeys(p.page.id for p in premises)),
        dream=dream,
        taken=taken,
        at=at,
    )
    why = f"follows from {', '.join(p.ref for p in premises)}"
    plan.inferences.append((title, why, op))


# --- material ------------------------------------------------------------------------


async def changed_since(wiki: WikiReader, base: str) -> list[str]:
    """Page ids written since the commit `base` (newest first; everything in the last
    MAX_COMMITS commits when it is further back)."""
    commits = []
    for c in await wiki.recent(MAX_COMMITS):
        if str(c.get("commit", ""))[:12] == base[:12]:
            break
        commits.append(c)
    return changed_pages(commits)


async def material(wiki: WikiReader, since_base: str | None) -> Material:
    """The reviewed pages changed since the commit `since_base` (every reviewed page when
    None: there was no dream that could write), then the reviewed pages they link to."""
    reviewed = await reviewed_pages(wiki)  # newest first
    by_id = {p.id: p for p in reviewed}
    if since_base is None:
        changed = reviewed
    else:
        changed = [by_id[i] for i in await changed_since(wiki, since_base) if i in by_id]
    out = Material()
    for page in changed:
        if not out.add(page):
            break
    for page in list(out.pages.values()):
        try:
            links = (await wiki.doc(page.id)).get("outlinks") or []
        except LookupError:  # gone since
            continue
        for target in links:
            linked = by_id.get(str(target))
            if linked is not None and not out.add(linked):
                return out
    return out
