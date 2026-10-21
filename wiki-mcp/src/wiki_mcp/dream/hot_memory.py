"""D5, hot memory: the short briefing every conversation starts with.

  read      the owner profile (whatever its status), his active goals, the reviewed pages,
            the open follow-ups on them (GET /open-loops), the wiki's changes of the last 14
            days, and the people pages changed in the last 30
  ask       the cheap model, for lines in six sections, each linking to one page
  check     every line links to a page it was given (never an inference); its "(unreviewed)"
            mark comes from the page, not the model; the section limits hold
  write     wiki/briefing/hot-memory, whole, within HOT_MEMORY_MAX_CHARS: over it, lines go
            from the end of Recent changes first, then Key people, then Active projects

wiki-mcp serves the page as the get_briefing tool and at the top of get_wiki_guide.

Every client reads that page, so it is made from material without private pages, private
follow-ups or commits that touched a private page. When there are private pages, a second
call makes the variant from everything, wiki/briefing/hot-memory-private (private), which
wiki-mcp serves to clients with wiki:private instead. Both go in the pass's one commit.

The asking is the caller's (the portal's pass in portal/dreams/hot_memory.py): this module
has the material, the answer's parse and checks, and makes the page and the commit.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from wiki_mcp.dream import Unparsable
from wiki_mcp.dream.pages import OWNER_PAGE, Page, lang, read, reviewed_pages
from wiki_mcp.dream.reader import WikiReader
from wiki_mcp.text import shorten
from wiki_mcp.wiki.facts import bullet_lines, is_inference_id, superseded
from wiki_mcp.wiki.store import markdown_section

PAGE = "wiki/briefing/hot-memory"
PRIVATE_PAGE = "wiki/briefing/hot-memory-private"
NAME = "hot-memory"  # the pass
MATERIAL_CHARS = 40_000
PROFILE_CHARS = 8_000
PAGE_CHARS = 1_500
TEXT_CHARS = 300
# (key in the answer, heading, most lines)
SECTIONS = (
    ("who", "Who", 4),
    ("goals", "Goals", 8),
    ("projects", "Active projects", 6),
    ("open_loops", "Open loops", 8),
    ("recent", "Recent changes (last 14 days)", 6),
    ("people", "Key people now", 5),
)
TRIM = ("recent", "people", "projects", "open_loops", "goals", "who")  # first to go first


@dataclass
class Line:
    text: str
    page: str


@dataclass
class Briefing:
    sections: dict[str, list[Line]] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)


def active_goals(profile: Page | None) -> list[str]:
    """The lines of the profile's Goals section that are not marked superseded."""
    if profile is None:
        return []
    section = markdown_section(profile.body, "Goals") or ""
    return [line.strip() for line in bullet_lines(section) if superseded(line) is None]


def parse(answer: str, known: dict[str, Page]) -> Briefing:
    """The lines that pass the checks; why the others were dropped. Unparsable when the
    answer is not a JSON object with the sections at all."""
    try:
        data = json.loads(answer)
    except ValueError as exc:
        raise Unparsable(f"not JSON: {exc}") from exc
    if not isinstance(data, dict) or not any(isinstance(data.get(k), list) for k, _, _ in SECTIONS):
        raise Unparsable("the answer must be a JSON object with the section lists")
    out = Briefing()
    seen: set[str] = set()
    for key, _, most in SECTIONS:
        lines: list[Line] = []
        for raw in data.get(key) or []:
            text = " ".join(str(raw.get("text") or "").split()) if isinstance(raw, dict) else ""
            page = str(raw.get("page") or "").strip("/").removesuffix(".md") if text else ""
            if not text or len(text) > TEXT_CHARS:
                out.dropped.append(f"{key}: a line without text or over {TEXT_CHARS} characters")
            elif is_inference_id(page) or "wiki/inferred" in text:
                out.dropped.append(f"{key}: rests on an inference: {text[:80]}")
            elif page not in known:
                out.dropped.append(f"{key}: links to a page it was not given ({page[:80]})")
            elif text.lower() in seen:
                continue
            elif len(lines) >= most:
                out.dropped.append(f"{key}: over its limit of {most} lines")
            else:
                seen.add(text.lower())
                lines.append(Line(text.replace("[", "(").replace("]", ")"), page))
        out.sections[key] = lines
    notes = data.get("notes")
    if isinstance(notes, list) and notes:
        out.dropped.append("model notes: " + "; ".join(str(n) for n in notes)[:300])
    return out


def render(
    briefing: Briefing, known: dict[str, Page], dream_id: int, at: dt.datetime, max_chars: int
) -> str:
    """The page body, within max_chars: lines are dropped in TRIM order until it fits."""
    sections = {k: list(v) for k, v in briefing.sections.items()}
    while True:
        text = _text(sections, known, dream_id, at)
        if len(text) <= max_chars:
            briefing.sections = sections
            return text
        key = next((k for k in TRIM if sections.get(k)), None)
        if key is None:
            briefing.sections = sections
            return text[:max_chars]  # the header alone is over: never happens in practice
        sections[key].pop()
        briefing.dropped.append(f"{key}: a line dropped to stay within {max_chars} characters")


def _text(
    sections: dict[str, list[Line]], known: dict[str, Page], dream_id: int, at: dt.datetime
) -> str:
    out = [
        "# Hot memory",
        "",
        f"Generated {at.strftime('%Y-%m-%d %H:%M')} UTC by dream {dream_id}. Stated facts only; "
        "(unreviewed) marks facts from draft pages.",
    ]
    for key, heading, _ in SECTIONS:
        out += ["", f"## {heading}", ""]
        lines = sections.get(key) or []
        for line in lines:
            page = known[line.page]
            mark = "" if page.reviewed else " (unreviewed)"
            out.append(f"- {line.text} ([{_label(page.title)}](/{page.id}.md)){mark}")
        if not lines:
            out.append("- Nothing current.")
    return "\n".join(out) + "\n"


