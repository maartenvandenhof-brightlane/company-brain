"""An assistant dream's session through the portal API: opening it makes the hygiene fixes,
closing it writes the dream log, and reverting the dream undoes both."""

from __future__ import annotations

from typing import Any

import httpx2

from tests.integration.test_portal_api import _chat, client, runtime  # noqa: F401 - fixtures
from tests.integration.test_portal_dreams import OLD, PAGE, SHED, _git, _page
from wiki_mcp.dream.sessions import SESSION_TTL_S
from wiki_mcp.runtime import Runtime

DOUBLED = "- [ ] - [ ] Order the kit (as of 2026-01)"
STARTED = "2026-09-30T08:00:00Z"


async def _fixable(client: httpx2.AsyncClient) -> None:  # noqa: F811
    """A reviewed page with a doubled checkbox."""
    await client.post("/import", json={"sources": [_chat()]})
    page = {"path": PAGE, "type": "project", "title": "Garden shed", "description": "A shed.",
            "sections": [{"heading": "Timeline", "lines": [OLD]},
                         {"heading": "Follow-ups", "lines": [DOUBLED]}]}  # fmt: skip
    r = await client.post("/file", json={"sources": [SHED], "reason": "Filed", "pages": [page]})
    assert r.status_code == 200, r.text
    assert (await client.post("/review", json={"id": PAGE})).status_code == 200


def _open(dream: int = 5, **over: Any) -> dict[str, Any]:
    return {"dream": dream, "client": "claude.ai", "started": STARTED, "number": 1,
            "since_base": None, **over}  # fmt: skip


async def test_a_session_opens_with_hygiene_closes_with_the_log_and_reverts(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    await _fixable(client)
    before = (runtime.store.root / f"{PAGE}.md").read_text("utf-8")
    head = _git(runtime, "rev-parse", "HEAD").strip()

    r = await client.post("/dream/session", json=_open())
    assert r.status_code == 200, r.text
    state = r.json()
    assert state["status"] == "open" and state["client"] == "claude.ai"
    assert state["base"] == head[:12] and state["since_base"] is None
    assert state["passes_to_do"] == ["duplicates", "deduction", "induction", "hot-memory"]
    [commit] = state["commits"]
    assert commit["pass"] == "hygiene" and commit["pages"] == [PAGE]
    message = _git(runtime, "log", "-1", "--format=%an|%s|%B")
    assert message.startswith("process:dream|Dream 5: hygiene|")
    assert "\nDream: 5\nDream-pass: hygiene\nDream-client: claude.ai" in message
    assert "- [ ] Order the kit (as of 2026-01)" in _page(runtime)[1]
    assert DOUBLED not in _page(runtime)[1]

    got = await client.get("/dream/session", params={"dream": 5})
    assert got.status_code == 200 and got.json() == state

    r = await client.post("/dream/session/close", json={"dream": 5})
    assert r.status_code == 200, r.text
    closed = r.json()
    assert closed["status"] == "finished" and closed["ended"] == "closed by the owner"
    assert closed["log_page"] == "wiki/maintenance/dream-2026-09-30-1"
    assert [c["pass"] for c in closed["commits"]] == ["hygiene", "log"]
    message = _git(runtime, "log", "-1", "--format=%s|%B")
    assert message.startswith("Dream 5: log|")
    assert "\nDream: 5\nDream-pass: log\nDream-client: claude.ai" in message
    fm, body = _page(runtime, closed["log_page"])
    assert fm["type"] == "maintenance-report"
    assert "Trigger: assistant." in body and "Client: claude.ai." in body and "EUR" not in body
    assert "(ended: closed by the owner)" in body and "collapsed a doubled checkbox" in body
    assert "deduction: not done: the owner ended the session" in body
    assert (await client.get("/dream/session", params={"dream": 5})).json() == closed

    again = await client.post("/dream/session/close", json={"dream": 5})
    assert again.status_code == 400 and "closed already" in again.json()["error"]

    r = await client.post("/dream/revert", json={"dream": 5})
    assert r.status_code == 200, r.text
    assert len(r.json()["reverted"]) == 2 and r.json()["conflict"] is None
    assert (runtime.store.root / f"{PAGE}.md").read_text("utf-8") == before
    assert not (runtime.store.root / f"{closed['log_page']}.md").exists()


async def test_nothing_to_fix_and_an_expired_session(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    head = _git(runtime, "rev-parse", "HEAD")
    r = await client.post("/dream/session", json=_open(6, client="chatgpt", number=2))
    assert r.status_code == 200, r.text
    assert r.json()["commits"] == [] and _git(runtime, "rev-parse", "HEAD") == head
    [skipped] = r.json()["skipped"]
    assert skipped.startswith("hygiene: nothing to fix on ") and skipped.endswith(" reviewed pages")

    later = runtime.sessions.clock() + SESSION_TTL_S + 1
    runtime.sessions.clock = lambda: later
    state = (await client.get("/dream/session", params={"dream": 6})).json()
    assert state["status"] == "expired" and state["finished"] is None  # a read closes nothing
    assert _git(runtime, "rev-parse", "HEAD") == head

    closed = (await client.post("/dream/session/close", json={"dream": 6})).json()
    assert closed["status"] == "finished" and closed["ended"] == "expired"
    _, body = _page(runtime, "wiki/maintenance/dream-2026-09-30-2")
    assert "(ended: expired)" in body and "hot-memory: not done: the session expired" in body
    assert "Dream-client: chatgpt" in _git(runtime, "log", "-1", "--format=%B")


async def test_session_refusals(client: httpx2.AsyncClient) -> None:  # noqa: F811
    assert (await client.get("/dream/session", params={"dream": 9})).status_code == 404
    assert (await client.get("/dream/session", params={"dream": "x"})).status_code == 400
    r = await client.post("/dream/session/close", json={"dream": 9})
    assert r.status_code == 400 and "no session" in r.json()["error"]
    for bad in (_open(0), _open(client="a\nb"), _open(started="today"), _open(number="1")):
        assert (await client.post("/dream/session", json=bad)).status_code == 400, bad

    assert (await client.post("/dream/session", json=_open(7))).status_code == 200
    r = await client.post("/dream/session", json=_open(7))
    assert r.status_code == 400 and "had its session already" in r.json()["error"]
    r = await client.post("/dream/session", json=_open(8))
    assert r.status_code == 400 and "dream 7 is still open" in r.json()["error"]

    wrong = {"Authorization": "Bearer " + "l" * 40}
    for path in ("/dream/session", "/dream/session/close"):
        assert (await client.post(path, json=_open(8), headers=wrong)).status_code == 401
    assert (await client.put("/dream/session", json={})).status_code == 405
