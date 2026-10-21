"""Keep the search index in step with edits made outside the brain (Obsidian, VS Code, git).

The wiki folder is a host directory. Every markdown file that changes on disk, by the brain's
own commits or by hand, is re-indexed after a quiet period; then each subscriber (the Drive
export, later) gets the ids of the changed documents. Hand edits are committed at the next start
(WikiStore.startup), as before.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath

from watchfiles import awatch

from wiki_mcp.wiki.paths import doc_id
from wiki_mcp.wiki.store import WikiStore

log = logging.getLogger(__name__)

Subscriber = Callable[[set[str]], Awaitable[None]]


def changed_docs(root: Path, paths: set[str]) -> set[PurePosixPath]:
    """The bundle paths of changed markdown documents: no hidden files or folders (.git, the
    store's .tmp files), nothing outside the bundle."""
    out = set()
    for raw in paths:
        try:
            rel = PurePosixPath(Path(raw).relative_to(root).as_posix())
        except ValueError:
            continue
        if rel.suffix == ".md" and not any(part.startswith(".") for part in rel.parts):
            out.add(rel)
    return out


class WikiWatcher:
    def __init__(self, store: WikiStore, debounce_ms: int = 1000) -> None:
        self.store = store
        self.debounce_ms = debounce_ms
        self.subscribers: list[Subscriber] = []

    def subscribe(self, fn: Subscriber) -> None:
        self.subscribers.append(fn)

    async def run(self) -> None:
        root = self.store.root.resolve()
        async for changes in awatch(root, debounce=self.debounce_ms, recursive=True):
            rels = changed_docs(root, {path for _, path in changes})
            if rels:
                await self.apply(rels)

    async def apply(self, rels: set[PurePosixPath]) -> None:
        for rel in rels:
            try:
                self.store.reindex(rel)
            except Exception:
                log.exception("could not re-index %s", rel)
        ids = {doc_id(rel) for rel in rels}
        log.debug("re-indexed %d changed documents", len(ids))
        for fn in self.subscribers:
            try:
                await fn(ids)
            except Exception:
                log.exception("watch subscriber failed")
