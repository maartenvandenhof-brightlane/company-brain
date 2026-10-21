"""Wiki pages end to end: a real wiki-mcp internal listener behind the portal."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from wiki_mcp.config import Settings as WikiSettings
from wiki_mcp.config import Trust
from wiki_mcp.internal import build_internal_app
from wiki_mcp.runtime import Runtime, create_runtime
from wiki_mcp.wiki.actor import Actor
from wiki_mcp.wiki.store import ChatSource

import portal.wikiclient
from portal.app import build_app
from portal.config import Settings
from portal.wikiclient import WikiClient
from tests.conftest import ORIGIN, _find, csrf_of, login, serve_app
from tests.htmlcheck import active_content

PORTAL_TOKEN = "p" * 40

EVIL_BODY = """# Jane Doe

Works with [the second brain](/wiki/concepts/company-brain.md) and [nobody](/wiki/people/nobody.md).

<script>alert("owned")</script>
<img src=x onerror=alert(1)>
[click](javascript:alert(document.cookie))
![tracker](https://evil.example/pixel.gif)
"""


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    settings = WikiSettings(
        wiki_dir=tmp_path / "wiki",
        state_dir=tmp_path / "wiki-state",
        types_file=_find("config/wiki-types.yaml"),
        seed_dir=_find("wiki-mcp/seed"),
    )
    rt = await create_runtime(settings, run_background=False)
    person = {
        "type": "person",
        "title": "Jane <b>Doe</b>",
        "description": "A person with a hostile page body.",
        "tags": ["test"],
        "related": ["wiki/concepts/company-brain"],
    }
    await rt.store.write_concept(
        "wiki/people/jane-doe", person, EVIL_BODY, "test page", Actor("test", Trust.AGENT)
    )
    await rt.store.write_concept(
        "wiki/people/john-roe",
        {**person, "title": "John Roe", "related": []},
        "Plain page.",
        "external suggestion",
        Actor("test", Trust.EXTERNAL),
    )
    await rt.store.import_chats(
        [ChatSource("claude-chat:trip-1", "Trip", "## User\n\nWe booked Lisbon.", messages=1)],
        Actor("portal", Trust.AGENT),
    )
    trip = {
        "type": "project",
        "title": "Lisbon trip",
        "description": "A trip planned in a chat.",
        "sources": ["raw/chats/claude/trip-1"],
    }
    await rt.store.write_concept(
        "wiki/projects/lisbon-trip",
        trip,
        "Booked.",
        "from an imported chat",
        Actor("t", Trust.AGENT),
    )
    yield rt
    await rt.close()


@pytest.fixture
async def wiki_api(runtime: Runtime) -> AsyncIterator[str]:
    rt = runtime
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


@pytest.fixture
async def client(
    wiki_api: str, make_settings: Callable[..., Settings]
) -> AsyncIterator[httpx2.AsyncClient]:
    app = build_app(make_settings(), wiki=WikiClient(wiki_api, PORTAL_TOKEN))
    async with serve_app(app) as url, httpx2.AsyncClient(base_url=url) as http:
        await login(http)
        yield http


async def test_wiki_requires_login(wiki_api: str, make_settings: Callable[..., Settings]) -> None:
    app = build_app(make_settings(), wiki=WikiClient(wiki_api, PORTAL_TOKEN))
    async with serve_app(app) as url, httpx2.AsyncClient(base_url=url) as http:
        for path in ("/wiki", "/wiki/page/wiki/people/jane-doe", "/wiki/search?q=x", "/docs"):
            assert (await http.get(path)).status_code == 303, path


async def test_index_and_types(client: httpx2.AsyncClient) -> None:
    r = await client.get("/wiki")
    assert r.status_code == 200
    assert 'href="/wiki/page/wiki/people/jane-doe"' in r.text
    assert 'href="/wiki/type/person"' in r.text
    r = await client.get("/wiki/type/person")
    assert "Jane &lt;b&gt;Doe&lt;/b&gt;" in r.text
    assert (await client.get("/wiki/type/nope")).status_code == 404


async def test_hostile_page_is_neutralised(client: httpx2.AsyncClient) -> None:
    r = await client.get("/wiki/page/wiki/people/jane-doe")
    assert r.status_code == 200
    main = r.text.split("<main", 1)[1]
    article = main.split('<article class="prose">', 1)[1].split("</article>", 1)[0]
    assert active_content(article) == []
    assert "&lt;script&gt;alert" in main  # shown as text, never run
    assert "Jane &lt;b&gt;Doe&lt;/b&gt;" in main  # title escaped
    assert 'href="/wiki/page/wiki/concepts/company-brain"' in r.text
    assert 'href="/wiki/page/wiki/people/nobody" class="broken"' in r.text
    # frontmatter: related ids become links
    assert "<dt>related</dt>" in r.text


async def test_no_inline_styles_or_handlers(client: httpx2.AsyncClient) -> None:
    """The CSP forbids inline styles and scripts: the templates must not rely on them."""
    for path in (
        "/wiki",
        "/wiki/page/wiki/people/jane-doe",
        "/wiki/search?q=jane",
        "/docs",
        "/wiki/drafts",
        "/wiki/graph",
        "/home",
        "/settings/assistant",
    ):
        page = (await client.get(path)).text
        assert " style=" not in page and "<style" not in page, path
        assert " onclick=" not in page and "<script>" not in page, path
        assert not re.search(r"<[^>]*\son[a-z]+\s*=", page), path


async def test_type_navigation(client: httpx2.AsyncClient) -> None:
    page = (await client.get("/wiki/page/wiki/people/jane-doe")).text
    # every wiki page carries the navigation by type, coloured by data-type
    assert '<a href="/wiki/type/person" data-type="person"' in page
    assert 'data-type="person"' in page.split("<main", 1)[1].split(">", 1)[0]
    # managed types with pages (guides, raw sources) are listed and have a page too
    assert 'href="/wiki/type/guide"' in page
    r = await client.get("/wiki/type/guide")
    assert r.status_code == 200 and 'href="/wiki/page/AGENTS"' in r.text


async def test_index_hides_frontmatter(client: httpx2.AsyncClient) -> None:
    assert "okf_version" not in (await client.get("/wiki")).text


async def test_static_files_are_revalidated(client: httpx2.AsyncClient) -> None:
    r = await client.get("/static/css/tokens.css")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    assert "--type-person" in r.text and "--type-source" in r.text
    assert (await client.get("/static/portal.css")).status_code == 200  # probed by the scripts
    assert (await client.get("/wiki")).headers["cache-control"] == "no-store"


async def test_backlinks(client: httpx2.AsyncClient) -> None:
    r = await client.get("/wiki/page/wiki/concepts/company-brain")
    assert "Backlinks" in r.text
    assert 'href="/wiki/page/wiki/people/jane-doe"' in r.text


async def test_bundle_paths(client: httpx2.AsyncClient) -> None:
    ok = await client.get("/wiki/page/wiki/concepts/company-brain.md")
    assert ok.status_code == 200
    for bad in ("/wiki/page/../../etc/passwd", "/wiki/page/.git/config", "/wiki/page/wiki/x/"):
        assert (await client.get(bad)).status_code == 404, bad
    r = await client.get("/wiki/page/log")
    assert r.status_code == 303 and r.headers["location"] == "/wiki/log"
    assert (await client.get("/wiki/page/AGENTS")).status_code == 200


async def test_search_log_recent_proposals(client: httpx2.AsyncClient) -> None:
    r = await client.get("/wiki/search", params={"q": "jane"})
    assert 'href="/wiki/page/wiki/people/jane-doe"' in r.text and "<mark>" in r.text
    assert active_content(r.text.split('<section id="results"', 1)[1]) == []
    r = await client.get("/wiki/search", params={"q": "<script>"})
    results = r.text.split('<section id="results"', 1)[1]  # after every form on the page
    assert r.status_code == 200 and active_content(results) == []
    assert "Wiki Update Log" in (await client.get("/wiki/log")).text
    recent = (await client.get("/wiki/recent")).text
    assert "test page" in recent and 'href="/wiki/page/wiki/people/jane-doe"' in recent
    proposals = (await client.get("/wiki/proposals")).text
    assert "john-roe" in proposals and 'href="/wiki/page/proposals/' in proposals
    pid = proposals.split('href="/wiki/page/proposals/')[1].split('"')[0]
    page = await client.get(f"/wiki/page/proposals/{pid}")
    assert page.status_code == 200 and "waiting for review" in page.text


async def test_docs(client: httpx2.AsyncClient) -> None:
    r = await client.get("/docs")
    assert r.status_code == 200 and "Company Brain" in r.text
    assert 'href="/docs/security"' in r.text
    assert (await client.get("/docs/security")).status_code == 200
    assert (await client.get("/docs/nope")).status_code == 404


async def test_wiki_down(make_settings: Callable[..., Settings]) -> None:
    app = build_app(make_settings(), wiki=WikiClient("http://127.0.0.1:9/api", PORTAL_TOKEN))
    async with serve_app(app) as url, httpx2.AsyncClient(base_url=url) as http:
        await login(http)
        r = await http.get("/wiki")
        assert r.status_code == 502 and "not reachable" in r.text


async def test_drafts_from_imported_chats(client: httpx2.AsyncClient) -> None:
    sources = (await client.get("/wiki/type/source")).text
    assert 'href="/wiki/drafts"' in sources
    page = (await client.get("/wiki/drafts")).text
    assert '<h1 class="page-title">Drafts to review</h1>' in page
    assert 'href="/wiki/drafts" aria-current="page"' in page  # under To review
    assert 'href="/wiki/proposals">proposals</a>' in page  # and how it differs
    proposals = (await client.get("/wiki/proposals")).text
    assert 'href="/wiki/drafts">Drafts</a>' in proposals  # a note on how they differ
    assert 'href="/wiki/page/wiki/projects/lisbon-trip"' in page
    assert '<span class="pill plain">draft</span>' in page
    assert "raw/chats/claude/trip-1" not in page  # the transcript itself is not listed
    other = (await client.get("/wiki/tag/test")).text
    assert "Tag: test" in other and 'href="/wiki/page/wiki/people/jane-doe"' in other
    assert "No pages with this tag" in (await client.get("/wiki/tag/nothing-here")).text


async def test_drafts_follow_the_current_status(
    client: httpx2.AsyncClient, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only `status: draft` puts a page on the Drafts list; provenance (trust agent,
    generated by chatgpt, the from-chat-import tag) never does. A status change made over
    MCP (store.write_concept) shows without a restart."""
    clock = [1000.0]  # the portal client's clock only, not the event loop's
    monkeypatch.setattr(portal.wikiclient, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    chatgpt = Actor("chatgpt", Trust.AGENT)
    page = "wiki/projects/import-review"
    fm = {
        "type": "project",
        "title": "Import review",
        "description": "A page filed from an imported chat.",
        "tags": ["from-chat-import"],
    }

    async def drafts() -> set[str]:
        clock[0] += 11  # past the portal's graph cache: revalidated by version
        listed = (await client.get("/wiki/drafts")).text.split('id="unmarked"')[0]
        tree = (await client.get("/wiki/api/tree", params={"only": "drafts"})).json()
        in_tree = {i["id"] for i in tree["items"]}
        fragment = (await client.get(f"/wiki/api/fragment/{page}")).json()
        assert (page in in_tree) == fragment["draft"] == (f'href="/wiki/page/{page}"' in listed)
        return in_tree

    await runtime.store.write_concept(page, {**fm, "status": "draft"}, "Text.", "new", chatgpt)
    assert await drafts() == {page, "wiki/projects/lisbon-trip"}

    await runtime.store.write_concept(
        page, {**fm, "status": "stable"}, "Text.", "reviewed", chatgpt
    )
    assert await drafts() == {"wiki/projects/lisbon-trip"}
    view = (await client.get(f"/wiki/page/{page}")).text
    assert "data-draft" not in view and "Draft to review" not in view
    stored = (await client.get(f"/wiki/api/fragment/{page}")).json()["frontmatter"]
    assert stored["trust"] == "agent" and stored["generated"]["by"] == "chatgpt/wiki-mcp"
    assert stored["tags"] == ["from-chat-import"]  # provenance kept
    tagged = (await client.get("/wiki/tag/from-chat-import")).text  # a plain tag list now
    assert f'href="/wiki/page/{page}"' in tagged
    # stable with the tag: listed apart, since dreams wait for "Mark reviewed"
    unmarked = (await client.get("/wiki/drafts")).text.split('id="unmarked"')[1]
    assert f'href="/wiki/page/{page}"' in unmarked
    assert "Not marked reviewed" in view and 'action="/wiki/review"' in view

    await runtime.store.write_concept(page, {**fm, "status": "draft"}, "Text.", "again", chatgpt)
    assert page in await drafts()
    assert "Draft to review" in (await client.get(f"/wiki/page/{page}")).text


async def test_private_pages_show_a_badge(client: httpx2.AsyncClient, runtime: Runtime) -> None:
    """The portal shows private pages (it is the owner's own UI), marked as such."""
    owner = Actor("owner", Trust.OWNER)
    fm = {"type": "owner", "title": "Personal life", "description": "Feelings.", "private": True}
    await runtime.store.write_concept("wiki/owner/personal", fm, "Text.", "new", owner)
    view = (await client.get("/wiki/page/wiki/owner/personal")).text
    assert '<span class="pill plain" title="Only assistants granted' in view
    assert (await client.get("/wiki/api/fragment/wiki/owner/personal")).json()["private"] is True
    public = (await client.get("/wiki/page/wiki/people/jane-doe")).text
    assert "wiki:private" not in public
    assert (await client.get("/wiki/api/fragment/wiki/people/jane-doe")).json()["private"] is False


async def test_footnote_citations_render(client: httpx2.AsyncClient, runtime: Runtime) -> None:
    """Filed facts cite their chats as footnotes: shown as `[n]` links to the raw source,
    not as literal `[^id]` text (a private page in the filing format)."""
    owner = Actor("owner", Trust.OWNER)
    fm = {"type": "owner", "title": "Personal life", "description": "Feelings.", "private": True}
    chat, hidden = "d24e8f1a-0b2c-4d5e-8f9a-123456789abc", "7f3c2b1a-9e8d-4c7b-a6f5-0e1d2c3b4a59"
    body = (
        "## Mood\n\n"
        f"- 2026-01: Prefers kits over own designs. [^{chat}]\n"
        f"- 2026-02: Sleeps better since the move. [^{hidden}]\n\n"
        "## Sources\n\n"
        f"[^{chat}]: [Planning the shed](/raw/chats/claude/{chat}.md), conversation of 2026-01-10\n"
        f"[^{hidden}]: [Private conversation](/raw/chats/claude/{hidden}.md)\n"
    )
    await runtime.store.write_concept("wiki/owner/personal", fm, body, "new", owner)
    view = (await client.get("/wiki/page/wiki/owner/personal")).text
    fragment = (await client.get("/wiki/api/fragment/wiki/owner/personal")).json()["html"]
    for out in (view, fragment):
        assert "[^" not in out
        assert f'<sup class="footnote-ref"><a href="/wiki/page/raw/chats/claude/{chat}"' in out
        assert f'<sup class="footnote-ref"><a href="/wiki/page/raw/chats/claude/{hidden}"' in out
        assert ">Planning the shed</a>, conversation of 2026-01-10" in out
        assert ">Private conversation</a>" in out


async def test_mark_reviewed(client: httpx2.AsyncClient, runtime: Runtime) -> None:
    trip = "wiki/projects/lisbon-trip"
    listed = (await client.get("/wiki/drafts")).text
    assert f'name="id" value="{trip}"' in listed and "Mark reviewed</button>" in listed
    page = (await client.get(f"/wiki/page/{trip}")).text
    assert "Draft to review" in page and 'action="/wiki/review"' in page
    csrf = csrf_of(page)

    no_csrf = await client.post(
        "/wiki/review", data={"id": trip, "next": "/wiki/drafts"}, headers={"Origin": ORIGIN}
    )
    assert no_csrf.status_code == 403
    r = await client.post(
        "/wiki/review",
        data={"csrf": csrf, "id": trip, "next": "/wiki/drafts"},
        headers={"Origin": ORIGIN},
    )
    assert r.status_code == 303 and r.headers["location"] == "/wiki/drafts"
    _, doc, _ = runtime.store.read(trip)
    fm = doc.frontmatter or {}
    assert fm["status"] == "stable" and "from-chat-import" not in (fm.get("tags") or [])
    after = (await client.get("/wiki/drafts")).text
    assert f'href="/wiki/page/{trip}"' not in after and "Marked reviewed." in after

    # somewhere else than /wiki/: back to the drafts; a raw source is refused
    r = await client.post(
        "/wiki/review",
        data={"csrf": csrf, "id": "raw/chats/claude/trip-1", "next": "https://evil.example/"},
        headers={"Origin": ORIGIN},
        follow_redirects=True,
    )
    assert str(r.url).endswith("/wiki/drafts") and "Not marked reviewed:" in r.text


async def test_identity_merge_is_accepted_in_the_portal(
    client: httpx2.AsyncClient, runtime: Runtime
) -> None:
    store, chatgpt = runtime.store, Actor("chatgpt", Trust.AGENT)
    owner = {"type": "owner", "title": "Owner profile", "description": "The owner."}
    await store.write_concept("wiki/owner/profile", owner, "# Owner profile", "test", chatgpt)
    dup = "wiki/people/maarten"
    fm = {"type": "person", "title": "Maarten", "description": "The owner, again."}
    await store.write_concept(dup, fm, "# Maarten\n\nImported.", "import", chatgpt)
    merge = await store.propose_identity_merge(dup, "wiki/owner/profile", "Same person", chatgpt)

    listed = (await client.get("/wiki/proposals")).text
    assert "Same person or thing:" in listed and f'<a href="/wiki/page/{dup}">Maarten</a>' in listed
    assert listed.count("Accept merge</button>") == 1  # the other proposal is no merge
    assert f'name="id" value="{merge.proposal_id}"' in listed
    csrf = csrf_of(listed)

    preview = store.read(merge.proposal_id)[1].frontmatter["payload"]
    assert "Consolidated page preview" in listed and "Imported." in listed
    data = {"id": merge.proposal_id, "decision": "accept", "review_token": preview["review_token"]}
    no_csrf = await client.post("/wiki/proposals/decide", data=data, headers={"Origin": ORIGIN})
    assert no_csrf.status_code == 403
    r = await client.post(
        "/wiki/proposals/decide", data={**data, "csrf": csrf}, headers={"Origin": ORIGIN}
    )
    assert r.status_code == 303 and r.headers["location"] == "/wiki/proposals"
    _, doc, _ = store.read(dup)
    assert (doc.frontmatter or {})["canonical"] == "wiki/owner/profile"
    after = (await client.get("/wiki/proposals")).text
    assert "Merge accepted." in after and "Accept merge</button>" not in after
    page = (await client.get(f"/wiki/page/{dup}")).text
    assert "Merged duplicate" in page and 'href="/wiki/page/wiki/owner/profile"' in page

    r = await client.post(
        "/wiki/proposals/decide",
        data={**data, "csrf": csrf},
        headers={"Origin": ORIGIN},
        follow_redirects=True,
    )
    assert "Merge accepted." in r.text  # accepted already: safe receipt retry
    history = (await client.get("/wiki/merged")).text
    assert "Maarten" in history and "Content consolidated." in history
    assert "Imported." in store.read("wiki/owner/profile")[1].body
    assert "Imported." not in store.read(dup)[1].body


async def test_legacy_merge_review_and_stale_preview_in_portal(
    client: httpx2.AsyncClient, runtime: Runtime
) -> None:
    from pathlib import PurePosixPath

    from wiki_mcp.wiki.okf import serialize_document

    store, actor = runtime.store, Actor("chatgpt", Trust.AGENT)
    duplicate, canonical = "wiki/projects/ai-agentic-system", "wiki/projects/second-brain-vault"
    for pid, title, content in (
        (duplicate, "AI agentic system", "- 2026-09-01: Original project fact."),
        (canonical, "Second brain vault", "- 2026-09-02: Canonical project fact."),
    ):
        await store.write_concept(
            pid,
            {"type": "project", "title": title, "description": "One project."},
            f"# {title}\n\n## Facts\n\n{content}",
            "Seed",
            actor,
        )
    rel, doc, _ = store.read(duplicate)
    fm = {**doc.frontmatter, "canonical": canonical, "status": "deprecated"}
    store._write_file(rel, serialize_document(fm, doc.body))
    store.repo.commit([rel], "Old redirect-only merge", author_name="Test", author_email="t@x")
    store.reindex(rel)
    history = (await client.get("/wiki/merged")).text
    assert "AI agentic system" in history
    csrf = csrf_of(history)
    r = await client.post(
        "/wiki/merged/review",
        headers={"Origin": ORIGIN},
        data={"csrf": csrf, "duplicate": duplicate, "canonical": canonical},
    )
    assert r.status_code == 303
    [merge] = [p for p in store.list_proposals() if p.get("duplicate") == duplicate]
    preview = (await client.get("/wiki/proposals")).text
    assert "Original project fact." in preview and "Canonical project fact." in preview
    token = merge["review_token"]
    await store.append_to_concept(canonical, "Facts", "- A new decision.", "Changed", actor)
    accept = {
        "csrf": csrf_of(preview),
        "id": merge["proposal_id"],
        "decision": "accept",
        "review_token": token,
    }
    r = await client.post(
        "/wiki/proposals/decide", headers={"Origin": ORIGIN}, data=accept, follow_redirects=True
    )
    assert "page changed after the preview" in r.text
    assert "Original project fact." not in store.read(canonical)[1].body
    await client.post(
        "/wiki/proposals/decide", headers={"Origin": ORIGIN}, data={**accept, "decision": "refresh"}
    )
    renewed = next(p for p in store.list_proposals() if p.get("duplicate") == duplicate)
    assert renewed["review_token"] != token
    r = await client.post(
        "/wiki/proposals/decide", headers={"Origin": ORIGIN}, data=accept, follow_redirects=True
    )
    assert "preview changed" in r.text
    await client.post(
        "/wiki/proposals/decide",
        headers={"Origin": ORIGIN},
        data={**accept, "review_token": renewed["review_token"]},
    )
    assert "Original project fact." in store.read(canonical)[1].body
    assert "A new decision." in store.read(canonical)[1].body
    assert store.read(duplicate)[1].frontmatter["merge_consolidated"] is True
    assert store.read_redirected(duplicate, actor)[0] == PurePosixPath(canonical + ".md")
