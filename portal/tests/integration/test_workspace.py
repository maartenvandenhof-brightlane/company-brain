"""The wiki workspace's JSON endpoints (/wiki/api/*) and the graph page, against a real
wiki-mcp: graph, explorer tree, page fragments, unlinked mentions and cache invalidation."""

from __future__ import annotations

import gzip
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx2
import pytest
from wiki_mcp.config import Settings as WikiSettings
from wiki_mcp.config import Trust
from wiki_mcp.internal import build_internal_app
from wiki_mcp.runtime import create_runtime
from wiki_mcp.wiki.actor import Actor
from wiki_mcp.wiki.observations import page_id as observation_page
from wiki_mcp.wiki.store import ChatSource, FilingPage, FilingSection

from portal.app import build_app
from portal.config import Settings
from portal.wikiclient import WikiClient
from tests.conftest import _find, login, serve_app
from tests.htmlcheck import active_content

PORTAL_TOKEN = "p" * 40
AGENT = Actor("test", Trust.AGENT)
PORTAL = Actor("portal", Trust.AGENT)
LONG = "raw/chats/claude/long-chat"
# the long chat's observation page (a draft, private): never listed by the workspace
OBSERVED = {
    "classification": {"types": ["planning"], "contexts": ["hobbies"]},
    "observations": [
        {
            "kind": "action",
            "basis": "reported",
            "text": "The owner planned a trip.",
            "msgs": [1],
            "quote": "Message 0 of part 1",
            "event": {"summary": "Planned a trip to Lisbon", "when": "unknown", "key": ""},
            "span": "state",
            "context": "hobbies",
            "valence": "neutral",
        }
    ],
}

HOSTILE = """<script>alert("owned")</script>
<img src=x onerror=alert(1)>
[click](javascript:alert(document.cookie)) ![tracker](https://evil.example/pixel.gif)
<a href="#" onclick="steal()">x</a> <style>body{display:none}</style>
"""


def _transcript(part: int) -> str:
    """A long transcript part, as the importer writes one, with hostile content inside."""
    turns = []
    for i in range(60):
        who = "User" if i % 2 == 0 else "Assistant"
        turns.append(
            f"## {who} · 2026-03-0{1 + part} 10:{i:02d}\n\n"
            f"Message {i} of part {part}: we talked about [Lisbon](/wiki/projects/lisbon.md) "
            f"and the [shed](../../../wiki/projects/shed.md).\n\n"
            + (HOSTILE if i % 10 == 3 else "")
            + ("```python\nprint('code')\n```\n" if i % 7 == 0 else "")
        )
    return "\n".join(turns)


@pytest.fixture
async def wiki_url(tmp_path: Path) -> AsyncIterator[str]:
    rt = await create_runtime(
        WikiSettings(
            wiki_dir=tmp_path / "wiki",
            state_dir=tmp_path / "wiki-state",
            types_file=_find("config/wiki-types.yaml"),
            seed_dir=_find("wiki-mcp/seed"),
        ),
        run_background=False,
    )
    store = rt.store
    await store.import_chats(
        [
            ChatSource("claude-chat:long-chat", "Long chat", _transcript(p), p, 3, messages=180)
            for p in (1, 2, 3)
        ]
        + [
            ChatSource("claude-chat:idle", "Idle chat", "## User\n\nHi", messages=1),
            ChatSource("chatgpt-chat:small-talk", "Small talk", "## User\n\nHey", messages=1),
        ],
        PORTAL,
    )
    await store.file_conversation(
        [LONG],
        [
            FilingPage(
                "wiki/projects/lisbon",
                (FilingSection("Timeline", ("- 2026-03: Planned a trip",)),),
                type="project",
                title="Lisbon",
                description="A trip to Lisbon.",
            )
        ],
        "filed",
        PORTAL,
    )
    await store.file_conversation(
        ["raw/chats/chatgpt/small-talk"], [], "nothing", PORTAL, skip="small talk"
    )
    observed = await store.write_observations(LONG, "observe-1", OBSERVED, PORTAL)
    assert observed["status"] == "committed"
    person = {
        "type": "person",
        "title": "Jane <b>Doe</b>",
        "description": "Plans trips.",
        "tags": ["test"],
        "status": "stable",
    }
    await store.write_concept(
        "wiki/people/jane-doe",
        person,
        "# Jane\n\nJane is going to [Lisbon](/wiki/projects/lisbon.md), see [x](/wiki/x/gone.md)."
        "\n\n## Plans\n\nText.\n\n" + HOSTILE,
        "test",
        AGENT,
    )
    await store.write_concept(
        "wiki/people/john-roe",
        {**person, "title": "John Roe"},
        "John talks about Lisbon a lot, and about Jane <b>Doe</b> too.",
        "test",
        AGENT,
    )
    app, _ = build_internal_app(
        rt.deps,
        token="l" * 40,
        client_name="librechat",
        scopes=["wiki:read"],
        allowed_hosts=["127.0.0.1:*"],
        portal_token=PORTAL_TOKEN,
    )
    async with serve_app(app) as url:
        yield url + "/api/portal/v1"
    await rt.close()


