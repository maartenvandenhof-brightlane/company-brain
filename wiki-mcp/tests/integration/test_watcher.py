"""Out-of-band edits in the wiki folder reach the search index without a restart."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from wiki_mcp.watcher import WikiWatcher, changed_docs
from wiki_mcp.wiki.store import WikiStore

PAGE = """---
type: concept
title: Quokka
description: A hand-written page about quokkas.
---

Quokkas live on Rottnest Island.
"""


def test_changed_docs_keeps_bundle_markdown_only(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    paths = {
        str(root / "wiki/concepts/a.md"),
        str(root / "wiki/concepts/.a.md.tmp"),
        str(root / ".git/HEAD"),
        str(root / ".obsidian/workspace.md"),
        str(root / "wiki/concepts/notes.txt"),
        str(tmp_path / "elsewhere.md"),
    }
    assert changed_docs(root, paths) == {PurePosixPath("wiki/concepts/a.md")}


async def _until(check: Callable[[], bool]) -> None:
    for _ in range(200):
        if check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


async def test_hand_edits_are_indexed_and_announced(store: WikiStore) -> None:
    watcher = WikiWatcher(store, debounce_ms=100)
    seen: list[set[str]] = []

    async def subscriber(ids: set[str]) -> None:
        seen.append(ids)

    watcher.subscribe(subscriber)
    task = asyncio.create_task(watcher.run())
    try:
        await asyncio.sleep(0.3)  # the watch is set up
        page = store.root / "wiki" / "concepts" / "quokka.md"
        page.write_text(PAGE, encoding="utf-8")
        await _until(lambda: bool(store.index.search("Rottnest")))
        assert store.index.search("Rottnest")[0].row.id == "wiki/concepts/quokka"
        await _until(lambda: any("wiki/concepts/quokka" in ids for ids in seen))
        page.write_text(PAGE.replace("Rottnest Island", "Bald Island"), encoding="utf-8")
        await _until(lambda: not store.index.search("Rottnest"))
        assert store.index.search("Bald")
        page.unlink()
        await _until(lambda: store.index.get("wiki/concepts/quokka") is None)
    finally:
        task.cancel()
