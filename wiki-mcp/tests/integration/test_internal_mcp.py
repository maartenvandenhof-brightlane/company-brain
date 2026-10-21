"""MCP tools exercised through the real SDK client over Streamable HTTP (internal listener)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest

from tests.conftest import SEED_DIR, TYPES_FILE
from tests.integration.helpers import mcp_client, serve_app, structured
from wiki_mcp.config import Settings, Trust
from wiki_mcp.internal import build_internal_app
from wiki_mcp.mcp.tools import SERVER_INSTRUCTIONS
from wiki_mcp.runtime import Runtime, create_runtime
from wiki_mcp.wiki.actor import Actor

TOKEN = "t" * 40

EXPECTED_TOOLS = {
    "search",
    "fetch",
    "search_wiki",
    "read_concept",
    "list_concepts",
    "get_index",
    "recent_changes",
    "get_backlinks",
    "list_unprocessed_sources",
    "get_wiki_guide",
    "get_briefing",
    "start_session",
    "lint_wiki",
    "write_concept",
    "append_to_concept",
    "ingest_source",
    "link_concepts",
    "set_private",
    "merge_identity",
    "review_proposals",
    "accept_proposal",
    "reject_proposal",
}  # the Second Brain's personal tools (dreams, observe, patterns) are not registered


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    settings = Settings(
        wiki_dir=tmp_path / "wiki",
        state_dir=tmp_path / "state",
        types_file=TYPES_FILE,
        seed_dir=SEED_DIR,
        mcp_exposure_mode="cloudflare",
        mcp_hostname="mcp.example.com",
    )
    rt = await create_runtime(settings, run_background=False)
    yield rt
    await rt.close()


@pytest.fixture
async def base_url(runtime: Runtime) -> AsyncIterator[str]:
    app, _ = build_internal_app(
        runtime.deps,
        token=TOKEN,
        client_name="librechat",
        scopes=["wiki:read", "wiki:write"],
        allowed_hosts=["127.0.0.1:*"],
    )
    async with serve_app(app) as url:
        yield url


async def test_tools_listed_with_schemas(base_url: str) -> None:
    async with mcp_client(f"{base_url}/mcp", TOKEN) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == EXPECTED_TOOLS
    assert tools["search"].input_schema["required"] == ["query"]
    assert list(tools["search"].input_schema["properties"]) == ["query"]
    assert tools["fetch"].input_schema["required"] == ["id"]
    assert tools["search"].annotations.read_only_hint is True
    assert tools["write_concept"].annotations.read_only_hint is False
    for tool in tools.values():
        assert tool.description and len(tool.description) > 20


async def test_chatgpt_search_fetch_shape(base_url: str) -> None:
    async with mcp_client(f"{base_url}/mcp", TOKEN) as client:
        res = structured(await client.call_tool("search", {"query": "second brain"}))
        assert set(res) == {"results"}
        first = res["results"][0]
        assert set(first) == {"id", "title", "url"}
        assert first["url"].startswith("https://mcp.example.com/page/")
        doc = structured(await client.call_tool("fetch", {"id": first["id"]}))
        assert set(doc) == {"id", "title", "text", "url", "metadata"}
        assert doc["text"].startswith("---\ntype:")


async def test_write_read_cycle_and_attribution(base_url: str, runtime: Runtime) -> None:
    async with mcp_client(f"{base_url}/mcp", TOKEN) as client:
        guide = structured(await client.call_tool("get_wiki_guide", {}))
        assert "House rules" in guide["guide"]
        assert any(t["type"] == "person" for t in guide["types"])
        src = structured(
            await client.call_tool(
                "ingest_source",
                {"title": "Chat with Jane", "content": "Jane joined Acme.", "trust": "agent"},
            )
        )
        res = structured(
            await client.call_tool(
                "write_concept",
                {
                    "path": "wiki/people/jane-doe",
                    "frontmatter": {
                        "type": "person",
                        "title": "Jane Doe",
                        "description": "A friend who works at Acme.",
                        "sources": [src["id"]],
                    },
                    "body": "Jane works at Acme. See [Company Brain](/wiki/concepts/company-brain.md).",
                    "reason": "User told me about Jane",
                },
            )
        )
        assert res["status"] == "committed"
        hits = structured(
            await client.call_tool("search_wiki", {"query": "acme", "type": "person"})
        )
        assert hits["results"][0]["id"] == "wiki/people/jane-doe"
        page = structured(await client.call_tool("read_concept", {"path": "wiki/people/jane-doe"}))
        assert page["frontmatter"]["sources"][0]["resource"] == f"/{src['path']}"
        back = structured(
            await client.call_tool("get_backlinks", {"path": "wiki/concepts/company-brain"})
        )
        assert "wiki/people/jane-doe" in [b["id"] for b in back["backlinks"]]
        changes = structured(await client.call_tool("recent_changes", {"limit": 2}))
        assert changes["changes"][0]["author"] == "librechat"
        assert changes["changes"][0]["reason"] == "User told me about Jane"
        listing = structured(await client.call_tool("list_concepts", {"type": "person"}))
        assert listing["count"] == 1
        lint = structured(await client.call_tool("lint_wiki", {}))
        assert lint["ok"] is True
    events = runtime.audit.recent(100)
    assert any(
        e["kind"] == "tool" and e.get("tool") == "write_concept" and e["client"] == "librechat"
        for e in events
    )
    # The listener's peer reaches the tool calls' audit entries (through a contextvar).
    assert {e["ip"] for e in events if e["kind"] == "tool"} == {"127.0.0.1"}


async def test_list_unprocessed_sources(base_url: str) -> None:
    async with mcp_client(f"{base_url}/mcp", TOKEN) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        listing = structured(await client.call_tool("list_unprocessed_sources", {}))
        everything = structured(
            await client.call_tool("list_unprocessed_sources", {"tag": "", "limit": 5})
        )
        rerun = structured(await client.call_tool("list_unprocessed_sources", {"rerun": True}))
    assert tools["list_unprocessed_sources"].annotations.read_only_hint is True
    assert listing == {"total": 0, "sources": []}
    assert rerun["total"] == 0 and rerun["playbook_since"]
    assert everything["total"] == len(everything["sources"])


async def test_read_concept_pages_long_bodies(base_url: str) -> None:
    lines = "".join(f"- message {i:05d} of a long transcript\n" for i in range(3000))
    async with mcp_client(f"{base_url}/mcp", TOKEN) as client:
        src = structured(
            await client.call_tool(
                "ingest_source", {"title": "Long chat", "content": lines, "trust": "agent"}
            )
        )
        first = structured(await client.call_tool("read_concept", {"path": src["id"]}))
        pieces, page = [first["body"]], first
        while page["more"]:
            page = structured(
                await client.call_tool(
                    "read_concept",
                    {"path": src["id"], "offset": page["next_offset"], "limit": 50000},
                )
            )
            pieces.append(page["body"])
        small = structured(await client.call_tool("read_concept", {"path": "AGENTS"}))
    assert first["more"] and first["offset"] == 0 and len(first["body"]) <= 40000
    assert first["body"].endswith("\n") and first["total_chars"] > 100000
    assert len("".join(pieces)) == first["total_chars"] and len(pieces) == 3
    assert page["next_offset"] is None
    assert not small["more"] and len(small["body"]) == small["total_chars"]


async def test_tool_errors_are_useful(base_url: str) -> None:
    async with mcp_client(f"{base_url}/mcp", TOKEN) as client:
        res = await client.call_tool(
            "write_concept",
            {
                "path": "../etc/passwd",
                "frontmatter": {"type": "person"},
                "body": "x",
                "reason": "test",
            },
        )
        assert res.is_error and "title" in res.content[0].text
        res = await client.call_tool("read_concept", {"path": "wiki/people/nobody"})
        assert res.is_error and "search" in res.content[0].text
        res = await client.call_tool(
            "accept_proposal", {"proposal_id": "proposals/x", "reason": "because"}
        )
        assert res.is_error and "wiki:admin" in res.content[0].text


async def test_resources_and_prompts(base_url: str) -> None:
    async with mcp_client(f"{base_url}/mcp", TOKEN) as client:
        uris = {str(r.uri) for r in (await client.list_resources()).resources}
        assert {"wiki://guide", "wiki://index"} <= uris
        guide = await client.read_resource("wiki://guide")
        assert "House rules" in guide.contents[0].text
        prompts = {p.name for p in (await client.list_prompts()).prompts}
        assert prompts == {"ingest-source", "answer-from-wiki", "file-conversation"}
        p = await client.get_prompt("answer-from-wiki", {"question": "Who is Jane?"})
        assert "Who is Jane?" in p.messages[0].content.text


async def test_the_briefing_as_tool_resource_and_guide(base_url: str, runtime: Runtime) -> None:
    async with mcp_client(f"{base_url}/mcp", TOKEN) as client:
        none = structured(await client.call_tool("get_briefing", {}))
        assert none["markdown"] is None and "wiki/owner/profile" in none["message"]
        text = (await client.read_resource("wiki://briefing")).contents[0].text
        assert "No briefing has been generated yet" in text

        body = "# Hot memory\n\n## Who\n\n- Builds sheds ([Owner profile](/wiki/owner/profile.md))"
        op = {"op": "write_managed", "page": "wiki/briefing/hot-memory", "body": body}
        await runtime.store.dream_commit(3, "hot-memory", "Briefing", [op])

        found = structured(await client.call_tool("get_briefing", {}))
        assert found["markdown"].startswith("# Hot memory") and found["dream"] == 3
        assert found["generated"] and found["page"] == "wiki/briefing/hot-memory"
        text = (await client.read_resource("wiki://briefing")).contents[0].text
        assert "- Builds sheds" in text
        guide = structured(await client.call_tool("get_wiki_guide", {}))
        assert next(iter(guide)) == "briefing" and "- Builds sheds" in guide["briefing"]["markdown"]


async def test_the_briefing_follows_the_private_scope(base_url: str, runtime: Runtime) -> None:
    ops = [
        {"op": "write_managed", "page": "wiki/briefing/hot-memory", "body": "- Builds sheds"},
        {"op": "write_managed", "page": "wiki/briefing/hot-memory-private",
         "body": "- Builds sheds\n- Misses his sister"},
    ]  # fmt: skip
    await runtime.store.dream_commit(3, "hot-memory", "Briefing", ops)
    async with mcp_client(f"{base_url}/mcp", TOKEN) as client:  # without wiki:private
        found = structured(await client.call_tool("get_briefing", {}))
        guide = structured(await client.call_tool("get_wiki_guide", {}))
        text = (await client.read_resource("wiki://briefing")).contents[0].text
        hidden = await client.call_tool(
            "read_concept", {"path": "wiki/briefing/hot-memory-private"}
        )
    for shown in (found["markdown"], guide["briefing"]["markdown"], text):
        assert "Builds sheds" in shown and "sister" not in shown
    assert found["page"] == "wiki/briefing/hot-memory" and hidden.is_error
    app, _ = build_internal_app(
        runtime.deps,
        token="p" * 40,
        client_name="librechat",
        scopes=["wiki:read", "wiki:private"],
        allowed_hosts=["127.0.0.1:*"],
    )
    async with serve_app(app) as url, mcp_client(f"{url}/mcp", "p" * 40) as client:
        found = structured(await client.call_tool("get_briefing", {}))
        text = (await client.read_resource("wiki://briefing")).contents[0].text
    assert "sister" in found["markdown"] and "sister" in text
    assert found["page"] == "wiki/briefing/hot-memory-private"


async def test_filing_prompts_point_to_the_playbook(base_url: str) -> None:
    async with mcp_client(f"{base_url}/mcp", TOKEN) as client:
        guide = (await client.read_resource("wiki://guide")).contents[0].text
        live = (await client.get_prompt("file-conversation", {})).messages[0].content.text
        imported = await client.get_prompt(
            "file-conversation", {"source_id": "raw/chats/claude/abc"}
        )
        ingest = await client.get_prompt("ingest-source", {"title": "Notes", "content": "Hi"})
    assert "## Filing a conversation" in guide
    for text in (live, imported.messages[0].content.text, ingest.messages[0].content.text):
        assert '"Filing a conversation"' in text and "get_wiki_guide" in text
    assert "this conversation" in live and "ingest_source" in live and "'agent'" in live
    imported_text = imported.messages[0].content.text
    assert "raw/chats/claude/abc" in imported_text and "ingest_source" not in imported_text
    assert "untrusted" in imported_text
    assert "Filing a conversation" in SERVER_INSTRUCTIONS
    assert "file-conversation" in SERVER_INSTRUCTIONS


async def test_bearer_required(base_url: str, runtime: Runtime) -> None:
    async with httpx2.AsyncClient() as http:
        r = await http.post(f"{base_url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 401
        r = await http.post(
            f"{base_url}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert r.status_code == 401
        assert (await http.get(f"{base_url}/health")).json()["status"] == "ok"
    assert any(
        e["kind"] == "internal_auth" and e["outcome"] == "denied" for e in runtime.audit.recent()
    )


async def test_start_session_returns_the_context_pages(base_url: str, runtime: Runtime) -> None:
    """One small payload: the bodies of context/soul, user and memory, created on first start."""
    async with mcp_client(f"{base_url}/mcp", TOKEN) as client:
        found = structured(await client.call_tool("start_session", {}))
        await runtime.store.write_concept(
            "context/memory",
            {"type": "context", "title": "Memory", "description": "Standing facts."},
            "- 2026-10: Quotes are in EUR.",
            "test",
            Actor("owner", Trust.OWNER),
        )
        again = structured(await client.call_tool("start_session", {}))
    assert set(found) == {"soul", "user", "memory"}
    assert found["soul"].startswith("# Soul") and found["user"].startswith("# User")
    assert again["memory"] == "- 2026-10: Quotes are in EUR."
    assert SERVER_INSTRUCTIONS.startswith("At the start of every conversation, call start_session")
