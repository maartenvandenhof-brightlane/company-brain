"""Dreams and private pages: what a dream derives from a private page stays private, the
briefing comes in a variant for every client and a private one, and dream commits follow the
private logging rules."""

from __future__ import annotations

from typing import Any

import httpx2
import pytest

from tests.integration.test_portal_api import _chat, client, runtime  # noqa: F401 - fixtures
from tests.integration.test_portal_dreams import NEW, OLD, PAGE, SHED, SID, _git, _inference, _page
from wiki_mcp.config import Trust
from wiki_mcp.runtime import Runtime
from wiki_mcp.wiki.actor import PRIVATE_SCOPE, Actor
from wiki_mcp.wiki.okf import is_private
from wiki_mcp.wiki.store import WikiError

SAM = "wiki/people/sam"
OWNER_PAGE = "wiki/owner/profile"
SAM_A = f"- 2026-01: Sam and I fell out over the move. [^{SID}]"
SAM_B = f"- 2026-02: Sam and I made up. [^{SID}]"
OWNER = Actor(
    "portal",
    Trust.OWNER,
    frozenset({"wiki:read", "wiki:write", "wiki:admin", PRIVATE_SCOPE}),
    human="owner",
)
PUBLIC = Actor("claude.ai", Trust.AGENT, frozenset({"wiki:read", "wiki:write"}))


async def _filed(client: httpx2.AsyncClient) -> None:  # noqa: F811
    """A reviewed public project page and a reviewed private person page."""
    await client.post("/import", json={"sources": [_chat()]})
    pages = [
        {"path": PAGE, "type": "project", "title": "Garden shed", "description": "A shed.",
         "sections": [{"heading": "Timeline", "lines": [OLD, NEW]}]},
        {"path": SAM, "type": "person", "title": "Sam", "description": "A friend.",
         "private": True, "sections": [{"heading": "Relationship", "lines": [SAM_A, SAM_B]}]},
    ]  # fmt: skip
    r = await client.post("/file", json={"sources": [SHED], "reason": "Filed", "pages": pages})
    assert r.status_code == 200, r.text
    for pid in (PAGE, SAM):
        assert (await client.post("/review", json={"id": pid})).status_code == 200


async def _commit(client: httpx2.AsyncClient, ops: list[dict[str, Any]]) -> httpx2.Response:  # noqa: F811
    return await client.post(
        "/dream/commit", json={"dream": 1, "pass": "p", "reason": "Why", "operations": ops}
    )


async def _propose(client: httpx2.AsyncClient, ops: list[dict[str, Any]]) -> httpx2.Response:  # noqa: F811
    return await client.post(
        "/dream/proposal",
        json={"dream": 2, "pass": "p", "summary": "S", "reason": "Why", "operations": ops},
    )


