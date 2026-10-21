"""The portal API's owner writes (review, goals, statements), open loops, and dreams: direct
commits, proposals with a base commit, and reverting a dream."""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx2
import pytest

from tests.integration.test_portal_api import _chat, client, runtime  # noqa: F401 - fixtures
from wiki_mcp.config import Trust
from wiki_mcp.runtime import Runtime
from wiki_mcp.wiki.actor import Actor
from wiki_mcp.wiki.okf import parse_document
from wiki_mcp.wiki.store import WikiError

SHED = "raw/chats/claude/0b6c8d7e-1111"
SID = "0b6c8d7e-1111"
PAGE = "wiki/projects/garden-shed"
OLD = f"- 2026-01: The shed will be a kit. [^{SID}]"
NEW = f"- 2026-03: The shed will be built from scratch. [^{SID}]"
LOOP = "- [ ] Order the kit (as of 2026-01)"
TODAY = dt.datetime.now(dt.UTC).date().isoformat()
OWNER = Actor(
    "portal", Trust.OWNER, frozenset({"wiki:read", "wiki:write", "wiki:admin"}), human="owner"
)


async def _filed(client: httpx2.AsyncClient, *, review: bool = True) -> None:  # noqa: F811
    """A chat filed as a draft page with two stated lines and a follow-up; reviewed."""
    await client.post("/import", json={"sources": [_chat()]})
    page = {
        "path": PAGE,
        "type": "project",
        "title": "Garden shed",
        "description": "A shed.",
        "sections": [
            {"heading": "Timeline", "lines": [OLD, NEW]},
            {"heading": "Follow-ups", "lines": [LOOP]},
        ],
    }
    r = await client.post("/file", json={"sources": [SHED], "reason": "Filed", "pages": [page]})
    assert r.status_code == 200, r.text
    if review:
        assert (await client.post("/review", json={"id": PAGE})).status_code == 200


def _page(runtime: Runtime, pid: str = PAGE) -> tuple[dict[str, Any], str]:  # noqa: F811
    doc = parse_document((runtime.store.root / f"{pid}.md").read_text(encoding="utf-8"))
    return doc.frontmatter or {}, doc.body


def _git(runtime: Runtime, *args: str) -> str:  # noqa: F811
    return runtime.store.repo._run(*args).stdout


# ------------------------------------------------------------------ review and loops


async def test_review_and_open_loops(client: httpx2.AsyncClient, runtime: Runtime) -> None:  # noqa: F811
    await _filed(client, review=False)
    fm, _ = _page(runtime)
    assert fm["status"] == "draft" and "from-chat-import" in fm["tags"]
    assert (await client.get("/open-loops")).json()["loops"] == []  # drafts are not read

    r = await client.post("/review", json={"id": PAGE})
    assert r.status_code == 200 and r.json()["status"] == "committed"
    fm, _ = _page(runtime)
    assert fm["status"] == "stable" and "from-chat-import" not in fm["tags"]
    assert fm["verified"][-1]["by"] == "human:owner"
    assert "Review wiki/projects/garden-shed" in _git(runtime, "log", "-1", "--format=%s")
    again = await client.post("/review", json={"id": PAGE})
    assert again.json()["status"] == "unchanged"

    [loop] = (await client.get("/open-loops")).json()["loops"]
    assert loop == {
        "page": PAGE,
        "title": "Garden shed",
        "status": "stable",
        "reviewed": True,
        "private": False,
        "line": LOOP,
        "date": "2026-01",
    }
    for bad in ({"id": SHED}, {"id": "AGENTS"}, {"id": "wiki/nope"}, {}):
        assert (await client.post("/review", json=bad)).status_code == 400


# ------------------------------------------------------------------ the owner


