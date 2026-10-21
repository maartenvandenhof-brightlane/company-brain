"""The portal API's reads that dreams use, as the JSON bodies it answers: GET /index, /docs,
/doc, /graph, /recent, /renamed and /open-loops (portal_api.py). The routes answer these, and
an assistant dream reads the wiki in this process through the same functions
(wiki_mcp.dream.local), so both see exactly the same data. `plain` turns a body into what a
client decodes from the API's JSON (dates and other YAML scalars become strings).

`private=False` answers as an app without wiki:private sees the wiki (the MCP tools' rules):
an assistant dream run from such an app reads it that way (dream/local.py). The API itself
always reads everything.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

from wiki_mcp.wiki.actor import DREAM
from wiki_mcp.wiki.graph import build_graph
from wiki_mcp.wiki.okf import is_private
from wiki_mcp.wiki.paths import doc_id, normalize_ref
from wiki_mcp.wiki.store import (
    CHAT_IMPORT_TAG,
    SKIPPED_PAGE,
    WikiStore,
    is_private_commit,
)

BOOT = secrets.token_hex(4)  # the index generation restarts at 0 with the process
MAX_DOCS = 1000
MAX_RECENT = 200


def dumps(content: Any) -> str:
    """The API's JSON: frontmatter may hold dates or other YAML scalars, rendered as strings."""
    return json.dumps(content, ensure_ascii=False, default=str, separators=(",", ":"))


def plain(content: Any) -> Any:
    """A body as a client of the API decodes it."""
    return json.loads(dumps(content))


def index_body(store: WikiStore) -> dict[str, Any]:
    counts: dict[str, int] = {}
    all_rows = store.index.all()
    rows = [r for r in all_rows if not r.canonical]
    for row in rows:
        counts[row.type or ""] = counts.get(row.type or "", 0) + 1
    return {
        "title": store.wiki_title,
        "known_ids": [r.id for r in all_rows],
        "markdown": store.read_text_file("index.md"),
        "types": [
            {
                "type": t.name,
                "folder": str(t.folder),
                "description": t.description,
                "count": counts.get(t.name, 0),
            }
            for t in store.registry.writable()
        ],
        "docs": [r.summary() for r in rows],
    }


def docs_body(
    store: WikiStore,
    *,
    type: str | None = None,
    tag: str | None = None,
    folder: str | None = None,
    status: str | None = None,
    limit: int = MAX_DOCS,
    private: bool = True,
    include_merged: bool = False,
    merged_only: bool = False,
) -> dict[str, Any]:
    rows = store.index.list_docs(
        type=type,
        tag=tag,
        folder=folder,
        status=status,
        limit=limit,
        private=private,
        include_merged=include_merged,
        merged_only=merged_only,
    )
    return {"count": len(rows), "docs": [r.summary() for r in rows]}


def doc_body(store: WikiStore, ref: str, *, private: bool = True) -> dict[str, Any]:
    """One file; WikiError when there is none (without `private`, also for a private one)."""
    if private:
        rel, doc, _text = store.read(ref)
        outlinks = store.index.outlinks(doc_id(rel))
    else:  # DREAM holds no wiki:private: related and sources lose private ids too
        rel, doc, _text = store.read_visible(ref, DREAM)
        hidden = store.index.private_ids()
        outlinks = [o for o in store.index.outlinks(doc_id(rel)) if o not in hidden]
    did = doc_id(rel)
    return {
        "id": did,
        "path": str(rel),
        "title": doc.title or rel.stem,
        "frontmatter": doc.frontmatter,
        "body": doc.body,
        "private": is_private(doc.frontmatter),
        "backlinks": [r.summary() for r in store.index.backlinks(did, private=private)],
        "outlinks": outlinks,
    }


def graph_body(
    store: WikiStore, include: set[str], since: str | None = None, *, private: bool = True
) -> dict[str, Any]:
    """The link graph; only {version, unchanged} when `since` is its current version."""
    proposals = store.list_proposals(private=private) if "proposals" in include else None
    version = f"{BOOT}.{store.index.generation}.{len(proposals) if proposals is not None else '-'}"
    if since == version:
        return {"version": version, "unchanged": True}
    graph = build_graph(
        store.index,
        raw="raw" in include,
        proposals=proposals,
        skipped_page=str(SKIPPED_PAGE)[:-3],
        chat_tag=CHAT_IMPORT_TAG,
        private=private,
    )
    return {"version": version, "skipped_page": str(SKIPPED_PAGE)[:-3], **graph}


def recent_body(store: WikiStore, since: str | None, limit: int) -> dict[str, Any]:
    """Git history, newest first; WikiError for a `since` git does not understand."""
    commits = store.recent_changes(since, max(1, min(limit, MAX_RECENT)))
    hidden = store.private_paths()
    return {
        "changes": [
            {
                "commit": c.sha[:12],
                "author": c.author,
                "date": c.date,
                "summary": c.subject,
                "reason": c.body.split("\n\n")[0] if c.body else "",
                "files": [{"status": s, "path": p} for s, p in c.files],
                "private": is_private_commit(c, hidden),
            }
            for c in commits
        ]
    }


async def renamed_body(store: WikiStore, ref: str) -> dict[str, Any]:
    """Where git history shows a page was renamed to; PathError for a bad ref."""
    rel = normalize_ref(ref)
    found = await asyncio.to_thread(store.repo.renamed_to, rel)
    return {"id": doc_id(rel), "to": sorted(p.removesuffix(".md") for p in found)}


def open_loops_body(store: WikiStore) -> dict[str, Any]:
    return {"loops": store.open_loops()}