async def test_the_briefing_for_every_client_names_no_private_page(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    await _filed(client)
    store = runtime.store
    public = {"op": "write_managed", "page": "wiki/briefing/hot-memory", "body": "- Sheds"}
    for leak in (f"- Sam ([Sam](/{SAM}.md))", f"- see {SAM}"):
        r = await _commit(client, [{**public, "body": leak}])
        assert r.status_code == 400 and "names a private page" in r.json()["error"], leak
    full = {"op": "write_managed", "page": "wiki/briefing/hot-memory-private",
            "body": f"- Sheds\n- Made up with [Sam](/{SAM}.md)"}  # fmt: skip
    assert (await _commit(client, [public, full])).status_code == 200
    fm, _ = _page(runtime, "wiki/briefing/hot-memory-private")
    assert fm["private"] is True and not is_private(_page(runtime, "wiki/briefing/hot-memory")[0])
    assert store.briefing()["markdown"].strip() == "- Sheds"
    mine = store.briefing(private=True)
    assert "Sam" in mine["markdown"] and mine["page"] == "wiki/briefing/hot-memory-private"
    # the commit touched a private page: neutral log line, trailer, hidden from recent changes
    assert "Private: yes" in _git(runtime, "log", "-1", "--format=%B")
    log = store.read_text_file("log.md")
    assert "a private page" in log and "hot-memory-private" not in log
    head = _git(runtime, "rev-parse", "HEAD")[:12]
    assert head not in [c.sha[:12] for c in store.recent_changes(private=False)]
    recent = (await client.get("/recent", params={"limit": 1})).json()["changes"][0]
    assert recent["private"] is True


async def test_a_dream_log_naming_a_private_page_is_private(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    await _filed(client)
    log = {"op": "write_managed", "page": "wiki/maintenance/dream-2026-09-29-1",
           "body": f"# Dream\n\n- {SAM}: orphan"}  # fmt: skip
    assert (await _commit(client, [log])).status_code == 200
    assert _page(runtime, "wiki/maintenance/dream-2026-09-29-1")[0]["private"] is True
    plain = {"op": "write_managed", "page": "wiki/maintenance/dream-2026-09-29-2",
             "body": "# Dream\n\n- nothing", "frontmatter": {"private": True}}  # fmt: skip
    assert (await _commit(client, [plain])).status_code == 200
    assert _page(runtime, "wiki/maintenance/dream-2026-09-29-2")[0]["private"] is True
    other = {**plain, "page": "wiki/maintenance/dream-2026-09-29-3", "frontmatter": {}}
    assert (await _commit(client, [other])).status_code == 200
    assert not is_private(_page(runtime, "wiki/maintenance/dream-2026-09-29-3")[0])


async def test_an_inference_resting_on_a_private_page_is_private(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    await _filed(client)
    store = runtime.store
    op = _inference(supports=[
        {"page": SAM, "quote": SAM_A, "source": SHED},
        {"page": SAM, "quote": SAM_B, "source": SHED},
    ], about=[SAM])  # fmt: skip
    r = await _propose(client, [op])
    assert r.status_code == 200, r.text
    pid = r.json()["proposal_id"]
    assert [p["private"] for p in store.list_proposals()] == [True]
    assert store.list_proposals(private=False) == []
    with pytest.raises(WikiError, match="no page"):
        await store.accept_proposal(
            pid, "Checked", Actor("x", Trust.OWNER, frozenset({"wiki:admin"}))
        )
    await store.accept_proposal(pid, "Checked", OWNER)
    fm, _ = _page(runtime, "wiki/inferred/prefers-kits")
    assert fm["private"] is True
    assert "Private: yes" in _git(runtime, "log", "-1", "--format=%B")
    # about a private page, resting on public lines: private too
    about = _inference(about=[SAM])
    about["page"] = "wiki/inferred/about-sam"
    pid = (await _propose(client, [about])).json()["proposal_id"]
    await store.accept_proposal(pid, "Checked", OWNER)
    assert _page(runtime, "wiki/inferred/about-sam")[0]["private"] is True


async def test_an_inference_becomes_private_with_its_support_page(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    await _filed(client)
    store = runtime.store
    op = _inference(about=[PAGE])
    pid = (await _propose(client, [op])).json()["proposal_id"]
    await store.accept_proposal(pid, "Checked", OWNER)
    assert not is_private(_page(runtime, "wiki/inferred/prefers-kits")[0])
    await store.set_private(PAGE, True, "personal after all", OWNER)
    assert _page(runtime, "wiki/inferred/prefers-kits")[0]["private"] is True
    files = _git(runtime, "show", "--name-only", "--format=", "HEAD").split()
    assert f"{PAGE}.md" in files and "wiki/inferred/prefers-kits.md" in files


async def test_the_briefing_for_every_client_loses_a_page_marked_private(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    await _filed(client)
    store = runtime.store
    body = f"- Building a shed ([Garden shed](/{PAGE}.md))\n- Owner stuff ([Me](/{OWNER_PAGE}.md))"
    op = {"op": "write_managed", "page": "wiki/briefing/hot-memory", "body": body}
    assert (await _commit(client, [op])).status_code == 200
    await store.set_private(PAGE, True, "personal after all", OWNER)
    shown = store.briefing()["markdown"]
    assert "shed" not in shown and "Owner stuff" in shown
    assert not is_private(_page(runtime, "wiki/briefing/hot-memory")[0])
    files = _git(runtime, "show", "--name-only", "--format=", "HEAD").split()
    assert "wiki/briefing/hot-memory.md" in files
    hits = store.index.search("shed", private=False)
    assert "wiki/briefing/hot-memory" not in [h.row.id for h in hits]


@pytest.mark.parametrize(
    ("ops", "why"),
    [
        # a merge of a private page into a public one
        ([{"op": "append", "page": PAGE, "section": "Timeline", "line": SAM_A},
          {"op": "deprecate", "page": SAM, "note": f"Merged into [Garden shed](/{PAGE}.md)."}],
         "private and non-private pages together"),
        ([{"op": "add_related", "page": PAGE, "ids": [SAM]}], "cannot relate to a private"),
        ([{"op": "append", "page": PAGE, "section": "Notes", "line": f"- see [Sam](/{SAM}.md)"}],
         "cannot link to a private"),
    ],
)  # fmt: skip
async def test_nothing_private_reaches_a_non_private_page(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
    ops: list[dict[str, Any]],
    why: str,
) -> None:
    await _filed(client)
    head = _git(runtime, "rev-parse", "HEAD")
    r = await _propose(client, ops)
    assert r.status_code == 400 and why in r.json()["error"], r.text
    assert _git(runtime, "rev-parse", "HEAD") == head


async def test_private_pages_may_relate_and_a_hygiene_link_fix_may_not_leak(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    await _filed(client)
    ok = await _propose(client, [{"op": "add_related", "page": SAM, "ids": [PAGE]}])
    assert ok.status_code == 200, ok.text
    assert runtime.store.list_proposals()[0]["private"] is True
    link = {"op": "replace", "page": PAGE, "old": "# Garden shed",
            "new": f"# Garden shed\n\nSee [Sam](/{SAM}.md)."}  # fmt: skip
    r = await _commit(client, [link])
    assert r.status_code == 400 and "cannot link to a private" in r.json()["error"]


async def test_open_loops_and_recent_changes_carry_private(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    await _filed(client)
    await runtime.store.append_to_concept(SAM, "Follow-ups", "- [ ] Call Sam", "a loop", OWNER)
    [loop] = (await client.get("/open-loops")).json()["loops"]
    assert loop["page"] == SAM and loop["private"] is True
    changes = (await client.get("/recent", params={"limit": 50})).json()["changes"]
    assert changes[0]["private"] is True and any(not c["private"] for c in changes)
    with pytest.raises(WikiError, match="no page"):
        await runtime.store.append_to_concept(SAM, "Notes", "- x", "not mine", PUBLIC)


async def test_an_app_without_private_access_marks_a_page_private_with_every_side_effect(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    await _filed(client)
    store = runtime.store
    pid = (await _propose(client, [_inference(about=[PAGE])])).json()["proposal_id"]
    await store.accept_proposal(pid, "Checked", OWNER)
    body = f"- Building a shed ([Garden shed](/{PAGE}.md))\n- Owner stuff ([Me](/{OWNER_PAGE}.md))"
    op = {"op": "write_managed", "page": "wiki/briefing/hot-memory", "body": body}
    assert (await _commit(client, [op])).status_code == 200
    await store.set_private(PAGE, True, "personal after all", PUBLIC)
    assert _page(runtime, PAGE)[0]["private"] is True
    assert _page(runtime, "wiki/inferred/prefers-kits")[0]["private"] is True
    assert "shed" not in store.briefing()["markdown"]
    files = _git(runtime, "show", "--name-only", "--format=", "HEAD").split()
    assert {f"{PAGE}.md", "wiki/inferred/prefers-kits.md", "wiki/briefing/hot-memory.md"} <= set(
        files
    )
    assert "Private: yes" in _git(runtime, "log", "-1", "--format=%B")
    # and now the page is gone for that app: marking it again is answered like a missing page
    with pytest.raises(WikiError, match="no page at"):
        await store.set_private(PAGE, True, "again", PUBLIC)