async def test_goals_are_added_and_dropped_never_deleted(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    r = await client.post("/owner", json={"action": "goals", "add": ["Ship the shed", "Run"]})
    assert r.status_code == 200, r.text
    fm, body = _page(runtime, "wiki/owner/profile")
    goal = f"- Ship the shed (set {TODAY})"
    assert goal in body and f"- Run (set {TODAY})" in body
    commit = _git(runtime, "log", "-1", "--format=%an %B")
    assert commit.startswith("portal ") and "Trust: owner" in commit

    r = await client.post("/owner", json={"action": "goals", "drop": [goal]})
    assert r.status_code == 200, r.text
    _, body = _page(runtime, "wiki/owner/profile")
    assert f"{goal} *(superseded {TODAY}: dropped by owner)*" in body
    for bad in (
        {"action": "goals", "drop": [goal]},  # not active any more
        {"action": "goals", "drop": ["- never set"]},
        {"action": "goals", "add": ["x"]},  # too short
        {"action": "rename"},
    ):
        assert (await client.post("/owner", json=bad)).status_code == 400, bad
    # the same goal again is not added twice
    before = _git(runtime, "rev-parse", "HEAD")
    await client.post("/owner", json={"action": "goals", "add": ["Run"]})
    assert _git(runtime, "rev-parse", "HEAD") == before


async def test_owner_statement_is_a_cited_owner_source(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    r = await client.post(
        "/owner",
        json={"action": "statement", "text": "I prefer mornings for deep work.", "section": ""},
    )
    assert r.status_code == 200, r.text
    fm, body = _page(runtime, "wiki/owner/profile")
    [source] = [s for s in fm["sources"] if "owner-statement" in s["resource"]]
    assert f"- {TODAY}: I prefer mornings for deep work. [^{source['id']}]" in body
    assert "## Facts" in body
    raw = parse_document((runtime.store.root / source["resource"][1:]).read_text("utf-8"))
    assert raw.frontmatter and raw.frontmatter["trust"] == "owner"
    assert raw.frontmatter["type"] == "source"
    # one commit for both
    files = _git(runtime, "show", "--name-only", "--format=", "HEAD").split()
    assert "wiki/owner/profile.md" in files and source["resource"][1:] in files
    with pytest.raises(WikiError, match="only the owner"):
        await runtime.store.owner_statement("x y z", "Facts", Actor("claude.ai", Trust.AGENT))


# ------------------------------------------------------------------ dream commits


async def test_dream_commit_applies_hygiene_in_one_commit(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    await _filed(client)
    fm, body = _page(runtime)
    ops = [
        {"op": "replace", "page": PAGE, "old": "# Garden shed", "new": "# Garden shed (kit)"},
        {"op": "write_managed", "page": "wiki/briefing/hot-memory", "body": "# Hot memory\n\n- x",
         "frontmatter": {"title": "Hot memory", "description": "Briefing."}},
    ]  # fmt: skip
    r = await client.post(
        "/dream/commit", json={"dream": 3, "pass": "hygiene", "reason": "Fixes", "operations": ops}
    )
    assert r.status_code == 200, r.text
    assert sorted(r.json()["pages"]) == ["wiki/briefing/hot-memory", PAGE]
    message = _git(runtime, "log", "-1", "--format=%an|%s|%B")
    assert message.startswith("process:dream|Dream 3: hygiene|")
    assert "\nDream: 3\n" in message and "Dream-pass: hygiene" in message
    brief, _ = _page(runtime, "wiki/briefing/hot-memory")
    assert brief["type"] == "briefing" and brief["dream"] == 3


@pytest.mark.parametrize(
    ("ops", "why"),
    [
        ([{"op": "supersede", "page": PAGE, "line": OLD, "date": TODAY, "reason": "x"}],
         "not allowed here"),  # proposals only
        ([{"op": "replace", "page": PAGE, "old": "will be a kit", "new": "was a kit"}],
         "bullet line removed or reworded"),
        ([{"op": "replace", "page": "raw/chats/claude/0b6c8d7e-1111", "old": "a", "new": "b"}],
         "only change pages under wiki/"),
        ([{"op": "replace", "page": "wiki/projects/draft", "old": "# Draft", "new": "# D"}],
         "not reviewed"),
        ([{"op": "write_managed", "page": "wiki/briefing/hot-memory",
           "body": "- [x](/wiki/inferred/y.md)"}], "never links to inferences"),
        ([{"op": "write_managed", "page": PAGE, "body": "x"}], "only the briefing"),
        ([{"op": "add_sources", "page": PAGE, "ids": [PAGE]}],
         "is not a raw source"),
    ],
)  # fmt: skip
async def test_dream_commit_refusals(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
    ops: list[dict[str, Any]],
    why: str,
) -> None:
    await _filed(client)
    draft = {"path": "wiki/projects/draft", "type": "project", "title": "Draft",
             "description": "d", "sections": []}  # fmt: skip
    await client.post("/file", json={"sources": [SHED], "reason": "Filed", "pages": [draft]})
    head = _git(runtime, "rev-parse", "HEAD")
    r = await client.post(
        "/dream/commit", json={"dream": 1, "pass": "hygiene", "reason": "Fixes", "operations": ops}
    )
    assert r.status_code == 400 and why in r.json()["error"], r.text
    assert _git(runtime, "rev-parse", "HEAD") == head and not runtime.store.repo.is_dirty()


# ------------------------------------------------------------------ dream proposals


async def test_a_supersede_proposal_keeps_the_old_line(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    await _filed(client)
    op = {"op": "supersede", "page": PAGE, "line": OLD, "date": "2026-03-01",
          "reason": "built from scratch instead"}  # fmt: skip
    r = await client.post(
        "/dream/proposal",
        json={"dream": 4, "pass": "deduction", "summary": "The shed plan changed",
              "reason": "A newer line replaces it", "operations": [op]},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    pid = r.json()["proposal_id"]
    [listed] = (await client.get("/proposals")).json()["proposals"]
    assert listed["dream"] == 4 and listed["pass"] == "deduction" and not listed["speculative"]
    assert _page(runtime)[1].count(OLD) == 1  # nothing changed yet

    await runtime.store.accept_proposal(pid, "Checked", OWNER)
    _, body = _page(runtime)
    assert f"{OLD} *(superseded 2026-03-01: built from scratch instead)*" in body
    assert NEW in body
    assert "From-dream: 4" in _git(runtime, "log", "-1", "--format=%B")


async def test_accepting_after_the_page_changed_is_refused(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    await _filed(client)
    op = {"op": "append", "page": PAGE, "section": "Follow-ups",
          "line": "- [ ] Confirm kit or scratch (as of 2026-03)"}  # fmt: skip
    r = await client.post(
        "/dream/proposal",
        json={"dream": 5, "pass": "deduction", "summary": "Conflict", "reason": "Two plans",
              "operations": [op]},
    )  # fmt: skip
    pid = r.json()["proposal_id"]
    await runtime.store.append_to_concept(
        PAGE, "Notes", "Bought wood.", "owner note", Actor("claude.ai", Trust.AGENT)
    )
    with pytest.raises(WikiError, match="page changed since the dream; run it again"):
        await runtime.store.accept_proposal(pid, "Checked", OWNER)


def _inference(**over: Any) -> dict[str, Any]:
    fm = {
        "title": "Prefers kits",
        "description": "The owner tends to prefer ready-made kits.",
        "kind": "induction",
        "confidence": "low",
        "speculative": False,
        "about": ["wiki/owner/profile"],
        "supports": [
            {"page": PAGE, "quote": OLD, "source": SHED},
            {"page": PAGE, "quote": NEW, "source": SHED},
        ],
        "stale_after": "2027-01-01T00:00:00Z",
    }
    fm.update(over)
    body = over.pop("body", "# Prefers kits\n\n**Inferred, not stated.** Confidence: low.\n")
    return {"op": "write_inference", "page": "wiki/inferred/prefers-kits",
            "frontmatter": fm, "body": body}  # fmt: skip


async def test_inference_proposals(client: httpx2.AsyncClient, runtime: Runtime) -> None:  # noqa: F811
    await _filed(client)

    async def propose(op: dict[str, Any], speculative: bool = False) -> httpx2.Response:
        return await client.post(
            "/dream/proposal",
            json={"dream": 6, "pass": "induction", "summary": "A pattern", "reason": "Recurs",
                  "operations": [op], "speculative": speculative},
        )  # fmt: skip

    r = await propose(_inference())
    assert r.status_code == 200, r.text
    await runtime.store.accept_proposal(r.json()["proposal_id"], "Fine", OWNER)
    fm, body = _page(runtime, "wiki/inferred/prefers-kits")
    assert fm["type"] == "inference" and fm["dream"] == 6 and fm["confidence"] == "low"
    assert fm["supports"][0] == {"page": PAGE, "quote": OLD, "source": SHED}

    bad = {
        "high": _inference(confidence="high"),  # two supports from one source: at most low
        "chain": _inference(supports=[
            {"page": PAGE, "quote": OLD, "source": SHED},
            {"page": "wiki/inferred/prefers-kits", "quote": "x", "source": SHED}]),
        "invented": _inference(supports=[
            {"page": PAGE, "quote": OLD, "source": SHED},
            {"page": PAGE, "quote": "- 2026-02: made up [^x]", "source": SHED}]),
        "one": _inference(supports=[{"page": PAGE, "quote": OLD, "source": SHED}]),
        "unmarked": _inference(speculative=True),  # no speculative banner
    }  # fmt: skip
    for name, op in bad.items():
        op["page"] = f"wiki/inferred/bad-{name}"
        assert (await propose(op)).status_code == 400, name
    spec = _inference(
        speculative=True,
        body="# Kits\n\n**Speculative, inferred, not stated.** Speculative: he dislikes tools.\n",
    )
    spec["page"] = "wiki/inferred/dislikes-tools"
    r = await propose(spec, speculative=True)
    assert r.status_code == 200, r.text
    listed = [p for p in (await client.get("/proposals")).json()["proposals"] if p["speculative"]]
    assert len(listed) == 1
    text = (runtime.store.root / f"{r.json()['proposal_id']}.md").read_text("utf-8")
    assert "**Speculative, inferred, not stated.**" in text


async def test_retire_deprecates_and_keeps_the_body(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    await _filed(client)
    r = await client.post(
        "/dream/proposal",
        json={"dream": 6, "pass": "induction", "summary": "A pattern", "reason": "Recurs",
              "operations": [_inference()]},
    )  # fmt: skip
    await runtime.store.accept_proposal(r.json()["proposal_id"], "Fine", OWNER)
    _, before = _page(runtime, "wiki/inferred/prefers-kits")
    retire = {"op": "deprecate", "page": "wiki/inferred/prefers-kits",
              "note": "Retired by dream 7: no longer holds."}  # fmt: skip
    r = await client.post(
        "/dream/proposal",
        json={"dream": 7, "pass": "induction", "summary": "Retire", "reason": "Superseded",
              "operations": [retire]},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    await runtime.store.accept_proposal(r.json()["proposal_id"], "Fine", OWNER)
    fm, after = _page(runtime, "wiki/inferred/prefers-kits")
    assert fm["status"] == "deprecated"
    assert after.startswith("# Prefers kits\n\nRetired by dream 7: no longer holds.")
    assert "**Inferred, not stated.** Confidence: low." in after and len(after) > len(before)


# ------------------------------------------------------------------ revert


async def test_revert_restores_the_tree_and_rejects_proposals(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    await _filed(client)
    tree = _git(runtime, "rev-parse", "HEAD").strip()
    before = (runtime.store.root / f"{PAGE}.md").read_text("utf-8")
    brief = {"op": "write_managed", "page": "wiki/briefing/hot-memory", "body": "# Hot memory"}
    r = await client.post(
        "/dream/commit",
        json={"dream": 8, "pass": "hot-memory", "reason": "Briefing", "operations": [brief]},
    )  # a page the dream creates: reverting deletes it
    assert r.status_code == 200, r.text
    for n, (old, new) in enumerate([("# Garden shed", "# Shed"), ("A shed.", "A garden shed.")]):
        r = await client.post(
            "/dream/commit",
            json={"dream": 8, "pass": f"hygiene-{n}", "reason": "Fixes",
                  "operations": [{"op": "replace", "page": PAGE, "old": old, "new": new}]},
        )  # fmt: skip
        assert r.status_code == 200, r.text
    op = {"op": "append", "page": PAGE, "section": "Notes", "line": "- a note"}
    await client.post(
        "/dream/proposal",
        json={"dream": 8, "pass": "deduction", "summary": "Note", "reason": "Note",
              "operations": [op]},
    )  # fmt: skip

    r = await client.post("/dream/revert", json={"dream": 8})
    assert r.status_code == 200, r.text
    result = r.json()
    assert len(result["reverted"]) == 3 and len(result["rejected"]) == 1
    assert not (runtime.store.root / "wiki/briefing/hot-memory.md").exists()
    assert result["conflict"] is None
    page = (runtime.store.root / f"{PAGE}.md").read_text("utf-8")
    assert page == before
    assert _git(runtime, "log", "-4", "--format=%s").splitlines()[1:] == [
        "Revert dream 8: Dream 8: hot-memory",
        "Revert dream 8: Dream 8: hygiene-0",
        "Revert dream 8: Dream 8: hygiene-1",
    ]
    assert (await client.get("/proposals")).json()["proposals"] == []
    # the wiki pages are as before the dream (index.md and log.md record the history)
    diff = _git(runtime, "diff", "--name-only", tree, "HEAD").split()
    assert [p for p in diff if p not in ("index.md", "log.md")] == []
    # reverting again changes nothing
    again = (await client.post("/dream/revert", json={"dream": 8})).json()
    assert again["reverted"] == [] and again["conflict"] is None


async def test_a_conflicting_revert_stops_cleanly(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    await _filed(client)
    r = await client.post(
        "/dream/commit",
        json={"dream": 9, "pass": "hygiene", "reason": "Fixes",
              "operations": [{"op": "replace", "page": PAGE, "old": "A shed.", "new": "A hut."}]},
    )  # fmt: skip
    dream_sha = r.json()["commit"]
    # the owner edits the same line afterwards
    path = runtime.store.root / f"{PAGE}.md"
    path.write_text(path.read_text("utf-8").replace("A hut.", "A big hut."), "utf-8")
    await runtime.store.startup(None)  # records the edit as an out-of-band commit
    head = _git(runtime, "rev-parse", "HEAD")

    result = (await client.post("/dream/revert", json={"dream": 9})).json()
    assert result["reverted"] == [] and result["rejected"] == []
    assert result["conflict"]["commit"] == dream_sha and result["conflict"]["pages"] == [
        f"{PAGE}.md"
    ]
    assert _git(runtime, "rev-parse", "HEAD") == head and not runtime.store.repo.is_dirty()
    assert "A big hut." in path.read_text("utf-8")


async def test_dream_writes_need_the_portal_token(client: httpx2.AsyncClient) -> None:  # noqa: F811
    wrong = {"Authorization": "Bearer " + "l" * 40}
    for path in ("/review", "/owner", "/dream/commit", "/dream/proposal", "/dream/revert"):
        assert (await client.post(path, json={}, headers=wrong)).status_code == 401
        assert (await client.put(path, json={})).status_code == 405
        assert (await client.post(path, json={})).status_code == 400


async def test_hygiene_may_make_three_exact_rewrites(
    client: httpx2.AsyncClient,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    await client.post("/import", json={"sources": [_chat()]})
    doubled = "- [ ] - [ ] Measure the garden (as of 2026-01)"
    jane = f"- 2026-01: Planned with [Jane](/wiki/people/jane.md). [^{SID}]"
    page = {"path": PAGE, "type": "project", "title": "Garden shed", "description": "A shed.",
            "sections": [{"heading": "Timeline", "lines": [OLD, jane]},
                         {"heading": "Follow-ups", "lines": [doubled]}]}  # fmt: skip
    person = {"path": "wiki/people/jane", "type": "person", "title": "Jane",
              "description": "A friend.", "sections": []}  # fmt: skip
    filed = {"sources": [SHED], "reason": "Filed", "pages": [page, person]}
    assert (await client.post("/file", json=filed)).status_code == 200
    await client.post("/review", json={"id": PAGE})
    # Jane's page is renamed outside the server (git mv), so the link is broken.
    _git(runtime, "mv", "wiki/people/jane.md", "wiki/people/jane-doe.md")
    _git(runtime, "-c", "user.name=o", "-c", "user.email=o@x", "commit", "-qm", "Rename Jane")
    renamed = (await client.get("/renamed", params={"id": "wiki/people/jane"})).json()
    assert renamed == {"id": "wiki/people/jane", "to": ["wiki/people/jane-doe"]}
    assert (await client.get("/renamed", params={"id": "../x"})).status_code == 400
    await runtime.store.owner_goals(["Build something of their own"], [], OWNER)
    goal = f"- Build something of their own (set {TODAY})"

    async def commit(ops: list[dict[str, Any]]) -> httpx2.Response:
        return await client.post(
            "/dream/commit",
            json={"dream": 2, "pass": "hygiene", "reason": "Fixes", "operations": ops},
        )

    moved = jane.replace("/wiki/people/jane.md", "/wiki/people/jane-doe.md")
    ok = await commit(
        [
            {"op": "replace", "page": PAGE, "old": doubled, "new": "- [ ] Measure the garden (as of 2026-01)"},
            {"op": "replace", "page": PAGE, "old": jane, "new": moved},
            {"op": "replace", "page": "wiki/owner/profile", "old": goal,
             "new": goal.replace("their own", "his own")},
        ]
    )  # fmt: skip
    assert ok.status_code == 200, ok.text
    _, body = _page(runtime)
    assert "- [ ] Measure the garden (as of 2026-01)" in body and moved in body
    assert "Build something of his own" in _page(runtime, "wiki/owner/profile")[1]

    refused = [
        # the pronoun fix is for the owner profile only
        {"op": "replace", "page": PAGE, "old": "- [ ] Measure", "new": "- [ ] Measure their"},
        # a link sent to a page that was never renamed from it
        {"op": "replace", "page": PAGE, "old": moved,
         "new": moved.replace("jane-doe.md", "shed.md")},
        # a reword dressed as a checkbox fix
        {"op": "replace", "page": PAGE, "old": "- [ ] Measure the garden",
         "new": "- [ ] Measure the lawn"},
    ]  # fmt: skip
    for op in refused:
        r = await commit([op])
        assert r.status_code == 400 and "reworded" in r.json()["error"], op
