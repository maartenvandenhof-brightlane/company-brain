"""What an ideas run reads. Two scopes:

recent (nightly, and after batch processing): the conversations filed since the last run,
and the pages they touched.
  changed   wiki pages written since `since` (GET /recent: the files of each commit)
  sources   the raw sources those pages cite (frontmatter `sources`): the conversations,
            batch-processed imports and chats filed live alike
  touched   the other pages citing those sources (their backlinks)

wiki (the Ideate button): the whole wiki, as far as one call allows: the most recently
updated pages, and the conversations they cite.

A raw source only counts once a page cites it, so importing an export (which only adds
raw sources) brings nothing in until it is processed. Transcripts are condensed like
for batch processing; everything is capped so one run stays one affordable model call.
Pages under wiki/inferred/ are marked inferred (and speculative when they say so): they are
conclusions, not facts. Observation pages and personal patterns (wiki/inferred/patterns/) are
never read: they are the patterns pass's alone.

Whatever the scope, a run also reads the owner's context (`owner_context`):
  goals      the active lines of ## Goals on wiki/owner/profile (not superseded), G1...
  loops      the open follow-ups (GET /open-loops) on reviewed pages and the profile, L1...
  briefing   the hot-memory briefing; while there is none, the profile, shortened
  dream      what the latest real dream changed: its log's Direct changes and Proposals
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wiki_mcp.dream import hot_memory
from wiki_mcp.dream.limits import MAX_CHARS, MAX_COMMITS, MAX_PAGES, SKIP_IDS, changed_pages
from wiki_mcp.dream.pages import OWNER_PAGE, read
from wiki_mcp.text import shorten
from wiki_mcp.wiki.facts import is_inference_id
from wiki_mcp.wiki.patterns import is_pattern_id
from wiki_mcp.wiki.store import markdown_section

from portal.dreams.store import DreamStore
from portal.ideas.progress import Progress
from portal.processing import condense
from portal.wikiclient import NotFound, WikiClient

MAX_SOURCES = 15
WIKI_SOURCES = 8  # whole-wiki runs: fewer transcripts, more pages
PAGE_CHARS = 3_000
SOURCE_CHARS = 8_000
BRIEFING_CHARS = 6_000
DREAM_CHARS = 3_000  # each of the dream log's two sections
SKIP_TYPES = {"guide", "proposal", "maintenance-report", "source", "briefing", "observation"}


@dataclass
class Item:
    id: str
    type: str
    title: str
    text: str
    inferred: bool = False  # under wiki/inferred/: a conclusion, not a fact
    speculative: bool = False


@dataclass
class Material:
    pages: list[Item] = field(default_factory=list)
    sources: list[Item] = field(default_factory=list)
    chars: int = 0

    @property
    def ids(self) -> set[str]:
        return {i.id for i in [*self.pages, *self.sources]}

    def __len__(self) -> int:
        return len(self.pages) + len(self.sources)

    def fits(self, text: str) -> bool:
        return self.chars + len(text) <= MAX_CHARS


def cited_sources(doc: dict[str, Any]) -> list[str]:
    """Ids (raw/...) of the bundle sources a page cites."""
    out = []
    for src in (doc.get("frontmatter") or {}).get("sources") or []:
        res = src.get("resource", "") if isinstance(src, dict) else ""
        if isinstance(res, str) and res.startswith("/raw/"):
            out.append(res[1:].removesuffix(".md"))
    return out


def source_text(body: str) -> str:
    """A transcript condensed (the owner's messages whole), other sources shortened."""
    transcript = condense.parse(body)
    if transcript.messages:
        chunks = condense.condense(transcript)
        return shorten(chunks[0], SOURCE_CHARS) if chunks else ""
    return shorten(body.strip(), SOURCE_CHARS)


def _item(doc: dict[str, Any], text: str) -> Item:
    fm = doc.get("frontmatter") or {}
    did = str(doc["id"])
    inferred = is_inference_id(did)
    return Item(
        did,
        str(fm.get("type") or ""),
        str(doc.get("title") or ""),
        text,
        inferred,
        inferred and fm.get("speculative") is True,
    )


async def _doc(wiki: WikiClient, did: str) -> dict[str, Any] | None:
    try:
        return await wiki.doc(did)
    except NotFound:  # renamed or removed since the commit
        return None


class _Reader:
    """Reads pages and sources into the material, within the caps, counting progress."""

    def __init__(self, wiki: WikiClient, progress: Progress | None) -> None:
        self.wiki = wiki
        self.progress = progress or Progress("recent", "")
        self.material = Material()
        self.docs: dict[str, dict[str, Any]] = {}
        self.source_ids: list[str] = []

    async def page(self, did: str) -> None:
        if len(self.material.pages) >= MAX_PAGES or did in self.docs or is_pattern_id(did):
            return
        doc = await _doc(self.wiki, did)
        self.progress.read()
        if doc is None:
            return
        self.docs[did] = doc
        fm = doc.get("frontmatter") or {}
        if fm.get("type") in SKIP_TYPES:
            return
        head = str(fm.get("description") or "")
        text = shorten(f"{head}\n\n{doc.get('body', '')}".strip(), PAGE_CHARS)
        if self.material.fits(text):
            self.material.pages.append(_item(doc, text))
            self.material.chars += len(text)
        self.source_ids.extend(s for s in cited_sources(doc) if s not in self.source_ids)

    async def sources(self, limit: int, *, touched: bool) -> None:
        """The cited sources; with `touched`, also the other pages citing each."""
        todo = self.source_ids[:limit]
        self.progress.expect(len(todo))
        for sid in todo:
            doc = await _doc(self.wiki, sid)
            self.progress.read()
            if doc is None:
                continue
            text = source_text(str(doc.get("body", "")))
            if text and self.material.fits(text):
                self.material.sources.append(_item(doc, text))
                self.material.chars += len(text)
            if not touched:
                continue
            links = [
                str(b["id"])
                for b in doc.get("backlinks") or []
                if isinstance(b, dict) and str(b.get("id", "")).startswith("wiki/")
            ]
            new = [i for i in links if i not in self.docs]
            self.progress.expect(min(len(new), MAX_PAGES - len(self.material.pages)))
            for did in new:
                await self.page(did)


async def collect(wiki: WikiClient, since: str, progress: Progress | None = None) -> Material:
    """The conversations filed since `since`, and the pages they touched."""
    reader = _Reader(wiki, progress)
    changed = changed_pages(await wiki.recent(MAX_COMMITS, since=since))[:MAX_PAGES]
    reader.progress.expect(len(changed))
    for did in changed:
        await reader.page(did)
    if reader.material.pages:  # nothing filed since: no sources either
        await reader.sources(MAX_SOURCES, touched=True)
    return reader.material


async def collect_wiki(wiki: WikiClient, progress: Progress | None = None) -> Material:
    """The most recently updated pages of the whole wiki, and the conversations they cite."""
    reader = _Reader(wiki, progress)
    docs = [
        d
        for d in await wiki.docs()
        if str(d.get("id", "")).startswith("wiki/")
        and d.get("type") not in SKIP_TYPES
        and d.get("id") not in SKIP_IDS
        and not is_pattern_id(str(d.get("id", "")))
    ]
    docs.sort(key=lambda d: str(d.get("updated") or ""), reverse=True)
    chosen = [str(d["id"]) for d in docs[:MAX_PAGES]]
    reader.progress.expect(len(chosen))
    for did in chosen:
        await reader.page(did)
    await reader.sources(WIKI_SOURCES, touched=False)
    return reader.material


@dataclass
class Context:
    """The owner's side of a run: what he wants, what is open, and what changed lately."""

    goals: list[str] = field(default_factory=list)  # G1...: the lines as on the profile
    loops: list[dict[str, Any]] = field(default_factory=list)  # L1...: {page, line, date}
    briefing: str = ""
    briefing_is_profile: bool = False  # no hot memory yet: the profile stands in for it
    dream: str = ""  # what the latest real dream changed, or ""

    def serves(self, kind: str, ref: str) -> str | None:
        """The text of goal Gn or loop Ln, or None when there is no such one."""
        prefix, items = ("G", self.goals) if kind == "goal" else ("L", self.loop_lines)
        if not ref.startswith(prefix) or not ref[1:].isdigit():
            return None
        n = int(ref[1:])
        return plain(items[n - 1]) if 1 <= n <= len(items) else None

    @property
    def loop_lines(self) -> list[str]:
        return [str(lp.get("line") or "") for lp in self.loops]


def plain(line: str) -> str:
    """A goal or loop line without its bullet or checkbox."""
    text = line.strip().lstrip("-*+ ").strip()
    return text[3:].strip() if text[:3] in ("[ ]", "[x]", "[X]") else text


async def owner_context(wiki: WikiClient, dreams: DreamStore | None) -> Context:
    ctx = Context()
    profile = await read(wiki, OWNER_PAGE)
    ctx.goals = hot_memory.active_goals(profile)
    ctx.loops = [
        lp
        for lp in await wiki.open_loops()
        if lp.get("page") == OWNER_PAGE or lp.get("reviewed") is True
    ]
    briefing = await read(wiki, hot_memory.PAGE)
    if briefing is not None and briefing.body.strip():
        ctx.briefing = shorten(briefing.body.strip(), BRIEFING_CHARS)
    elif profile is not None:
        ctx.briefing = shorten(profile.body.strip(), BRIEFING_CHARS)
        ctx.briefing_is_profile = True
    last = dreams.last(real=True) if dreams is not None else None
    if last is not None and last.log_page:
        page = await read(wiki, last.log_page.strip("/").removesuffix(".md"))
        if page is not None:
            sections = [
                markdown_section(page.body, heading) for heading in ("Direct changes", "Proposals")
            ]
            ctx.dream = "\n\n".join(
                shorten(s.strip(), DREAM_CHARS) for s in sections if s and s.strip()
            )
    return ctx