@pytest.fixture
async def wiki(wiki_url: str) -> AsyncIterator[WikiClient]:
    client = WikiClient(wiki_url, PORTAL_TOKEN)
    yield client
    await client.close()


@pytest.fixture
async def http(
    wiki: WikiClient, make_settings: Callable[..., Settings]
) -> AsyncIterator[httpx2.AsyncClient]:
    app = build_app(make_settings(), wiki=wiki)
    async with serve_app(app) as url, httpx2.AsyncClient(base_url=url) as client:
        await login(client)
        yield client


def _structural(fragment: str) -> list[str]:
    """Active content in a whole fragment, outside the templates' own <div>s and icons
    (the article inside it is checked strictly, see _article)."""
    return [p for p in active_content(fragment) if p not in ("<div>", "<svg>")]


def _article(fragment: str) -> str:
    return fragment.split('<article class="prose">', 1)[1].split("</article>", 1)[0]


# ------------------------------------------------------------------ access


async def test_api_needs_a_session_and_the_same_origin(
    wiki: WikiClient, make_settings: Callable[..., Settings]
) -> None:
    app = build_app(make_settings(), wiki=wiki)
    async with serve_app(app) as url, httpx2.AsyncClient(base_url=url) as client:
        for path in (
            "/wiki/api/graph",
            "/wiki/api/tree",
            "/wiki/api/fragment/wiki/people/jane-doe",
        ):
            r = await client.get(path)
            assert r.status_code == 401 and r.json()["error"], path  # not a login redirect
        await login(client)
        cross = await client.get("/wiki/api/graph", headers={"Sec-Fetch-Site": "cross-site"})
        assert cross.status_code == 403
        same = await client.get("/wiki/api/graph", headers={"Sec-Fetch-Site": "same-origin"})
        assert same.status_code == 200
        assert same.headers["cache-control"] == "no-store"
        assert (await client.post("/wiki/api/graph")).status_code == 405


async def test_background_refresh_does_not_keep_the_session_alive(
    http: httpx2.AsyncClient,
) -> None:
    r = await http.get("/wiki/api/graph", headers={"X-Portal-Background": "1"})
    assert r.status_code == 200


# ------------------------------------------------------------------ graph


