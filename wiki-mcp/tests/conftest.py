from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from wiki_mcp.config import Trust
from wiki_mcp.wiki.actor import Actor
from wiki_mcp.wiki.index import SearchIndex
from wiki_mcp.wiki.store import WikiStore
from wiki_mcp.wiki.types import TypeRegistry

PROJECT = Path(__file__).resolve().parents[1]


def _find(rel: str) -> Path:
    for base in (PROJECT, PROJECT.parent):
        candidate = base / rel
        if candidate.exists():
            return candidate
    raise FileNotFoundError(rel)


TYPES_FILE = _find("config/wiki-types.yaml")
SEED_DIR = _find("seed")


@pytest.fixture
def registry() -> TypeRegistry:
    return TypeRegistry.load(TYPES_FILE)


@pytest.fixture
def agent() -> Actor:
    return Actor("claude.ai", Trust.AGENT)


@pytest.fixture
def owner() -> Actor:
    return Actor(
        "claude.ai",
        Trust.OWNER,
        frozenset({"wiki:read", "wiki:write", "wiki:admin"}),
        human="owner",
    )


@pytest.fixture
async def store(tmp_path: Path, registry: TypeRegistry) -> AsyncIterator[WikiStore]:
    index = SearchIndex(tmp_path / "index.sqlite")
    s = WikiStore(
        tmp_path / "wiki",
        index,
        registry,
        public_base_url="https://mcp.example.com",
        proposal_threshold=Trust.EXTERNAL,
    )
    await s.startup(SEED_DIR)
    yield s
    index.close()


def person(title: str = "Jane Doe", **extra: object) -> dict[str, object]:
    fm: dict[str, object] = {
        "type": "person",
        "title": title,
        "description": f"{title} is a person used in tests.",
        "tags": ["test"],
    }
    fm.update(extra)
    return fm
