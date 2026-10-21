"""D4, hygiene: small fixes on reviewed pages, made in code (no model call).

Direct fixes, one commit for the pass (wiki-mcp re-checks each one, see wiki/facts.py):
  sources     a raw source a footnote cites (`[^id]`) but frontmatter `sources` lacks is added
  checkbox    a doubled checkbox is collapsed: `- [ ] - [ ] x` becomes `- [ ] x`
  link        a link to a page that no longer exists goes to the page it was renamed to, when
              git history shows exactly one
  pronoun     on the owner profile: their/them/... become his/him/... (not "they", which
              needs the verb changed: that is flagged instead)
A link on a non-private page is never sent to a private one (flagged instead).
Flags only (the dream log lists them): orphans (reviewed pages nothing links to), broken
links without a single rename, footnotes no source matches, lines that need two fixes.

A line gets at most one fix per dream; the next dream takes the next one. Duplicate pages
are judged by a model and only ever proposed as merges (merge.py, not here).

survey() reads what the fixes need (the index, the link graph, renames) and plans them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from wiki_mcp.dream.pages import OWNER_PAGE, Page
from wiki_mcp.dream.reader import WikiReader
from wiki_mcp.wiki.facts import bullet_lines, collapse_checkboxes, footnotes, he_for_they
from wiki_mcp.wiki.links import resolve_link

_TARGET = re.compile(r"\]\(([^)\s]+)\)")
_THEY = re.compile(r"\b[Tt]hey\b")


@dataclass
class Fix:
    op: dict[str, Any]  # a wiki-mcp dream operation (wiki/dreamops.py)
    line: str  # for the dream log: "page: change: reason"


@dataclass
class Hygiene:
    fixes: list[Fix] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


@dataclass
class Survey:
    """The fixes and flags for the reviewed pages, and what the merges need next."""

    planned: Hygiene
    known: set[str]  # every indexed id
    private: frozenset[str]  # ids of private pages

    def reason(self, dream: int) -> str:
        """The commit's reason."""
        n = len(self.planned.fixes)
        return f"Dream {dream} hygiene: {n} small fixes on reviewed pages"


def raw_ids_by_stem(docs: list[dict[str, Any]]) -> dict[str, str]:
    """Footnote id -> raw source id, for the ids exactly one raw source has."""
    seen: dict[str, list[str]] = {}
    for d in docs:
        did = str(d.get("id", ""))
        if did.startswith("raw/"):
            seen.setdefault(did.rsplit("/", 1)[-1], []).append(did)
    return {stem: ids[0] for stem, ids in seen.items() if len(ids) == 1}


async def survey(wiki: WikiReader, pages: list[Page]) -> Survey:
    """The fixes and flags for these reviewed pages, with the wiki's index and link graph."""
    docs = (await wiki.index()).get("docs", [])
    private = frozenset(str(d["id"]) for d in docs if d.get("private"))
    graph = await wiki.graph(max_age=0)
    backlinks = {
        str(n.get("id")): int(n.get("backlink_count") or 0) for n in graph.get("nodes", [])
    }
    known = {str(d["id"]) for d in docs}
    planned = await plan(wiki, pages, raw_ids_by_stem(docs), known, backlinks, private)
    return Survey(planned, known, private)


async def plan(
    wiki: WikiReader,
    pages: list[Page],
    raw: dict[str, str],
    known: set[str],
    backlinks: dict[str, int],
    private: frozenset[str] = frozenset(),
) -> Hygiene:
    """The fixes and flags for these reviewed pages; `private`: ids of private pages."""
    out = Hygiene()
    renames: dict[str, list[str]] = {}
    for page in pages:
        _sources(page, raw, out)
        changed: set[str] = set()
        for line in page.body.split("\n"):
            if line in changed or not line.strip():
                continue
            fixed, what = await _line_fix(wiki, page, line, known, renames, out, private)
            if fixed is None:
                continue
            if page.body.count(line) != 1:
                out.flags.append(f"{page.id}: not fixed, the line occurs twice: {line.strip()}")
                continue
            changed.add(line)
            out.fixes.append(
                Fix(
                    {"op": "replace", "page": page.id, "old": line, "new": fixed},
                    f"{page.id}: {what}",
                )
            )
        if page.id == OWNER_PAGE and _THEY.search(page.body):
            out.flags.append(f"{page.id}: uses 'they' for the owner; fix it by hand (he)")
        if backlinks.get(page.id, 0) == 0 and page.id != OWNER_PAGE:
            out.flags.append(f"{page.id}: orphan, no other page links here")
    return out


def _sources(page: Page, raw: dict[str, str], out: Hygiene) -> None:
    listed = set(page.sources)
    missing: list[str] = []
    for fid in dict.fromkeys(footnotes(page.body)):
        rid = raw.get(fid)
        if rid is None:
            out.flags.append(f"{page.id}: footnote [^{fid}] matches no single raw source")
        elif rid not in listed and rid not in missing:
            missing.append(rid)
    if missing:
        out.fixes.append(
            Fix(
                {"op": "add_sources", "page": page.id, "ids": missing},
                f"{page.id}: added {', '.join(missing)} to sources: cited in a footnote, "
                "missing from frontmatter",
            )
        )


async def _line_fix(
    wiki: WikiReader,
    page: Page,
    line: str,
    known: set[str],
    renames: dict[str, list[str]],
    out: Hygiene,
    private: frozenset[str] = frozenset(),
) -> tuple[str | None, str]:
    """The line with its first fix applied, and what was done; (None, "") when none."""
    fixes: list[tuple[str, str]] = []
    if line in bullet_lines(line):
        collapsed = collapse_checkboxes(line)
        if collapsed != line:
            fixes.append((collapsed, "collapsed a doubled checkbox"))
    for target in _TARGET.findall(line):
        did = resolve_link(target, PurePosixPath(page.path))
        if did is None or did in known:
            continue
        if did not in renames:
            renames[did] = await wiki.renamed(did)
        to = [t for t in renames[did] if t in known]
        if len(to) != 1 or line.count(f"]({target})") != 1:
            out.flags.append(f"{page.id}: broken link to {did}, no single page it was renamed to")
            continue
        if to[0] in private and not page.private:
            out.flags.append(f"{page.id}: broken link to {did}, renamed to a private page")
            continue
        fixes.append(
            (line.replace(f"]({target})", f"](/{to[0]}.md)"), f"link to {did} now goes to {to[0]}")
        )
    if page.id == OWNER_PAGE:
        pronouns = he_for_they(line)
        if pronouns != line:
            fixes.append((pronouns, "the owner's pronouns: he/him/his"))
    if len(fixes) > 1:
        out.flags.append(f"{page.id}: a line needs {len(fixes)} fixes; one per dream: {line}")
    return fixes[0] if fixes else (None, "")
