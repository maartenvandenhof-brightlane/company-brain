"""The link graph of the wiki for the portal's graph view and file explorer, built in one pass
from the search index (no file reads).

Nodes are indexed documents; edges are their links (body links, `related`, cited `sources`).
A link to a page that does not exist points to a `ghost` node and is marked `broken`. Raw
sources and pending proposals are left out unless asked for; links to a raw source that is
left out are dropped, not turned into ghosts.

A page (not a raw source) whose frontmatter has `status: draft` is marked `draft`: its current
status is the only thing that decides it, never its tags, trust or who generated it. A document
marked `private: true` is marked `private`; with private=False it is left out, with its links.

Imported chats carry what the portal needs to handle thousands of them:
  conversation  the conversation they belong to, so its `-part-N` files and `-after-N`
                continuations can be folded into one node
  unprocessed   no page cites them yet (SearchIndex.uncited_sources, as GET /unprocessed)
  skipped       only the skipped-conversations page cites them (processed, nothing filed)
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any

from wiki_mcp.wiki.index import DocRow, SearchIndex
from wiki_mcp.wiki.paths import PathError, doc_id, normalize_ref

_CHAT = re.compile(r"^(raw/chats/[^/]+/.+?)(?:-after-(\d+))?(?:-part-(\d+))?$")
_UNIT = re.compile(r"-part-\d+$")


def is_draft(row: DocRow) -> bool:
    """A draft to review: a page whose current frontmatter says `status: draft`."""
    return row.status == "draft" and not row.path.startswith("raw/")


def conversation_of(did: str) -> tuple[str, int, int] | None:
    """(conversation id, after, part) of an imported chat's id; None for anything else."""
    m = _CHAT.match(did)
    if m is None:
        return None
    return m.group(1), int(m.group(2) or 0), int(m.group(3) or 1)


def _folder(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent


def _node(row: DocRow, backlinks: int, date: str | None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": row.id,
        "title": row.title or row.id.rsplit("/", 1)[-1],
        "type": row.type or "",
        "folder": _folder(row.path),
        "tags": row.tags,
        "backlink_count": backlinks,
    }
    if date:
        node["date"] = date
    return node


def build_graph(
    index: SearchIndex,
    *,
    raw: bool = False,
    proposals: Iterable[dict[str, Any]] | None = None,
    skipped_page: str = "",
    chat_tag: str = "chat-import",
    private: bool = True,
) -> dict[str, Any]:
    """{nodes, edges} as described in the module docstring. `proposals` are the store's
    list_proposals() entries when they should be included."""
    rows = index.all()
    dates = index.activity()
    all_ids = {r.id for r in rows}
    redirects = {r.id: r.canonical for r in rows if r.canonical}
    links = list(
        dict.fromkeys(
            (s, redirects.get(d) or d) for s, d in index.all_links() if s not in redirects
        )
    )
    if not private:
        hidden = {r.id for r in rows if r.private}
        links = [(s, d) for s, d in links if s not in hidden]
    counts: dict[str, int] = {}
    for _, dst in links:
        counts[dst] = counts.get(dst, 0) + 1

    unprocessed: set[str] = set()
    if raw:
        for parts, _ in index.uncited_sources(tag=chat_tag, private=private):
            unprocessed.update(r.id for r in parts)
    # who cites each raw transcript (its -part-N files count together, as for processing)
    kinds = {r.id: r.type or "" for r in rows}
    citers: dict[str, set[str]] = {}
    for src, dst in links:
        if dst.startswith("raw/") and kinds.get(src) != "source":
            citers.setdefault(_UNIT.sub("", dst), set()).add(src)

    nodes: dict[str, dict[str, Any]] = {}
    for row in rows:
        is_raw = row.path.startswith("raw/")
        if row.canonical or (is_raw and not raw) or (row.private and not private):
            continue
        node = _node(row, counts.get(row.id, 0), dates.get(row.id))
        if row.private:
            node["private"] = True
        if is_raw:
            chat = conversation_of(row.id)
            if chat is not None:
                node["conversation"], after, part = chat
                if after:
                    node["after"] = after
                if part > 1:
                    node["part"] = part
            if row.id in unprocessed:
                node["unprocessed"] = True
            elif skipped_page and citers.get(_UNIT.sub("", row.id)) == {skipped_page}:
                node["skipped"] = True
        elif is_draft(row):
            node["draft"] = True
        nodes[row.id] = node

    edges: list[dict[str, Any]] = []
    for src, dst in links:
        if src not in nodes:
            continue
        if dst in nodes:
            edges.append({"source": src, "target": dst})
        elif dst not in all_ids:  # a missing page (a raw source left out is not missing)
            if dst not in nodes:
                nodes[dst] = {
                    "id": dst,
                    "title": dst.rsplit("/", 1)[-1],
                    "type": "",
                    "folder": _folder(dst),
                    "tags": [],
                    "backlink_count": counts.get(dst, 0),
                    "ghost": True,
                }
            edges.append({"source": src, "target": dst, "broken": True})

    for p in proposals or []:
        pid = str(p.get("proposal_id") or "")
        if not pid.startswith("proposals/") or (p.get("private") and not private):
            continue
        nodes[pid] = {
            "id": pid,
            "title": str(p.get("title") or pid.rsplit("/", 1)[-1]),
            "type": "proposal",
            "folder": "proposals",
            "tags": [],
            "backlink_count": 0,
            **({"date": str(p["created"])} if p.get("created") else {}),
            **({"private": True} if p.get("private") else {}),
        }
        target = _target_id(p.get("target"))
        if target and target in nodes and not nodes[target].get("ghost"):
            edges.append({"source": pid, "target": target})

    return {"nodes": list(nodes.values()), "edges": edges}


def _target_id(target: Any) -> str | None:
    if not isinstance(target, str) or not target:
        return None
    try:
        return doc_id(normalize_ref(target))
    except PathError:
        return None
