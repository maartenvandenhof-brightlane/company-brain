"""The portal's read-only JSON API on the internal listener."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest
from mcp.shared.auth import OAuthClientInformationFull

from tests.conftest import SEED_DIR, TYPES_FILE, person
from tests.integration.helpers import mcp_client, serve_app, structured
from wiki_mcp.auth.provider import OAuthProvider
from wiki_mcp.config import Settings, Trust
from wiki_mcp.internal import build_internal_app
from wiki_mcp.runtime import Runtime, create_runtime
from wiki_mcp.wiki.actor import Actor
from wiki_mcp.wiki.maintenance import run_lint

LIBRECHAT_TOKEN = "l" * 40
PORTAL_TOKEN = "p" * 40
API = "/api/portal/v1"


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    settings = Settings(
        wiki_dir=tmp_path / "wiki",
        state_dir=tmp_path / "state",
        types_file=TYPES_FILE,
        seed_dir=SEED_DIR,
    )
    rt = await create_runtime(settings, run_background=False)
    yield rt
    await rt.close()


@pytest.fixture
async def client(runtime: Runtime) -> AsyncIterator[httpx2.AsyncClient]:
    app, _ = build_internal_app(
        runtime.deps,
        token=LIBRECHAT_TOKEN,
        client_name="librechat",
        scopes=["wiki:read"],
        allowed_hosts=["127.0.0.1:*"],
        portal_token=PORTAL_TOKEN,
    )
    async with (
        serve_app(app) as url,
        httpx2.AsyncClient(
            base_url=url + API, headers={"Authorization": f"Bearer {PORTAL_TOKEN}"}
        ) as http,
    ):
        yield http


async def test_requires_the_portal_token(client: httpx2.AsyncClient) -> None:
    assert (await client.get("/index", headers={"Authorization": ""})).status_code == 401
    wrong = {"Authorization": f"Bearer {LIBRECHAT_TOKEN}"}
    assert (await client.get("/index", headers=wrong)).status_code == 401


async def test_portal_token_is_not_accepted_on_mcp(client: httpx2.AsyncClient) -> None:
    root = str(client.base_url).rstrip("/").removesuffix(API)
    r = await client.post(root + "/mcp", json={})
    assert r.status_code == 401


async def test_read_only(client: httpx2.AsyncClient) -> None:
    assert (await client.post("/index")).status_code == 405


async def test_index_lists_types_and_docs(client: httpx2.AsyncClient) -> None:
    body = (await client.get("/index")).json()
    assert body["markdown"].startswith("---") or "# " in body["markdown"]
    ids = {d["id"] for d in body["docs"]}
    assert "wiki/concepts/company-brain" in ids
    types = {t["type"]: t for t in body["types"]}
    assert types["concept"]["count"] >= 1
    assert types["concept"]["folder"] == "wiki/concepts"


async def test_doc_with_backlinks(client: httpx2.AsyncClient) -> None:
    body = (await client.get("/doc", params={"id": "/wiki/concepts/company-brain.md"})).json()
    assert body["id"] == "wiki/concepts/company-brain"
    assert body["frontmatter"]["type"] == "concept"
    assert "wiki/howtos/using-this-wiki" in body["outlinks"]
    assert "wiki/howtos/using-this-wiki" in {b["id"] for b in body["backlinks"]}


async def test_doc_rejects_traversal_and_missing(client: httpx2.AsyncClient) -> None:
    for ref in ("../etc/passwd", ".git/config", "wiki/nope"):
        r = await client.get("/doc", params={"id": ref})
        assert r.status_code == 404, ref
        assert "error" in r.json()


async def test_search_uses_the_index(client: httpx2.AsyncClient) -> None:
    body = (await client.get("/search", params={"q": "second brain"})).json()
    assert body["results"]
    assert {"id", "title", "snippet"} <= set(body["results"][0])


async def test_recent_log_and_proposals(client: httpx2.AsyncClient) -> None:
    changes = (await client.get("/recent", params={"limit": 5})).json()["changes"]
    assert changes and changes[0]["commit"]
    assert (await client.get("/recent", params={"since": "yesterday"})).status_code == 400
    assert "Wiki Update Log" in (await client.get("/log")).json()["markdown"]
    assert (await client.get("/proposals")).json() == {"proposals": []}


async def test_processing_counts(client: httpx2.AsyncClient) -> None:
    await client.post("/import", json={"sources": [_chat(), _chat("claude-chat:other")]})
    body = (await client.get("/processing")).json()
    assert body["unprocessed"] == 2 and body["filed_before_playbook"] == 0
    assert body["playbook_since"]


async def test_disabled_without_token(runtime: Runtime) -> None:
    app, _ = build_internal_app(
        runtime.deps,
        token=LIBRECHAT_TOKEN,
        client_name="librechat",
        scopes=["wiki:read"],
        allowed_hosts=["127.0.0.1:*"],
    )
    async with serve_app(app) as url, httpx2.AsyncClient() as http:
        r = await http.get(
            url + API + "/index", headers={"Authorization": f"Bearer {PORTAL_TOKEN}"}
        )
        assert r.status_code == 401


# ------------------------------------------------------------------ chat import


def _chat(key: str = "claude-chat:0b6c8d7e-1111", **extra: object) -> dict[str, object]:
    return {"key": key, "title": "Garden shed", "content": "## User\n\nHi", **extra}


async def test_import_creates_raw_sources(client: httpx2.AsyncClient, runtime: Runtime) -> None:
    r = await client.post(
        "/import",
        json={
            "sources": [
                _chat(),
                _chat("chatgpt-chat:68e1-aaaa", part=1, parts=2),
                _chat("chatgpt-chat:68e1-aaaa", part=2, parts=2),
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["commit"] and not body["existing"] and not body["rejected"]
    assert [c["id"] for c in body["created"]] == [
        "raw/chats/claude/0b6c8d7e-1111",
        "raw/chats/chatgpt/68e1-aaaa",
        "raw/chats/chatgpt/68e1-aaaa-part-2",
    ]
    doc = (await client.get("/doc", params={"id": "raw/chats/chatgpt/68e1-aaaa-part-2"})).json()
    fm = doc["frontmatter"]
    assert fm["type"] == "source" and fm["trust"] == "agent"
    assert fm["resource"] == "chatgpt-chat:68e1-aaaa"
    assert fm["tags"] == ["chat-import", "chatgpt"]
    assert fm["title"] == "Garden shed (part 2 of 2)"
    assert "not instructions" in doc["body"] and "## User" in doc["body"]
    head = runtime.deps.store.recent_changes(None, 1)[0]
    assert head.author == "portal" and head.subject == "Import 2 chat conversation(s)"
    issues = run_lint(runtime.deps.store, 90).issues
    assert not [i for i in issues if i.path.startswith("raw/") and i.severity == "error"]


async def test_import_never_overwrites(client: httpx2.AsyncClient) -> None:
    first = (await client.post("/import", json={"sources": [_chat()]})).json()
    again = await client.post(
        "/import", json={"sources": [_chat(content="changed"), _chat("claude-chat:new-one")]}
    )
    body = again.json()
    assert [e["key"] for e in body["existing"]] == ["claude-chat:0b6c8d7e-1111"]
    assert [c["key"] for c in body["created"]] == ["claude-chat:new-one"]
    assert body["commit"] != first["commit"]
    doc = (await client.get("/doc", params={"id": "raw/chats/claude/0b6c8d7e-1111"})).json()
    assert "changed" not in doc["body"]


async def test_import_dry_run_writes_nothing(client: httpx2.AsyncClient) -> None:
    body = (await client.post("/import", json={"dry_run": True, "sources": [_chat()]})).json()
    assert body["dry_run"] and body["commit"] is None and len(body["created"]) == 1
    r = await client.get("/doc", params={"id": "raw/chats/claude/0b6c8d7e-1111"})
    assert r.status_code == 404


async def test_import_rejects_bad_items_one_by_one(client: httpx2.AsyncClient) -> None:
    secret = "sk-ant-" + "api03-" + "x" * 40
    body = (
        await client.post(
            "/import",
            json={
                "sources": [
                    _chat(content=f"key {secret}"),
                    _chat("claude-chat:../../etc"),
                    _chat("wiki-chat:abc"),
                    _chat("claude-chat:ok", part=3, parts=2),
                    _chat("claude-chat:fine"),
                ]
            },
        )
    ).json()
    assert len(body["rejected"]) == 4
    assert [c["key"] for c in body["created"]] == ["claude-chat:fine"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"sources": []},
        {"sources": ["x"]},
        {"sources": [{"key": "claude-chat:a", "title": 1, "content": "x"}]},
        {"sources": [_chat(part=True)]},
        {"sources": [_chat()] * 501},
    ],
)
async def test_import_rejects_bad_requests(client: httpx2.AsyncClient, payload: object) -> None:
    assert (await client.post("/import", json=payload)).status_code == 400


async def test_import_is_the_only_write(client: httpx2.AsyncClient) -> None:
    for path in ("/doc", "/import/../index", "/importx"):
        assert (await client.post(path, json={})).status_code in (404, 405)
    for method in ("PUT", "DELETE", "PATCH"):
        assert (await client.request(method, "/import", json={})).status_code == 405
    wrong = {"Authorization": f"Bearer {LIBRECHAT_TOKEN}"}
    r = await client.post("/import", json={"sources": [_chat()]}, headers=wrong)
    assert r.status_code == 401


async def test_import_size_cap(client: httpx2.AsyncClient) -> None:
    big = "x" * (16 * 1024 * 1024)
    r = await client.post("/import", json={"sources": [_chat(content=big)]})
    assert r.status_code == 413


# ------------------------------------------------------------------ continued conversations

SHED = "raw/chats/claude/0b6c8d7e-1111"


def _counted(n: int, last: str, **extra: object) -> dict[str, object]:
    return _chat(messages=n, last_message_at=last, **extra)


async def test_import_records_counts_and_reports_them(client: httpx2.AsyncClient) -> None:
    await client.post("/import", json={"sources": [_counted(4, "2026-01-10T09:30:00Z")]})
    fm = (await client.get("/doc", params={"id": SHED})).json()["frontmatter"]
    assert fm["messages"] == 4 and fm["last_message_at"] == "2026-01-10T09:30:00Z"
    again = await client.post("/import", json={"sources": [_counted(4, "2026-01-10T09:30:00Z")]})
    body = again.json()
    assert body["commit"] is None and not body["created"]  # unchanged: a no-op
    assert body["existing"] == [
        {
            "key": "claude-chat:0b6c8d7e-1111",
            "part": 1,
            "after": 0,
            "imported_messages": 4,
            "last_message_at": "2026-01-10T09:30:00Z",
        }
    ]


async def test_continuation_stores_only_the_new_messages(client: httpx2.AsyncClient) -> None:
    await client.post("/import", json={"sources": [_counted(4, "2026-01-10T09:30:00Z")]})
    original = (await client.get("/doc", params={"id": SHED})).json()["body"]
    tail = _counted(6, "2026-02-01T08:00:00Z", after=4, content="## User\n\nMore", parts=2)
    first = (await client.post("/import", json={"sources": [tail]})).json()
    assert [c["id"] for c in first["created"]] == [f"{SHED}-after-4"]
    # The second part arrives in a later batch: still accepted.
    second = (await client.post("/import", json={"sources": [{**tail, "part": 2}]})).json()
    assert [c["id"] for c in second["created"]] == [f"{SHED}-after-4-part-2"]
    doc = (await client.get("/doc", params={"id": f"{SHED}-after-4"})).json()
    fm = doc["frontmatter"]
    assert fm["continues"] == SHED and fm["messages"] == 6
    assert fm["title"] == "Garden shed (continued) (part 1 of 2)"
    assert "chat-import" in fm["tags"] and "More" in doc["body"]
    assert (await client.get("/doc", params={"id": SHED})).json()["body"] == original
    # Now 6 are imported: the full transcript reports that, the same tail is a no-op,
    # and a tail built on the old count is refused.
    full = (await client.post("/import", json={"sources": [_counted(6, "x")]})).json()
    assert full["rejected"]  # bad timestamp
    full = (
        await client.post("/import", json={"sources": [_counted(6, "2026-02-01T08:00:00Z")]})
    ).json()
    assert full["existing"][0]["imported_messages"] == 6
    again = (await client.post("/import", json={"sources": [tail]})).json()
    assert again["existing"] and not again["created"]
    stale = _counted(7, "2026-02-02T08:00:00Z", after=5, content="## User\n\nLater")
    body = (await client.post("/import", json={"sources": [stale]})).json()
    assert "5" in body["rejected"][0]["error"] and not body["created"]


async def test_continuation_needs_the_original(client: httpx2.AsyncClient) -> None:
    tail = _counted(3, "2026-02-01T08:00:00Z", after=2)
    body = (await client.post("/import", json={"sources": [tail]})).json()
    assert body["rejected"] and not body["created"]
    bad = _counted(3, "2026-02-01T08:00:00Z", after=3)
    body = (await client.post("/import", json={"sources": [bad]})).json()
    assert "'after'" in body["rejected"][0]["error"]


async def test_legacy_sources_count_messages_from_their_header(
    client: httpx2.AsyncClient,
) -> None:
    content = "# Garden shed\n\n- Source: Claude.ai conversation\n- Messages: 3\n\n## User\n\nHi"
    await client.post("/import", json={"sources": [_chat(content=content)]})
    body = (
        await client.post("/import", json={"sources": [_counted(5, "2026-01-10T09:30:00Z")]})
    ).json()
    assert body["existing"][0]["imported_messages"] == 3
    assert body["existing"][0]["last_message_at"] is None


# ------------------------------------------------------------------ filing processed chats


async def test_file_and_unprocessed(client: httpx2.AsyncClient) -> None:
    await client.post("/import", json={"sources": [_chat()]})
    todo = (await client.get("/unprocessed")).json()
    assert todo["total"] == 1 and todo["sources"][0]["id"] == SHED
    page = {
        "path": "wiki/projects/garden-shed",
        "type": "project",
        "title": "Garden shed",
        "description": "A shed.",
        "sections": [{"heading": "Timeline", "lines": ["- 2026-01: Planned"]}],
    }
    r = await client.post("/file", json={"sources": [SHED], "reason": "Filed", "pages": [page]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == ["wiki/projects/garden-shed"] and body["commit"]
    assert (await client.get("/unprocessed")).json()["total"] == 0
    rerun = (await client.get("/unprocessed", params={"rerun": "1"})).json()
    assert rerun["playbook_since"] and rerun["total"] == 0
    everything = (await client.get("/unprocessed", params={"all": "1"})).json()
    assert [s["id"] for s in everything["sources"]] == [SHED] and everything["sources"][0][
        "filed_at"
    ]
    counts = (await client.get("/processing")).json()
    assert counts == {**counts, "unprocessed": 0, "imported": 1}
    again = {"sources": [SHED], "reason": "Filed again", "pages": [page], "replace": True}
    assert (await client.post("/file", json=again)).status_code == 200
    assert (await client.post("/file", json={**again, "replace": "yes"})).status_code == 400


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"sources": "x", "reason": "r", "pages": []},
        {"sources": [SHED], "reason": 1, "pages": []},
        {"sources": [SHED], "reason": "Filed", "pages": [{"path": 1}]},
        {
            "sources": [SHED],
            "reason": "Filed",
            "pages": [{"path": "wiki/x/y", "sections": [{"heading": "H", "lines": [1]}]}],
        },
        {"sources": [SHED], "reason": "Filed", "pages": []},  # nothing to file
    ],
)
async def test_file_rejects_bad_payloads(client: httpx2.AsyncClient, payload: object) -> None:
    await client.post("/import", json={"sources": [_chat()]})
    assert (await client.post("/file", json=payload)).status_code == 400


async def test_file_needs_the_portal_token(client: httpx2.AsyncClient) -> None:
    wrong = {"Authorization": f"Bearer {LIBRECHAT_TOKEN}"}
    r = await client.post("/file", json={}, headers=wrong)
    assert r.status_code == 401
    assert (await client.put("/file", json={})).status_code == 405


# ------------------------------------------------------------------ graph


async def test_graph_pages_raw_and_versions(client: httpx2.AsyncClient) -> None:
    await client.post("/import", json={"sources": [_chat()]})
    body = (await client.get("/graph")).json()
    ids = {n["id"] for n in body["nodes"]}
    assert "wiki/concepts/company-brain" in ids and SHED not in ids
    assert body["skipped_page"] == "wiki/references/skipped-conversations"
    assert all({"source", "target"} <= set(e) for e in body["edges"])
    node = next(n for n in body["nodes"] if n["id"] == "wiki/concepts/company-brain")
    assert set(node) >= {"id", "title", "type", "folder", "tags", "backlink_count"}

    raw = (await client.get("/graph", params={"include": "raw,proposals"})).json()
    shed = next(n for n in raw["nodes"] if n["id"] == SHED)
    assert shed["unprocessed"] is True and shed["conversation"] == SHED
    assert raw["version"] != body["version"]  # proposals counted in the version

    same = (await client.get("/graph", params={"since": body["version"]})).json()
    assert same == {"version": body["version"], "unchanged": True}
    await client.post("/import", json={"sources": [_chat("claude-chat:another")]})
    moved = (await client.get("/graph", params={"since": body["version"]})).json()
    assert "unchanged" not in moved and moved["version"] != body["version"]


async def test_graph_is_read_only(client: httpx2.AsyncClient) -> None:
    assert (await client.post("/graph", json={})).status_code == 405
    wrong = {"Authorization": f"Bearer {LIBRECHAT_TOKEN}"}
    assert (await client.get("/graph", headers=wrong)).status_code == 401


# ------------------------------------------------------------------ private pages


async def test_file_private_pages_and_sources(client: httpx2.AsyncClient) -> None:
    await client.post("/import", json={"sources": [_chat(), _chat("claude-chat:skipme")]})
    page = {
        "path": "wiki/people/sister",
        "type": "person",
        "title": "Sister",
        "description": "The owner's sister.",
        "sections": [{"heading": "Feelings", "lines": ["- 2026-09: Misses her"]}],
        "private": True,
    }
    payload = {"sources": [SHED], "reason": "Filed", "pages": [page], "private_sources": True}
    r = await client.post("/file", json=payload)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"commit", "created", "updated", "unchanged", "proposed"}
    doc = (await client.get("/doc", params={"id": "wiki/people/sister"})).json()
    assert doc["private"] is True and doc["frontmatter"]["private"] is True
    assert (await client.get("/doc", params={"id": SHED})).json()["private"] is True
    docs = {d["id"]: d for d in (await client.get("/docs")).json()["docs"]}
    assert docs["wiki/people/sister"]["private"] is True
    assert docs["wiki/concepts/company-brain"]["private"] is False
    index = (await client.get("/index")).json()
    assert "Sister" not in index["markdown"]  # index.md is for every client
    assert {d["id"]: d["private"] for d in index["docs"]}["wiki/people/sister"] is True
    hits = (await client.get("/search", params={"q": "sister"})).json()["results"]
    assert hits[0]["id"] == "wiki/people/sister" and hits[0]["private"] is True
    graph = (await client.get("/graph", params={"include": "raw"})).json()
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes["wiki/people/sister"]["private"] is True and nodes[SHED]["private"] is True

    skip = {
        "sources": ["raw/chats/claude/skipme"],
        "reason": "Nothing to file",
        "skip": "feelings only",
        "private_sources": True,
    }
    assert (await client.post("/file", json=skip)).status_code == 200
    body = (await client.get("/doc", params={"id": "wiki/references/skipped-conversations"})).json()
    assert "- [Private conversation](/raw/chats/claude/skipme.md)" in body["body"]
    assert "Garden shed" not in body["body"] and "feelings only" not in body["body"]
    assert (await client.get("/unprocessed")).json()["total"] == 0
    for bad in (
        {**payload, "private_sources": "yes"},
        {**payload, "pages": [{**page, "private": 1}]},
    ):
        assert (await client.post("/file", json=bad)).status_code == 400

    # LibreChat (no wiki:private) sees none of it over MCP.
    root = str(client.base_url).rstrip("/").removesuffix(API)
    async with mcp_client(f"{root}/mcp", LIBRECHAT_TOKEN) as mcp:
        found = structured(await mcp.call_tool("search_wiki", {"query": "sister"}))
        assert found["results"] == []
        res = await mcp.call_tool("read_concept", {"path": "wiki/people/sister"})
        assert res.is_error and "no page at" in res.content[0].text


# ------------------------------------------------------------------ connected apps


async def test_apps_and_private_access_toggle(runtime: Runtime) -> None:
    db = runtime.settings.state_dir / "oauth.sqlite"
    oauth = OAuthProvider(db, issuer="", resource="", audit=runtime.audit, owner_username="")
    for cid in ("gpt-1", "gpt-2"):  # one app, two client_ids
        await oauth.register_client(
            OAuthClientInformationFull(
                client_id=cid,
                client_name="ChatGPT",
                redirect_uris=["https://chatgpt.com/connector/oauth/cb"],  # type: ignore[list-item]
            )
        )
    oauth._issue("gpt-2", ["wiki:read", "wiki:private"], None, "f2")
    app, _ = build_internal_app(
        runtime.deps,
        token=LIBRECHAT_TOKEN,
        client_name="librechat",
        scopes=["wiki:read", "wiki:write"],
        allowed_hosts=["127.0.0.1:*"],
        portal_token=PORTAL_TOKEN,
        oauth_db=db,
    )
    async with (
        serve_app(app) as url,
        httpx2.AsyncClient(
            base_url=url + API, headers={"Authorization": f"Bearer {PORTAL_TOKEN}"}
        ) as http,
    ):
        body = (await http.get("/apps")).json()
        assert body["internal"] == {
            "label": "librechat",
            "private": False,
            "toggle": None,
            "consented": False,
        }
        [gpt] = body["apps"]
        assert gpt["label"] == "chatgpt" and gpt["kind"] == "dcr" and gpt["active_grants"] == 1
        assert gpt["consented"] and gpt["toggle"] == "default" and gpt["private"]
        assert {"created", "last_used"} <= gpt.keys()

        r = await http.post("/apps/private", json={"label": "chatgpt", "access": "off"})
        assert r.status_code == 200 and r.json()["private"] is False
        assert oauth.private_toggle("chatgpt") == "off"
        assert (await http.get("/apps")).json()["apps"][0]["toggle"] == "off"
        [change] = [e for e in runtime.audit.recent() if e["kind"] == "private_access"]
        assert change["by"] == "portal" and change["access"] == "off"

        for bad in (
            {"label": "nosuch", "access": "on"},
            {"label": "chatgpt", "access": "yes"},
            {"label": "chatgpt"},
            {"label": 3, "access": "on"},
            ["chatgpt", "on"],
        ):
            r = await http.post("/apps/private", json=bad)
            assert r.status_code == 400, bad
        r = await http.post("/apps/private", json={"label": "nosuch", "access": "on"})
        assert "no connected app" in r.json()["error"]
        assert oauth.private_toggle("chatgpt") == "off"

        # LibreChat takes the same toggle, applied per request, without a restart.
        async def librechat_may_unmark() -> bool:
            async with mcp_client(f"{url}/mcp", LIBRECHAT_TOKEN) as mcp:
                res = await mcp.call_tool(
                    "set_private", {"path": "wiki/none", "private": False, "reason": "Test"}
                )
                return "no page at" in res.content[0].text  # past the scope check

        assert not await librechat_may_unmark()
        r = await http.post("/apps/private", json={"label": "librechat", "access": "on"})
        assert r.status_code == 200 and r.json() == {
            "label": "librechat",
            "toggle": "on",
            "private": True,
        }
        assert (await http.get("/apps")).json()["internal"]["private"] is True
        assert await librechat_may_unmark()
        r = await http.post("/apps/private", json={"label": "librechat", "access": "default"})
        assert r.json()["private"] is None and not await librechat_may_unmark()
        wrong = {"Authorization": f"Bearer {LIBRECHAT_TOKEN}"}
        r = await http.post(
            "/apps/private", json={"label": "chatgpt", "access": "on"}, headers=wrong
        )
        assert r.status_code == 401 and oauth.private_toggle("chatgpt") == "off"
    oauth.close()


async def test_apps_without_oauth_state(client: httpx2.AsyncClient) -> None:
    assert (await client.get("/apps")).json()["apps"] == []
    r = await client.post("/apps/private", json={"label": "chatgpt", "access": "on"})
    assert r.status_code == 400


async def test_owner_decides_identity_merges(client: httpx2.AsyncClient, runtime: Runtime) -> None:
    store, agent = runtime.store, Actor("chatgpt", Trust.AGENT)
    owner = {"type": "owner", "title": "Owner profile", "description": "The owner."}
    await store.write_concept("wiki/owner/profile", owner, "# Owner profile", "test", agent)
    for slug, title in (("maarten", "Maarten"), ("mvdh", "MvdH")):
        await store.write_concept(f"wiki/people/{slug}", person(title), "Me.", "import", agent)
    merge = await store.propose_identity_merge(
        "wiki/people/maarten", "wiki/owner/profile", "Same person", agent
    )
    [listed] = (await client.get("/proposals")).json()["proposals"]
    assert listed["action"] == "identity" and listed["duplicate"] == "wiki/people/maarten"

    for bad in ({"id": merge.proposal_id}, {"id": merge.proposal_id, "decision": "maybe"}, {}):
        assert (await client.post("/proposal", json=bad)).status_code == 400
    assert (
        await client.post("/proposal", json={"id": merge.proposal_id, "decision": "accept"})
    ).status_code == 400
    r = await client.post(
        "/proposal",
        json={
            "id": merge.proposal_id,
            "decision": "accept",
            "review_token": listed["review_token"],
        },
    )
    assert r.status_code == 200 and r.json()["status"] == "committed", r.text
    doc = (await client.get("/doc", params={"id": "wiki/people/maarten"})).json()
    assert doc["frontmatter"]["canonical"] == "wiki/owner/profile"
    docs = (await client.get("/index")).json()["docs"]
    [row] = [d for d in docs if d["id"] == "wiki/owner/profile"]
    assert row["aliases"] == ["Maarten"] and row["canonical"] is None

    other = await store.propose_identity_merge(
        "wiki/people/mvdh", "wiki/owner/profile", "Same person", agent
    )
    r = await client.post("/proposal", json={"id": other.proposal_id, "decision": "reject"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    doc = (await client.get("/doc", params={"id": "wiki/people/mvdh"})).json()
    assert "canonical" not in doc["frontmatter"]
    assert (await client.get("/proposals")).json()["proposals"] == []


async def test_non_merge_approval_still_works_without_a_preview_token(
    client: httpx2.AsyncClient, runtime: Runtime
) -> None:
    result = await runtime.store.write_concept(
        "wiki/concepts/proposed-note",
        {"type": "concept", "title": "Proposed note", "description": "A note from a source."},
        "A proposed fact.",
        "Source evidence",
        Actor("web", Trust.EXTERNAL),
    )
    assert result.proposal_id
    response = await client.post("/proposal", json={"id": result.proposal_id, "decision": "accept"})
    assert response.status_code == 200 and response.json()["status"] == "committed"
