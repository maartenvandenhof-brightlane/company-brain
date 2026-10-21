"""The changes a dream may make, as a list of operations on pages.

The portal's dreams send them through the portal API: a few directly (POST /dream/commit,
one commit per pass), the rest only as a proposal for the owner (POST /dream/proposal, the
proposal action `dream`), which accept_proposal applies with the same code.

  direct     add_sources     add footnoted raw sources to a page's frontmatter `sources`
             replace         replace an exact substring of a page body (hygiene fixes)
             write_managed   write a hot-memory briefing or a dream log, whole
             write_pattern   create, update or retire a personal pattern page
                             (wiki/inferred/patterns/, wiki/patterns.py), whole: the patterns pass
  proposal   append          add a line at the end of a section
             supersede       mark a stated line superseded (its text stays, a suffix is added)
             add_sources     as above
             add_related     add page ids to a page's `related`
             write_inference create or update an inference page (wiki/inferred/)
             deprecate       set a page's status to deprecated and put a note at its top

Every page an operation changes must be reviewed (status stable, without the
from-chat-import tag); nothing under raw/ is ever touched. Except for the managed pages a
dream regenerates, every bullet line a page had must still be there afterwards, identical or
with a superseded suffix (facts.guard_bullets). This module only checks the shape of the
operations; applying them is the store's (WikiStore.apply_dream_ops).

Private pages: the briefing comes in two variants, BRIEFING_PAGE for every client (it may not
name a private page) and BRIEFING_PRIVATE_PAGE (always private) for clients with wiki:private.
The store also keeps what a dream derives from a private page on private pages only.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from wiki_mcp.wiki.facts import is_inference_id
from wiki_mcp.wiki.patterns import is_pattern_id

DIRECT_OPS = frozenset({"add_sources", "replace", "write_managed", "write_pattern"})
PROPOSAL_OPS = frozenset(
    {"append", "supersede", "add_sources", "add_related", "write_inference", "deprecate"}
)
MAX_OPS = 200
MAX_LINE = 4000
BRIEFING_PAGE = "wiki/briefing/hot-memory"
BRIEFING_PRIVATE_PAGE = "wiki/briefing/hot-memory-private"
BRIEFING_PAGES = frozenset({BRIEFING_PAGE, BRIEFING_PRIVATE_PAGE})
DREAM_LOG = re.compile(r"^wiki/maintenance/dream-\d{4}-\d{2}-\d{2}-\d{1,3}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class OpError(ValueError):
    """An operation is malformed or not allowed here."""


@dataclass(frozen=True)
class Op:
    op: str
    page: str  # page id, e.g. wiki/people/jane
    section: str = ""
    line: str = ""
    old: str = ""
    new: str = ""
    date: str = ""
    reason: str = ""
    note: str = ""
    ids: tuple[str, ...] = ()  # add_sources: raw ids; add_related: page ids
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    def as_dict(self) -> dict[str, Any]:
        """The operation as stored in a proposal (empty fields left out)."""
        out = {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(self).items()}
        return {k: v for k, v in out.items() if v not in ("", [], {})}

    def describe(self) -> str:
        """One markdown line for the proposal page."""
        where = f"`{self.page}`"
        if self.op == "append":
            return f"- add to {where} § {self.section}: {self.line}"
        if self.op == "supersede":
            return f"- mark superseded in {where} ({self.reason}): {self.line}"
        if self.op in ("add_sources", "add_related"):
            what = "sources" if self.op == "add_sources" else "related pages"
            return f"- add {what} to {where}: {', '.join(self.ids)}"
        if self.op == "write_inference":
            title = self.frontmatter.get("title", "")
            return f"- write the inference {where}: {title}"
        if self.op == "deprecate":
            return f"- deprecate {where}: {self.note}"
        if self.op == "replace":
            return f"- replace in {where}: `{self.old}` → `{self.new}`"
        return f"- write {where}"


def _text(entry: dict[str, Any], key: str, limit: int, *, one_line: bool = True) -> str:
    value = entry.get(key, "")
    if not isinstance(value, str):
        raise OpError(f"'{key}' must be a string")
    if one_line and "\n" in value.strip("\n"):
        raise OpError(f"'{key}' must be a single line")
    if len(value) > limit:
        raise OpError(f"'{key}' is longer than {limit} characters")
    return value.strip("\n") if one_line else value


def _page_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OpError("each operation needs a 'page' id")
    pid = value.strip().strip("/").removesuffix(".md")
    if not pid.startswith("wiki/"):
        raise OpError(f"{pid}: dreams only change pages under wiki/")
    return pid


def parse_ops(raw: Any, allowed: frozenset[str]) -> list[Op]:
    """Operations from a request, checked for shape and kind; OpError otherwise."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_OPS:
        raise OpError(f"'operations' must be a list of 1 to {MAX_OPS} operations")
    ops: list[Op] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise OpError("each operation must be an object")
        kind = entry.get("op")
        if kind not in allowed:
            raise OpError(
                f"operation {str(kind)[:30]!r} is not allowed here; allowed: "
                + ", ".join(sorted(allowed))
            )
        page = _page_id(entry.get("page"))
        ids = entry.get("ids", [])
        if not isinstance(ids, list) or len(ids) > 50 or not all(isinstance(i, str) for i in ids):
            raise OpError("'ids' must be a list of at most 50 strings")
        fm = entry.get("frontmatter", {})
        if not isinstance(fm, dict):
            raise OpError("'frontmatter' must be an object")
        op = Op(
            op=str(kind),
            page=page,
            section=_text(entry, "section", 120),
            line=_text(entry, "line", MAX_LINE),
            old=_text(entry, "old", MAX_LINE, one_line=False),
            new=_text(entry, "new", MAX_LINE, one_line=False),
            date=_text(entry, "date", 10),
            reason=_text(entry, "reason", 300),
            note=_text(entry, "note", 500),
            ids=tuple(i.strip() for i in ids),
            frontmatter=fm,
            body=_text(entry, "body", 200_000, one_line=False),
        )
        _check(op)
        ops.append(op)
    return ops


def _check(op: Op) -> None:
    need = {
        "append": ("section", "line"),
        "supersede": ("line", "date", "reason"),
        "replace": ("old",),
        "deprecate": ("note",),
        "write_managed": ("body",),
        "write_inference": ("body",),
    }.get(op.op, ())
    for key in need:
        if not getattr(op, key).strip():
            raise OpError(f"{op.op} on {op.page}: '{key}' is required")
    if op.op in ("add_sources", "add_related") and not op.ids:
        raise OpError(f"{op.op} on {op.page}: 'ids' is required")
    if op.op == "supersede" and not _DATE.match(op.date):
        raise OpError(f"supersede on {op.page}: 'date' must be YYYY-MM-DD")
    if op.op == "write_managed" and op.page not in BRIEFING_PAGES and not DREAM_LOG.match(op.page):
        raise OpError(f"{op.page}: only the briefing and dream logs are written whole")
    if op.op == "write_pattern" and (not is_pattern_id(op.page) or not op.frontmatter):
        raise OpError(
            f"{op.page}: write_pattern writes a wiki/inferred/patterns/ page from its frontmatter"
        )
    if op.op != "write_pattern" and is_pattern_id(op.page):
        raise OpError(f"{op.page}: personal patterns change only through write_pattern")
    if op.op == "write_inference" and not is_inference_id(op.page):
        raise OpError(f"{op.page}: inferences live under wiki/inferred/")
    if op.op in ("append", "supersede", "replace", "add_sources", "add_related") and (
        is_inference_id(op.page)
    ):
        raise OpError(f"{op.page}: inference pages change only through write_inference")