async def test_graph_variants(http: httpx2.AsyncClient) -> None:
    body = (await http.get("/wiki/api/graph")).json()
    nodes = {n["id"]: n for n in body["nodes"]}
    assert "wiki/people/jane-doe" in nodes and not [i for i in nodes if i.startswith("raw/")]
    assert nodes["wiki/projects/lisbon"]["draft"] is True
    assert nodes["wiki/x/gone"]["ghost"] is True
    assert body["skipped_page"] == "wiki/references/skipped-conversations"
    assert all(e["source"] in nodes and e["target"] in nodes for e in body["edges"])

    raw = (await http.get("/wiki/api/graph", params={"raw": "1"})).json()
    rnodes = {n["id"]: n for n in raw["nodes"]}
    assert rnodes["raw/chats/claude/idle"]["unprocessed"] is True
    assert rnodes["raw/chats/chatgpt/small-talk"]["skipped"] is True
    assert rnodes[f"{LONG}-part-3"]["conversation"] == LONG
    assert "unprocessed" not in rnodes[LONG]

    same = (await http.get("/wiki/api/graph", params={"v": body["version"]})).json()
    assert same == {"version": body["version"], "unchanged": True}


async def test_graph_is_gzipped_when_asked(http: httpx2.AsyncClient) -> None:
    r = await http.get("/wiki/api/graph", params={"raw": "1"}, headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip"
    assert "raw/chats/claude/idle" in r.text  # httpx decodes it


async def test_graph_refreshes_after_an_import(http: httpx2.AsyncClient, wiki: WikiClient) -> None:
    before = (await http.get("/wiki/api/graph", params={"raw": "1"})).json()
    await wiki.import_chats(
        [{"key": "claude-chat:fresh", "title": "Fresh", "content": "## User\n\nNew"}]
    )
    after = (await http.get("/wiki/api/graph", params={"raw": "1", "v": before["version"]})).json()
    assert after["version"] != before["version"]
    fresh = next(n for n in after["nodes"] if n["id"] == "raw/chats/claude/fresh")
    assert fresh["unprocessed"] is True
    tree = (await http.get("/wiki/api/tree", params={"folder": "raw/chats/claude"})).json()
    assert "raw/chats/claude/fresh" in [i["id"] for i in tree["items"]]


async def test_graph_page_lists_pages_and_links(http: httpx2.AsyncClient) -> None:
    r = await http.get("/wiki/graph")
    assert r.status_code == 200
    assert "data-graph-mount" in r.text and 'id="graph-list"' in r.text
    assert 'href="/wiki/page/wiki/projects/lisbon"' in r.text
    assert "Jane &lt;b&gt;Doe&lt;/b&gt;" in r.text
    assert "wiki/x/gone" in r.text and "(missing page)" in r.text
    assert " style=" not in r.text and "<script>" not in r.text
    assert (await http.get("/wiki/graph?page=99")).status_code == 200
    # a nearly empty wiki: no links to the chat import, which is not wired
    empty = r.text.split('class="empty"', 1)[1].split("</section>", 1)[0]
    assert "nearly empty" in empty and "/import" not in empty


# ------------------------------------------------------------------ explorer


async def test_tree_levels(http: httpx2.AsyncClient) -> None:
    root = (await http.get("/wiki/api/tree")).json()
    names = [f["path"] for f in root["folders"]]
    assert names[:2] == ["wiki", "raw"]
    raw = next(f for f in root["folders"] if f["path"] == "raw")
    assert raw["unprocessed"] == 1 and raw["count"] == 5
    wiki = next(f for f in root["folders"] if f["path"] == "wiki")
    assert wiki["drafts"] == 1

    claude = (await http.get("/wiki/api/tree", params={"folder": "raw/chats/claude"})).json()
    items = {i["id"]: i for i in claude["items"]}
    long = items[LONG]
    assert long["kind"] == "conversation" and long["title"] == "Long chat"
    assert [p["id"] for p in long["parts"]] == [LONG, f"{LONG}-part-2", f"{LONG}-part-3"]
    assert long["parts"][1]["label"] == "Conversation, part 2"
    assert items["raw/chats/claude/idle"]["unprocessed"] is True
    assert claude["total"] == 2 and claude["next_offset"] is None

    paged = (
        await http.get("/wiki/api/tree", params={"folder": "raw/chats/claude", "limit": 1})
    ).json()
    assert len(paged["items"]) == 1 and paged["next_offset"] == 1
    rest = (
        await http.get(
            "/wiki/api/tree", params={"folder": "raw/chats/claude", "limit": 1, "offset": 1}
        )
    ).json()
    assert rest["next_offset"] is None and rest["folders"] == []
    assert (await http.get("/wiki/api/tree", params={"folder": "nope"})).status_code == 400


async def test_tree_filters(http: httpx2.AsyncClient) -> None:
    found = (await http.get("/wiki/api/tree", params={"q": "lisbon"})).json()
    assert [i["id"] for i in found["items"]] == ["wiki/projects/lisbon"]
    drafts = (await http.get("/wiki/api/tree", params={"only": "drafts"})).json()
    assert [i["id"] for i in drafts["items"]] == ["wiki/projects/lisbon"]
    todo = (await http.get("/wiki/api/tree", params={"only": "unprocessed"})).json()
    assert [i["id"] for i in todo["items"]] == ["raw/chats/claude/idle"]
    assert todo["transcripts"] == 1


async def test_observation_pages_are_not_browsed(http: httpx2.AsyncClient) -> None:
    observed = observation_page(LONG)
    assert observed.startswith("wiki/observations/")
    for params in ({}, {"raw": "1", "proposals": "1"}):
        body = (await http.get("/wiki/api/graph", params=params)).json()
        ids = {n["id"] for n in body["nodes"]}
        assert not [i for i in ids if i.startswith("wiki/observations/")]
        assert all(e["source"] in ids and e["target"] in ids for e in body["edges"])
    wiki = (await http.get("/wiki/api/tree", params={"folder": "wiki"})).json()
    assert "wiki/observations" not in [f["path"] for f in wiki["folders"]]
    assert (await http.get("/wiki/api/tree", params={"folder": "wiki/observations"})).is_error
    for params in ({"q": "observ"}, {"only": "drafts", "folder": "wiki"}):
        found = (await http.get("/wiki/api/tree", params=params)).json()
        assert observed not in [i["id"] for i in found["items"]]
    assert observed not in (await http.get("/wiki/graph")).text
    # only hidden from browsing: the page itself still opens
    r = await http.get(f"/wiki/api/fragment/{observed}")
    assert r.status_code == 200 and r.json()["type"] == "observation"


# ------------------------------------------------------------------ fragments


async def test_page_fragment(http: httpx2.AsyncClient) -> None:
    r = await http.get("/wiki/api/fragment/wiki/people/jane-doe")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["kind"] == "page" and body["id"] == "wiki/people/jane-doe"
    assert body["title"] == "Jane <b>Doe</b>" and body["type"] == "person"
    assert body["url"] == "/wiki/page/wiki/people/jane-doe"
    assert _structural(body["html"]) == [] and _structural(body["info_html"]) == []
    assert active_content(_article(body["html"])) == []
    assert "&lt;script&gt;alert" in body["html"]
    assert "Jane &lt;b&gt;Doe&lt;/b&gt;" in body["html"]
    assert 'href="/wiki/page/wiki/x/gone" class="broken"' in body["html"]
    assert "Backlinks" in body["info_html"] and "Properties" in body["info_html"]
    assert {"id": "wiki/x/gone", "exists": False, "type": ""} in body["outlinks"]
    assert body["frontmatter"]["status"] == "stable" and body["draft"] is False
    # the same markup as the full page
    full = (await http.get("/wiki/page/wiki/people/jane-doe")).text
    assert _article(body["html"]) == _article(full)


async def test_long_imported_transcript_fragments(http: httpx2.AsyncClient) -> None:
    for pid in (LONG, f"{LONG}-part-2", f"{LONG}-part-3"):
        body = (await http.get(f"/wiki/api/fragment/{pid}")).json()
        assert body["raw"] is True and body["type"] == "source"
        assert _structural(body["html"]) == [] and _structural(body["info_html"]) == []
        article = _article(body["html"])
        assert active_content(article) == [], pid
        assert article.count("&lt;script&gt;") == 6
        assert "Message 59" in article
        assert "transcript" in body["html"]  # the transcript styling


async def test_draft_fragment(http: httpx2.AsyncClient) -> None:
    body = (await http.get("/wiki/api/fragment/wiki/projects/lisbon")).json()
    assert body["draft"] is True and body["status"] == "draft"
    backlinks = [b["id"] for b in body["backlinks"]]
    assert "wiki/people/jane-doe" in backlinks and f"{LONG}-part-2" in backlinks


async def test_missing_fragment_is_json(http: httpx2.AsyncClient) -> None:
    r = await http.get("/wiki/api/fragment/wiki/people/nobody")
    assert r.status_code == 404 and r.json()["error"]
    assert (await http.get("/wiki/api/fragment/../../etc/passwd")).status_code in (400, 404)


async def test_view_fragments(http: httpx2.AsyncClient) -> None:
    recent = (await http.get("/wiki/api/view", params={"path": "/wiki/recent"})).json()
    assert recent["kind"] == "view" and recent["title"] == "Recent changes"
    assert _structural(recent["html"]) == [] and recent["url"] == "/wiki/recent"
    typed = (await http.get("/wiki/api/view", params={"path": "/wiki/type/person"})).json()
    assert typed["title"] == "People" and "John Roe" in typed["html"]
    search = (await http.get("/wiki/api/view", params={"path": "/wiki/search?q=lisbon"})).json()
    assert search["url"] == "/wiki/search?q=lisbon" and "results" in search["html"]
    tagged = (
        await http.get("/wiki/api/view", params={"path": "/wiki/tag/from-chat-import"})
    ).json()
    assert tagged["title"] == "Tag: from-chat-import" and "Lisbon" in tagged["html"]
    redirect = (await http.get("/wiki/api/view", params={"path": "/wiki/page/index"})).json()
    assert redirect == {"redirect": "/wiki"}
    page = await http.get("/wiki/api/view", params={"path": "/wiki/page/wiki/people/john-roe"})
    assert page.json()["id"] == "wiki/people/john-roe"
    for bad in ("/import", "https://evil.example/wiki", "/wiki/api/graph", ""):
        assert (await http.get("/wiki/api/view", params={"path": bad})).status_code == 400, bad
    assert (await http.get("/wiki/api/view", params={"path": "/wiki/nope"})).status_code == 404


# ------------------------------------------------------------------ mentions


async def test_unlinked_mentions(http: httpx2.AsyncClient) -> None:
    body = (await http.get("/wiki/api/mentions/wiki/projects/lisbon")).json()
    ids = [m["id"] for m in body["mentions"]]
    assert "wiki/people/john-roe" in ids  # says Lisbon without a link
    assert "wiki/people/jane-doe" not in ids  # links to it
    assert "wiki/projects/lisbon" not in ids
    assert "<mark>" in body["mentions"][0]["snippet_html"]
    assert "<script" not in json.dumps(body)


def test_gzip_roundtrip() -> None:
    from portal.workspace import encode

    raw, packed = encode({"a": "é"})
    assert gzip.decompress(packed) == raw


def test_plain_snippets() -> None:
    from portal.workspace import plain_snippet

    raw = "…health.md). - Related to [**Ada** Bakker 2](/wiki/people/ada-bakker-2.md). ## Timeline"
    assert plain_snippet(raw) == "…. - Related to **Ada** Bakker 2. Timeline"


async def test_the_portal_opens_on_the_workspace(http: httpx2.AsyncClient) -> None:
    landing = await http.get("/")
    assert landing.status_code == 303 and landing.headers["location"] == "/wiki"
    wiki = (await http.get("/wiki")).text
    assert 'href="/home"' not in wiki and "/import" not in wiki
    assert 'href="/wiki/graph"' in wiki and 'src="/static/js/theme-init.js"' in wiki
