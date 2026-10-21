"""The wiki's pages as batch processing sees them: to offer an extraction call the pages a
transcript is likely about (so it links to them instead of inventing duplicates), and to
match new entities against existing pages by title or alias.

A duplicate merged into another page (`canonical` set) is no page of its own here: its id
leads to its canonical page (`redirects`), which carries its title as an alias."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from wiki_mcp.wiki.okf import OWNER_WORDS

OWNER_PAGE = "wiki/owner/profile"
PERSONAL_PAGE = "wiki/owner/personal"  # the owner's private page: feelings, values, reflections
CANDIDATES = 30
# Page types that are not entities to file facts on.
_SKIP_TYPES = {"source", "guide", "proposal", "maintenance-report"}
_WORD = re.compile(r"\w+", re.UNICODE)


def norm(text: str) -> str:
    """A title for comparison: no accents, case or punctuation."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(_WORD.findall(plain.lower()))


def words(text: str) -> set[str]:
    return {w for w in norm(text).split() if len(w) >= 3}


@dataclass
class Page:
    id: str
    type: str
    title: str
    description: str = ""
    private: bool = False  # only clients with the wiki:private scope see it
    aliases: tuple[str, ...] = ()  # other names it is known by

    def named(self, key: str) -> bool:
        """Whether its title or one of its aliases is `key` (normalised with norm)."""
        return any(norm(n) == key for n in (self.title, *self.aliases))

    def line(self) -> str:
        desc = f": {self.description[:120]}" if self.description else ""
        return f"- {self.id} ({self.type}) {self.title}{desc}"


class Catalogue:
    def __init__(self, docs: list[dict[str, Any]], types: list[dict[str, Any]]) -> None:
        self.pages: dict[str, Page] = {}
        self.redirects: dict[str, str] = {}  # a merged duplicate's id -> its canonical page's
        for d in docs:
            kind = str(d.get("type") or "")
            if d.get("canonical"):
                self.redirects[str(d["id"])] = str(d["canonical"])
            elif kind and kind not in _SKIP_TYPES:
                self.add(
                    Page(
                        str(d["id"]),
                        kind,
                        str(d.get("title") or d["id"]),
                        str(d.get("description") or ""),
                        d.get("private") is True,
                        tuple(str(a) for a in d.get("aliases") or []),
                    )
                )
        # writable types and their folders, from /index
        self.folders = {str(t["type"]): str(t["folder"]) for t in types if t.get("folder")}

    def add(self, page: Page) -> None:
        self.pages[page.id] = page

    def candidates(self, text: str, limit: int = CANDIDATES) -> list[Page]:
        """Pages whose titles appear in `text`, best first; the owner's profile always."""
        found = words(text)
        flat = f" {norm(text)} "
        scored: list[tuple[float, str]] = []
        for page in self.pages.values():
            title_words = words(page.title)
            if not title_words:
                continue
            score = len(title_words & found) / len(title_words)
            if f" {norm(page.title)} " in flat:
                score += 2
            if score >= 0.5:
                scored.append((score, page.id))
        scored.sort(key=lambda s: (-s[0], s[1]))
        picked = [self.pages[i] for _, i in scored[:limit]]
        if OWNER_PAGE in self.pages and self.pages[OWNER_PAGE] not in picked:
            picked.append(self.pages[OWNER_PAGE])
        return picked

    def page(self, pid: str | None) -> Page | None:
        """The page with this id, or the canonical page of the duplicate with this id."""
        if not pid:
            return None
        return self.pages.get(self.redirects.get(pid, pid))

    def is_owner(self, name: str) -> bool:
        """Whether `name` is exactly (normalised) the owner profile's title or an alias, or a
        generic word for the owner (OWNER_WORDS: "owner", "eigenaar", ...)."""
        key = norm(name)
        if not key:
            return False
        if key in {norm(w) for w in OWNER_WORDS}:
            return True
        owner = self.pages.get(OWNER_PAGE)
        return owner is not None and owner.named(key)

    def by_title(self, title: str, type: str | None = None) -> Page | None:
        """The page with this exact title or alias (normalised), preferring the same type."""
        key = norm(title)
        same = [p for p in self.pages.values() if p.named(key)]
        typed = [p for p in same if p.type == type]
        if typed:
            return typed[0]
        return same[0] if len(same) == 1 else None

    def near(self, title: str, type: str) -> list[Page]:
        """Pages of the same type whose title words contain, or are contained in, this one's
        ("Jane" and "Jane Doe"): maybe the same entity."""
        mine = words(title)
        if not mine:
            return []
        return [
            p
            for p in self.pages.values()
            if p.type == type and (w := words(p.title)) and w != mine and (w <= mine or mine <= w)
        ]
