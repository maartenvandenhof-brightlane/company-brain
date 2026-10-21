"""A WikiReader over the store in this process, for assistant dreams: the same data as the
portal API gives the portal's WikiClient, built by the same functions (wiki_mcp.portal_reads)
and decoded the way a client decodes the API's JSON. Every read is fresh (no cache).

With `private=False` it reads the wiki as an app without wiki:private sees it, for an
assistant dream run from such an app: no private document is listed or found (it answers
like a missing one), backlinks, outlinks, `related`, `sources`, the graph, open follow-ups
and renames lose private ids. Commits that touched a private page keep only their id and
date, marked private (their subject, reason and files could tell), so deduction still finds
the last dream's base commit among them; hot memory leaves private commits out anyway.
"""

from __future__ import annotations

from typing import Any

from wiki_mcp.portal_reads import (
    MAX_DOCS,
    doc_body,
    docs_body,
    graph_body,
    index_body,
    open_loops_body,
    plain,
    recent_body,
    renamed_body,
)
from wiki_mcp.wiki.store import WikiError, WikiStore


class NotFound(LookupError):
    """No such document (the portal API's 404)."""


class LocalReader:
    def __init__(self, store: WikiStore, *, private: bool = True) -> None:
        self.store = store
        self.private = private  # False: as an app without wiki:private sees the wiki

    async def docs(self, *, type: str | None = None) -> list[dict[str, Any]]:
        body = docs_body(self.store, type=type, limit=MAX_DOCS, private=self.private)
        return list(plain(body)["docs"])

    async def doc(self, ref: str) -> dict[str, Any]:
        try:
            body = doc_body(self.store, ref, private=self.private)
        except WikiError as exc:
            raise NotFound(str(exc)) from exc
        return dict(plain(body))

    async def index(self) -> dict[str, Any]:
        body = dict(plain(index_body(self.store)))
        if not self.private:
            body["docs"] = [d for d in body["docs"] if not d.get("private")]
            body["known_ids"] = [
                i for i in body.get("known_ids", []) if i not in self.store.index.private_ids()
            ]
        return body

    async def graph(self, max_age: float = 0) -> dict[str, Any]:
        return dict(plain(graph_body(self.store, {"raw", "proposals"}, private=self.private)))

    async def recent(self, limit: int = 50, *, since: str | None = None) -> list[dict[str, Any]]:
        changes = list(plain(recent_body(self.store, since, limit))["changes"])
        if self.private:
            return changes
        return [_redacted(c) if c.get("private") else c for c in changes]

    async def renamed(self, ref: str) -> list[str]:
        to = [str(i) for i in plain(await renamed_body(self.store, ref))["to"]]
        if self.private:
            return to
        hidden = self.store.index.private_ids()
        return [i for i in to if i not in hidden]

    async def open_loops(self) -> list[dict[str, Any]]:
        loops = list(plain(open_loops_body(self.store))["loops"])
        return loops if self.private else [lp for lp in loops if not lp.get("private")]


def _redacted(commit: dict[str, Any]) -> dict[str, Any]:
    """A commit that touched a private page, as an app without wiki:private may know it."""
    return {"commit": commit["commit"], "author": "", "date": commit["date"], "summary": "",
            "reason": "", "files": [], "private": True}  # fmt: skip
