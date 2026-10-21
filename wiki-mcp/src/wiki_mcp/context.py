"""The session context: context/soul, context/user and context/memory in the wiki.

Plain wiki pages (type `context`), editable by hand, over REST and with write_concept. MCP
`start_session` returns all three in one payload; the Drive export's "Start here" Doc will too.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from wiki_mcp.wiki.actor import SYSTEM, Actor
from wiki_mcp.wiki.okf import parse_document
from wiki_mcp.wiki.store import WikiError, WikiStore

CONTEXT_FILES = ("soul", "user", "memory")


async def ensure_context(store: WikiStore, seed_dir: Path) -> None:
    """Create missing context pages from the seed's templates (a new wiki gets them with the
    seed; this covers older wikis and deleted pages)."""
    for name in CONTEXT_FILES:
        template = seed_dir / "context" / f"{name}.md"
        if store.exists(PurePosixPath(f"context/{name}.md")) or not template.is_file():
            continue
        doc = parse_document(template.read_text(encoding="utf-8"))
        await store.write_concept(
            f"context/{name}", doc.frontmatter, doc.body, "Add the session context template", SYSTEM
        )


def session_context(store: WikiStore, actor: Actor) -> dict[str, str]:
    """The three context pages' bodies as `actor` sees them ("" when missing or hidden)."""
    out = {}
    for name in CONTEXT_FILES:
        try:
            out[name] = store.read_visible(f"context/{name}", actor)[1].body.strip()
        except WikiError:
            out[name] = ""
    return out
