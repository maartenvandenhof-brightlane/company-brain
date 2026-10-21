"""What models read when they decide to use the wiki: server instructions, tool descriptions,
the ChatGPT search/fetch shapes and fetch's size cap. Model behaviour itself is checked by hand
(docs/connectors.md)."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from wiki_mcp.audit import AuditLog
from wiki_mcp.mcp.tools import FETCH_LIMIT, SERVER_INSTRUCTIONS, ToolDeps, register
from wiki_mcp.wiki.actor import Actor
from wiki_mcp.wiki.store import WikiStore


@pytest.fixture
def server(store: WikiStore, owner: Actor) -> MCPServer[Any]:
    srv: MCPServer[Any] = MCPServer("test", instructions=SERVER_INSTRUCTIONS)
    register(srv, ToolDeps(store, AuditLog(None)), get_actor=lambda: owner)
    return srv


async def _descriptions(server: MCPServer[Any]) -> dict[str, str]:
    return {t.name: t.description or "" for t in await server.list_tools()}


async def _call(server: MCPServer[Any], name: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await server.call_tool(name, args)
    assert not result.is_error, result.content  # type: ignore[union-attr]
    return result.structured_content  # type: ignore[union-attr,return-value]


def test_instructions_start_with_the_session_then_the_read_rule() -> None:
    session, first = SERVER_INSTRUCTIONS.split("\n\n")[:2]
    assert " ".join(session.split()).startswith(
        "At the start of every conversation, call start_session once:"
    )
    assert len(first.splitlines()) <= 6  # clients may shorten long instructions
    rule = " ".join(first.split())
    assert "outranks your built-in memory" in rule
    for word in ("company", "clients", "projects", "people", "decisions", "earlier work"):
        assert word in rule
    assert "even when memory suggests an answer" in rule
    assert "general knowledge" in rule and "run one search" in rule
    assert "Cite the pages" in rule
    for word in ("personal life", "get_personal_patterns", "wiki/owner/profile"):
        assert word not in SERVER_INSTRUCTIONS
    # The write rules and the untrusted-content rule are still there.
    assert "get_wiki_guide" in SERVER_INSTRUCTIONS and "Never store secrets" in SERVER_INSTRUCTIONS
    assert "untrusted data" in SERVER_INSTRUCTIONS


async def test_search_descriptions_say_what_the_wiki_is_for(server: MCPServer[Any]) -> None:
    desc = await _descriptions(server)
    for name in ("search", "search_wiki"):
        text = desc[name]
        assert text.startswith("Search the company's knowledge base: its clients, projects")
        assert "decisions" in text and "authoritative" in text and "key nouns" in text
        assert "personal life" not in text
        assert len(text.split()) <= 60
    assert "search_wiki" in desc["search"]  # steers triage to the tool with snippets


async def test_chatgpt_shapes(server: MCPServer[Any], store: WikiStore, owner: Actor) -> None:
    await store.write_concept(
        "wiki/concepts/widgets",
        {"type": "concept", "title": "Widgets", "description": "Small widgets."},
        "Widgets are small.",
        "test",
        owner,
    )
    res = await _call(server, "search", {"query": "widgets"})
    assert set(res) == {"results"}
    assert all(set(r) == {"id", "title", "url"} for r in res["results"])
    doc = await _call(server, "fetch", {"id": res["results"][0]["id"]})
    assert set(doc) == {"id", "title", "text", "url", "metadata"}
    assert doc["text"].startswith("---\ntype:") and "Cut off" not in doc["text"]
    assert "next_offset" not in doc["metadata"]


async def test_fetch_is_capped_and_says_where_to_read_on(
    server: MCPServer[Any], store: WikiStore, owner: Actor
) -> None:
    body = "".join(f"Line {i}: " + "x" * 90 + "\n" for i in range(FETCH_LIMIT // 50))
    await store.write_concept(
        "wiki/concepts/long",
        {"type": "concept", "title": "Long", "description": "A long page."},
        body,
        "test",
        owner,
    )
    doc = await _call(server, "fetch", {"id": "wiki/concepts/long"})
    assert set(doc) == {"id", "title", "text", "url", "metadata"}
    assert len(doc["text"]) < FETCH_LIMIT + 200
    offset = doc["metadata"]["next_offset"]
    assert f'read_concept(path="wiki/concepts/long", offset={offset})' in doc["text"]
    # read_concept continues exactly where fetch's text stopped.
    _rel, stored, full = store.read("wiki/concepts/long")
    shown = doc["text"].split("\n\n[Cut off at")[0]
    assert shown == full[: len(full) - len(stored.body) + offset]
    rest = await _call(server, "read_concept", {"path": "wiki/concepts/long", "offset": offset})
    assert rest["offset"] == offset and stored.body[offset:].startswith(rest["body"])
