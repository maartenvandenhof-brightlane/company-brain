"""The pages dreams read: reviewed pages (status stable, without the from-chat-import tag,
the same rule wiki-mcp enforces on every dream write), read whole through a WikiReader.
Dreams see private pages too; the material marks them `private="true"`, and what a pass
derives from one goes only onto private pages (wiki-mcp enforces the rest)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from wiki_mcp.dream.reader import WikiReader
from wiki_mcp.wiki.store import WikiStore

OWNER_PAGE = "wiki/owner/profile"
# Written by the server (or a dream) only: never material for a pass. Observations are the
# patterns pass's evidence only (dream/patterns.py).
MANAGED = {
    "source",
    "guide",
    "proposal",
    "maintenance-report",
    "inference",
    "briefing",
    "observation",
}


@dataclass
class Page:
    id: str
    path: str
    type: str
    title: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def status(self) -> str:
        return str(self.frontmatter.get("status") or "stable")

    @property
    def private(self) -> bool:
        return self.frontmatter.get("private") is True

    @property
    def reviewed(self) -> bool:
        return WikiStore.is_reviewed(self.frontmatter)

    @property
    def sources(self) -> list[str]:
        """The raw source ids in frontmatter `sources`."""
        out = []
        for src in self.frontmatter.get("sources") or []:
            res = src.get("resource", "") if isinstance(src, dict) else ""
            if isinstance(res, str) and res.startswith("/raw/"):
                out.append(res[1:].removesuffix(".md"))
        return out


def page_of(doc: dict[str, Any]) -> Page:
    fm = doc.get("frontmatter") or {}
    return Page(
        str(doc["id"]),
        str(doc.get("path") or doc["id"] + ".md"),
        str(fm.get("type") or ""),
        str(doc.get("title") or doc["id"]),
        fm,
        str(doc.get("body") or ""),
    )


async def read(wiki: WikiReader, did: str) -> Page | None:
    try:
        doc = await wiki.doc(did)
    except LookupError:  # no such page
        return None
    return page_of(doc)


async def reviewed_pages(wiki: WikiReader) -> list[Page]:
    """Every reviewed page of the types agents write, newest first."""
    summaries = [
        d
        for d in await wiki.docs()
        if str(d.get("id", "")).startswith("wiki/")
        and d.get("type") not in MANAGED
        and d.get("status") in (None, "stable")  # no status means stable
    ]
    summaries.sort(key=lambda d: str(d.get("updated") or ""), reverse=True)
    pages = []
    for d in summaries:
        if "from-chat-import" in (d.get("tags") or []):
            continue
        page = await read(wiki, str(d["id"]))
        if page is not None and page.reviewed:
            pages.append(page)
    return pages


_WORD = re.compile(r"[a-zà-ÿ]+")
_NL = frozenset(
    "de het een en van is dat niet op te voor met zijn ik je wil ook maar om naar bij hij zij "  # noqa: SIM905
    "wordt worden als nog er wat die deze was heeft hebben door over uit kan moet".split()
)
_EN = frozenset(
    "the a an and of is that not on to for with be i you want also but at he she as it what "  # noqa: SIM905
    "this was has have by about from can must will".split()
)


def lang(text: str) -> str:
    """ "nl" or "en": whichever language's common words the text uses more (ties: en)."""
    words = _WORD.findall(text.lower())
    nl = sum(w in _NL for w in words)
    en = sum(w in _EN for w in words)
    return "nl" if nl > en else "en"


def numbered(page: Page) -> tuple[str, dict[str, str]]:
    """A page for a pass's material, every body line numbered `L1: …`, and the lines by
    number, so the model's references (page id and line number) resolve to exact text."""
    lines = page.body.split("\n")
    by_number = {f"L{i}": line for i, line in enumerate(lines, 1)}
    mark = ' private="true"' if page.private else ""
    head = (
        f'<page id="{page.id}" type="{page.type}" status="{page.status}" '
        f'lang="{lang(page.body)}"{mark}>'
    )
    body = "\n".join(f"{n}: {line}" for n, line in by_number.items())
    return f"{head}\n{body}\n</page>", by_number
