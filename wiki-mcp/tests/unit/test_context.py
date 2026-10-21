"""The session context pages: created when missing, returned by start_session."""

from __future__ import annotations

from pathlib import PurePosixPath

from tests.conftest import SEED_DIR
from wiki_mcp.context import ensure_context, session_context
from wiki_mcp.wiki.actor import Actor
from wiki_mcp.wiki.store import WikiStore


async def test_missing_context_pages_are_recreated(store: WikiStore, agent: Actor) -> None:
    page = store.root / "context" / "user.md"
    assert page.is_file()  # a new wiki gets them with the seed
    page.unlink()
    store.reindex(PurePosixPath("context/user.md"))
    assert session_context(store, agent)["user"] == ""
    head = store.repo.head()
    await ensure_context(store, SEED_DIR)
    assert page.is_file() and store.repo.head() != head and not store.repo.is_dirty()
    assert session_context(store, agent)["user"].startswith("# User")
    head = store.repo.head()
    await ensure_context(store, SEED_DIR)  # nothing missing: no commit
    assert store.repo.head() == head
