"""The in-process reader (dream/local.py) sees exactly what the portal sees through the portal
API, and the dream material built from either is the same."""

from __future__ import annotations

from typing import Any

import httpx2

from tests.integration.test_portal_api import _chat, client, runtime  # noqa: F401 - fixtures
from tests.integration.test_portal_dreams import NEW, OLD, PAGE, SHED, SID, _git
from wiki_mcp.dream import deduction, hot_memory, merge
from wiki_mcp.dream.induction import inferences_block, prepare, text_of, windows
from wiki_mcp.dream.local import LocalReader
from wiki_mcp.dream.pages import reviewed_pages
from wiki_mcp.dream.reader import WikiReader
from wiki_mcp.runtime import Runtime


class HttpReader:
    """The reads of the portal's WikiClient (portal/wikiclient.py), without its caches."""

    def __init__(self, http: httpx2.AsyncClient) -> None:
        self.http = http

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        r = await self.http.get(path, params={k: v for k, v in params.items() if v is not None})
        if r.status_code == 404:
            raise LookupError(r.json()["error"])
        assert r.status_code == 200, r.text
        return r.json()  # type: ignore[no-any-return]

    async def docs(self, *, type: str | None = None) -> list[dict[str, Any]]:
        return list((await self._get("/docs", type=type)).get("docs", []))

    async def doc(self, ref: str) -> dict[str, Any]:
        return await self._get("/doc", id=ref)

    async def index(self) -> dict[str, Any]:
        return await self._get("/index")

    async def graph(self, max_age: float = 0) -> dict[str, Any]:
        return await self._get("/graph", include="raw,proposals")

    async def recent(self, limit: int = 50, *, since: str | None = None) -> list[dict[str, Any]]:
        return list((await self._get("/recent", limit=limit, since=since)).get("changes", []))

    async def renamed(self, ref: str) -> list[str]:
        return [str(i) for i in (await self._get("/renamed", id=ref)).get("to", [])]

    async def open_loops(self) -> list[dict[str, Any]]:
        return list((await self._get("/open-loops")).get("loops", []))


async def _wiki(client: httpx2.AsyncClient, runtime: Runtime) -> None:  # noqa: F811
    """Reviewed pages (one private, one with a link to a renamed page), a draft, a follow-up,
    a proposal and a rename."""
    await client.post("/import", json={"sources": [_chat()]})
    jane = f"- 2026-01: Planned with [Jane](/wiki/people/jane.md). [^{SID}]"
    pages = [
        {"path": PAGE, "type": "project", "title": "Garden shed", "description": "A shed.",
         "sections": [{"heading": "Timeline", "lines": [OLD, NEW, jane]},
                      {"heading": "Follow-ups", "lines": ["- [ ] Order the kit (as of 2026-01)"]}]},
        {"path": "wiki/projects/garden-shed-build", "type": "project", "title": "Garden shed build",
         "description": "Building the shed.", "sections": [
             {"heading": "Timeline", "lines": [f"- 2026-02: Bought wood. [^{SID}]"]}]},
        {"path": "wiki/people/jane", "type": "person", "title": "Jane", "description": "A friend.",
         "sections": []},
        {"path": "wiki/people/sam", "type": "person", "title": "Sam", "description": "A friend.",
         "private": True, "sections": [
             {"heading": "Relationship", "lines": [f"- 2026-02: Sam and I made up. [^{SID}]"]}]},
        {"path": "wiki/projects/draft", "type": "project", "title": "Draft", "description": "d",
         "sections": []},
    ]  # fmt: skip
    filed = {"sources": [SHED], "reason": "Filed", "pages": pages}
    assert (await client.post("/file", json=filed)).status_code == 200
    for pid in (PAGE, "wiki/projects/garden-shed-build", "wiki/people/sam", "wiki/people/jane"):
        assert (await client.post("/review", json={"id": pid})).status_code == 200
    _git(runtime, "mv", "wiki/people/jane.md", "wiki/people/jane-doe.md")
    _git(runtime, "-c", "user.name=o", "-c", "user.email=o@x", "commit", "-qm", "Rename Jane")
    await runtime.store.startup(None)
    goals = {"action": "goals", "add": ["Build a shed of their own"]}
    assert (await client.post("/owner", json=goals)).status_code == 200
    op = {"op": "append", "page": PAGE, "section": "Notes", "line": "- a note"}
    r = await client.post(
        "/dream/proposal",
        json={"dream": 1, "pass": "deduction", "summary": "Note", "reason": "Note",
              "operations": [op]},
    )  # fmt: skip
    assert r.status_code == 200, r.text


async def test_every_read_is_the_same(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    await _wiki(client, runtime)
    http, local = HttpReader(client), LocalReader(runtime.store)

    assert await local.index() == await http.index()
    assert await local.docs() == await http.docs()
    assert await local.docs(type="person") == await http.docs(type="person")
    for ref in (
        PAGE,
        "wiki/people/sam",
        "/wiki/owner/profile.md",
        "wiki/concepts/company-brain",
        SHED,
    ):
        assert await local.doc(ref) == await http.doc(ref), ref
    for missing in ("wiki/nope", "../etc/passwd"):
        for reader in (http, local):
            try:
                await reader.doc(missing)
            except LookupError:
                continue
            raise AssertionError(f"{missing} found")
    graph = await local.graph()
    assert graph == await http.graph() and any(n.get("private") for n in graph["nodes"])
    assert await local.recent(3) == await http.recent(3)
    assert await local.recent(500) == await http.recent(500)  # capped alike
    assert await local.recent(50, since="2020-01-01") == await http.recent(50, since="2020-01-01")
    assert await local.renamed("wiki/people/jane") == await http.renamed("wiki/people/jane")
    assert await local.renamed("wiki/people/jane") == ["wiki/people/jane-doe"]
    loops = await local.open_loops()
    assert loops == await http.open_loops() and loops


async def _material(wiki: WikiReader, since_base: str | None) -> dict[str, Any]:
    reviewed = await reviewed_pages(wiki)
    found, too_long = windows(reviewed)
    state = await prepare(wiki, reviewed, 1)
    block = inferences_block(state.existing, state.by_id)
    brief, known = await hot_memory.material(wiki)
    private, _ = await hot_memory.material(wiki, private=True)
    return {
        "reviewed": [(p.id, p.frontmatter, p.body) for p in reviewed],
        "deduction": (await deduction.material(wiki, since_base)).text,
        "hot": (brief, sorted(known), private),
        "induction": [text_of(w, block) for w in found] + [p.id for p in too_long],
        "known": sorted(state.known),
        "merge": [(a.id, b.id) for a, b in merge.candidates(reviewed)],
    }


async def test_the_material_is_the_same(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    base = (await HttpReader(client).recent(1))[0]["commit"]
    await _wiki(client, runtime)
    http, local = HttpReader(client), LocalReader(runtime.store)
    for since in (None, base):
        built = await _material(local, since)
        assert built == await _material(http, since), since
    assert built["merge"] and built["deduction"].count("<page ") >= 2
