"""What one model call may read, for dreams and the portal's ideas runs alike, and which pages
recent commits changed (GET /recent)."""

from __future__ import annotations

from typing import Any

MAX_COMMITS = 200
MAX_PAGES = 40  # pages in one call's material
MAX_CHARS = 60_000  # all material (about 15k tokens)
SKIP_IDS = {"wiki/references/skipped-conversations"}


def changed_pages(commits: list[dict[str, Any]]) -> list[str]:
    """Wiki page ids written in these commits, newest first, without deleted ones."""
    ids: list[str] = []
    for commit in commits:
        for f in commit.get("files") or []:
            path = str(f.get("path", ""))
            if f.get("status") == "D" or not path.startswith("wiki/") or not path.endswith(".md"):
                continue
            did = path.removesuffix(".md")
            if did not in ids and did not in SKIP_IDS:
                ids.append(did)
    return ids
