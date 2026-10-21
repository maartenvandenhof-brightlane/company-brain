"""Search ranking: filler words, curated pages before raw sources, type and folder keywords."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import pytest

from wiki_mcp.wiki.index import SearchIndex, fts_query
from wiki_mcp.wiki.okf import Document


def _doc(type_: str, title: str, body: str, **fm: object) -> Document:
    return Document({"type": type_, "title": title, "description": title, **fm}, body)


# A long transcript that repeats the words far more often than the curated page does.
CHATTER = "User: tell me about my company Acme and the projects I am working on. " * 200


@pytest.fixture
def index(tmp_path: Path) -> Iterator[SearchIndex]:
    idx = SearchIndex(tmp_path / "index.sqlite")
    docs = [
        (
            "wiki/organizations/acme.md",
            _doc("organization", "Acme", "The owner's company. Sells widgets."),
        ),
        (
            "wiki/organizations/old-acme.md",
            _doc("organization", "Old Acme", "Moved: the owner's company.", status="deprecated"),
        ),
        ("wiki/projects/widget-shop.md", _doc("project", "Widget shop", "An online shop.")),
        ("wiki/concepts/vat.md", _doc("concept", "VAT", "Value added tax on widgets.")),
        *(
            (f"raw/chats/claude/chat-{i}.md", _doc("source", f"Chat {i} about my company", CHATTER))
            for i in range(12)
        ),
    ]
    idx.rebuild((PurePosixPath(p), d) for p, d in docs)
    yield idx
    idx.close()


def ids(index: SearchIndex, query: str, limit: int = 10) -> list[str]:
    return [h.row.id for h in index.search(query, limit=limit)]


def test_filler_words_are_dropped() -> None:
    assert fts_query("Tell me more about my company") == '"company"*'
    assert fts_query("Vertel me meer over mijn bedrijf") == '"bedrijf"*'
    assert fts_query("what did we decide about pricing", mode="OR") == '"decide" OR "pricing"*'


def test_query_of_only_filler_words_still_searches(index: SearchIndex) -> None:
    assert fts_query("what is this") == '"what" AND "is" AND "this"*'
    assert fts_query("?!") is None
    index.search("what about it")  # no error; may or may not match


def test_curated_pages_rank_above_raw_for_the_same_match(index: SearchIndex) -> None:
    got = ids(index, "Tell me more about my company")
    assert got[0] == "wiki/organizations/acme"
    first_raw = next(i for i, d in enumerate(got) if d.startswith("raw/"))
    assert all(not d.startswith("raw/") for d in got[:first_raw])


def test_deprecated_pages_rank_last_among_curated(index: SearchIndex) -> None:
    got = ids(index, "company")
    assert got.index("wiki/organizations/acme") < got.index("wiki/organizations/old-acme")


def test_raw_sources_can_still_be_found(index: SearchIndex) -> None:
    assert "raw/chats/claude/chat-3" in ids(index, "chat 3", limit=50)
    assert ids(index, "chat 3")[0] == "raw/chats/claude/chat-3"


def test_any_word_fallback_when_all_words_only_match_raw(index: SearchIndex) -> None:
    # Only the transcripts contain "working"; the project page must still come first.
    got = ids(index, "What projects am I working on?")
    assert got[0] == "wiki/projects/widget-shop"
    assert any(d.startswith("raw/") for d in got)


def test_type_and_folder_are_keywords(index: SearchIndex) -> None:
    assert ids(index, "projects")[0] == "wiki/projects/widget-shop"
    assert ids(index, "organizations", limit=2) == [
        "wiki/organizations/acme",
        "wiki/organizations/old-acme",
    ]


def test_limit_and_filters_still_apply(index: SearchIndex) -> None:
    assert len(ids(index, "company", limit=3)) == 3
    assert [h.row.id for h in index.search("widgets", type="concept")] == ["wiki/concepts/vat"]
