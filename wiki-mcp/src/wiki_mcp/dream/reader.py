"""What the dream code reads from the wiki, in the shapes of wiki-mcp's portal API
(portal_api.py). The portal's WikiClient is one; a reader over the store in this process can
be another.

  docs(type=)        GET /docs: page summaries {id, path, type, title, status, tags, updated,
                     private, ...}
  doc(ref)           GET /doc: one document {id, path, title, frontmatter, body, outlinks,
                     backlinks, private, ...}; LookupError (a subclass) when there is none
  index()            GET /index: {docs: [{id, private, ...}], ...}
  graph(max_age=)    GET /graph with raw sources: {nodes: [{id, backlink_count, ...}], ...};
                     max_age is how old a cached graph may be (0: fresh)
  recent(limit, since=)   GET /recent: commits, newest first {commit, date, summary, private,
                     files: [{path, status}]}
  renamed(ref)       GET /renamed: the page ids git history shows `ref` was renamed to
  open_loops()       GET /open-loops: {page, line, date, reviewed, private}
"""

from __future__ import annotations

from typing import Any, Protocol


class WikiReader(Protocol):
    async def docs(self, *, type: str | None = None) -> list[dict[str, Any]]: ...

    async def doc(self, ref: str) -> dict[str, Any]: ...

    async def index(self) -> dict[str, Any]: ...

    async def graph(self, max_age: float = ...) -> dict[str, Any]: ...

    async def recent(
        self, limit: int = ..., *, since: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def renamed(self, ref: str) -> list[str]: ...

    async def open_loops(self) -> list[dict[str, Any]]: ...