def _label(title: str) -> str:
    return title.replace("[", "(").replace("]", ")")


async def material(
    wiki: WikiReader, *, private: bool = False, reviewed: list[Page] | None = None
) -> tuple[str, dict[str, Page]]:
    """What the model reads, and the pages its lines may link to; without `private`, nothing
    from a private page. `reviewed`: the reviewed pages, when already read."""
    now = dt.datetime.now(dt.UTC)
    profile = await read(wiki, OWNER_PAGE)
    if profile is not None and profile.private and not private:
        profile = None
    if reviewed is None:
        reviewed = await reviewed_pages(wiki)
    pages = [p for p in reviewed if p.id != OWNER_PAGE and (private or not p.private)]
    known: dict[str, Page] = {p.id: p for p in pages}
    parts = ["<material>"]
    if profile is not None:
        known[profile.id] = profile
        parts.append(
            f'<owner-profile id="{profile.id}" status="{profile.status}" '
            f'reviewed="{str(profile.reviewed).lower()}">\n'
            f"{shorten(profile.body, PROFILE_CHARS)}\n</owner-profile>"
        )
    goals = active_goals(profile)
    parts.append(
        "<goals>\n" + "\n".join(f"G{i}: {g}" for i, g in enumerate(goals, 1)) + "\n</goals>"
    )
    loops = [
        lp for lp in await wiki.open_loops() if lp.get("page") in known
    ]  # reviewed pages and the profile (without private ones unless `private`)
    parts.append(
        "<open-loops>\n"
        + "\n".join(
            f"L{i}: [{lp['page']}] {lp['line']} ({lp.get('date') or 'undated'})"
            for i, lp in enumerate(loops, 1)
        )
        + "\n</open-loops>"
    )
    since = (now - dt.timedelta(days=14)).strftime("%Y-%m-%d")
    changes = []
    for c in await wiki.recent(100, since=since):
        if c.get("private") and not private:
            continue
        files = [f["path"].removesuffix(".md") for f in c.get("files") or []]
        wiki_files = [f for f in files if f in known]
        if wiki_files:
            changes.append(
                f"- {str(c.get('date'))[:10]}: {c.get('summary')}: {', '.join(wiki_files)}"
            )
    parts.append("<changes>\n" + "\n".join(changes[:60]) + "\n</changes>")
    month = (now - dt.timedelta(days=30)).strftime("%Y-%m-%d")
    people = [
        p.id for p in pages if p.type == "person" and str(p.frontmatter.get("updated", "")) >= month
    ]
    parts.append("<people-changed-recently>\n" + "\n".join(people) + "\n</people-changed-recently>")
    used = sum(len(p) for p in parts)
    for page in pages:
        text = shorten(page.body, PAGE_CHARS)
        block = (
            f'<page id="{page.id}" type="{page.type}" status="{page.status}" '
            f'lang="{lang(page.body)}">\n{text}\n</page>'
        )
        if used + len(block) > MATERIAL_CHARS:
            break
        parts.append(block)
        used += len(block)
    parts.append("</material>")
    return "\n".join(parts), known


@dataclass
class Made:
    """A briefing made from one answer: the page body and its line count."""

    body: str
    lines: int


def made(
    briefing: Briefing, known: dict[str, Page], dream_id: int, at: dt.datetime, max_chars: int
) -> Made:
    """The page for a checked answer, within max_chars (what was trimmed is in
    `briefing.dropped`)."""
    body = render(briefing, known, dream_id, at, max_chars)
    return Made(body, sum(len(v) for v in briefing.sections.values()))


def _op(page: str, body: str, title: str) -> dict[str, Any]:
    return {
        "op": "write_managed",
        "page": page,
        "body": body,
        "frontmatter": {
            "title": title,
            "description": "The generated briefing every conversation starts with.",
        },
    }


@dataclass
class Write:
    """The pass's one commit."""

    reason: str
    ops: list[dict[str, Any]]
    lines: list[str]  # for the dream log


async def write(
    wiki: WikiReader, dream_id: int, public: Made | None, private: Made | None, same: bool
) -> Write:
    """The commit writing the briefing (`public`: None when there was no material for it)
    and its private variant (`private`: None when not made). `same`: the private variant's
    material is the public one's, so it was not asked for: while the variant exists, it is
    written as the same briefing."""
    ops: list[dict[str, Any]] = []
    lines: list[str] = []
    count = 0
    if public is not None:
        count = public.lines
        ops.append(_op(PAGE, public.body, "Hot memory"))
        lines.append(f"{PAGE}: rewritten with {count} lines ({len(public.body)} characters)")
    if not same:
        if private is not None:
            ops.append(_op(PRIVATE_PAGE, private.body, "Hot memory (with private pages)"))
            lines.append(
                f"{PRIVATE_PAGE}: rewritten with {private.lines} lines "
                f"({len(private.body)} characters)"
            )
    elif ops and await read(wiki, PRIVATE_PAGE) is not None:
        # nothing private any more: the variant is the same briefing
        ops.append(_op(PRIVATE_PAGE, ops[0]["body"], "Hot memory (with private pages)"))
        lines.append(f"{PRIVATE_PAGE}: the same as {PAGE}")
    return Write(f"Dream {dream_id} hot memory: the briefing, {count} lines", ops, lines)
