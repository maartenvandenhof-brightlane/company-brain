"""Private pages: the `private: true` marker, the wiki:private scope and every path that could
show a private page to a client without it."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from tests.conftest import person
from wiki_mcp.audit import AuditLog
from wiki_mcp.config import Trust
from wiki_mcp.mcp.tools import ToolDeps, register
from wiki_mcp.wiki.actor import Actor
from wiki_mcp.wiki.graph import build_graph
from wiki_mcp.wiki.index import SearchIndex
from wiki_mcp.wiki.maintenance import run_lint
from wiki_mcp.wiki.okf import parse_document
from wiki_mcp.wiki.store import ChatSource, FilingPage, FilingSection, WikiError, WikiStore
from wiki_mcp.wiki.types import TypeRegistry

FRIEND = "wiki/people/best-friend"
MOVE = "wiki/life/move-to-ghent"
COLLEAGUE = "wiki/people/colleague"
HOBBY = "wiki/concepts/hobby"
SECRET_REASON = "Friend shared zebrafish worries"


@pytest.fixture
def me() -> Actor:
    return Actor("claude.ai", Trust.AGENT, frozenset({"wiki:read", "wiki:write", "wiki:private"}))


@pytest.fixture
def guest() -> Actor:
    """No wiki:private, but everything else (admin included)."""
    return Actor("librechat", Trust.AGENT, frozenset({"wiki:read", "wiki:write", "wiki:admin"}))


class Tools:
    def __init__(self, store: WikiStore) -> None:
        self.actor: Actor | None = None
        self.server: MCPServer[Any] = MCPServer("test")
        register(self.server, ToolDeps(store, AuditLog(None)), get_actor=lambda: self.actor)  # type: ignore[arg-type,return-value]

    async def ok(self, actor: Actor, name: str, **args: Any) -> dict[str, Any]:
        self.actor = actor
        result = await self.server.call_tool(name, args)
        assert not result.is_error, result.content  # type: ignore[union-attr]
        return result.structured_content  # type: ignore[union-attr,return-value]

    async def error(self, actor: Actor, name: str, **args: Any) -> str:
        self.actor = actor
        with pytest.raises(ToolError) as exc:
            await self.server.call_tool(name, args)
        return str(exc.value).removeprefix(f"Error executing tool {name}: ")


@pytest.fixture
def tools(store: WikiStore) -> Tools:
    return Tools(store)


async def _seed(store: WikiStore, me: Actor) -> None:
    await store.write_concept(COLLEAGUE, person("Colleague"), "Works with me.", "test", me)
    await store.write_concept(
        FRIEND,
        person("Best friend (unnamed)", private=True, related=[COLLEAGUE]),
        "## Feelings\n\n- 2026-09: The owner worries about the zebrafish plan.",
        SECRET_REASON,
        me,
    )
    await store.write_concept(
        HOBBY,
        {
            "type": "concept",
            "title": "Hobby",
            "description": "A hobby.",
            "related": [FRIEND, COLLEAGUE],
        },
        "Fishing.",
        "test",
        me,
    )
    await store.write_concept(
        MOVE,
        {"type": "life-event", "title": "Move to Ghent", "description": "Moving house."},
        "Moved in 2026 with the best friend's help.",
        "test",
        me,
    )


def _git(store: WikiStore, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", f"safe.directory={store.root}", "-C", str(store.root), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


# ------------------------------------------------------------------ the marker


def test_type_registry_reads_private_defaults(registry: TypeRegistry) -> None:
    life = registry.get("life-event")
    assert life is not None and life.private and str(life.folder) == "wiki/life"
    assert not registry.get("person").private  # type: ignore[union-attr]


async def test_type_default_and_sticky_marker(store: WikiStore, me: Actor, guest: Actor) -> None:
    await _seed(store, me)
    assert (store.read(MOVE)[1].frontmatter or {})["private"] is True  # type default
    # A later write that does not say keeps the marker, for both kinds of page.
    await store.write_concept(FRIEND, person("Best friend (unnamed)"), "Rewritten.", "test", me)
    await store.append_to_concept(MOVE, "Notes", "- Boxes.", "test", me)
    assert (store.read(FRIEND)[1].frontmatter or {})["private"] is True
    assert (store.read(MOVE)[1].frontmatter or {})["private"] is True
    # The personal owner page is private by default too; the profile is not.
    owner = {"type": "owner", "title": "Me", "description": "The owner."}
    await store.write_concept("wiki/owner/personal", owner, "## Values", "test", me)
    await store.write_concept("wiki/owner/profile", owner, "## Goals", "test", me)
    assert store.is_private_file(PurePosixPath("wiki/owner/personal.md"))
    assert not store.is_private_file(PurePosixPath("wiki/owner/profile.md"))
    # Only an actor with wiki:private can un-mark, or opt out of a type's default.
    life = {"type": "life-event", "title": "Trip", "description": "A trip.", "private": False}
    with pytest.raises(WikiError, match="wiki:private"):
        await store.write_concept("wiki/life/trip", life, "Went.", "test", guest)
    await store.write_concept("wiki/life/trip", life, "Went.", "test", me)
    assert "private" not in (store.read("wiki/life/trip")[1].frontmatter or {})
    await store.write_concept(FRIEND, person("Best friend", private=False), "Now open.", "test", me)
    assert "private" not in (store.read(FRIEND)[1].frontmatter or {})
    with pytest.raises(WikiError):
        await store.write_concept(COLLEAGUE, person("Colleague", private="yes"), "x", "test", me)


# ------------------------------------------------------------------ reading


async def test_read_tools_hide_private_pages(
    store: WikiStore, tools: Tools, me: Actor, guest: Actor
) -> None:
    await _seed(store, me)

    # search and search_wiki
    assert (await tools.ok(guest, "search", query="zebrafish"))["results"] == []
    assert [r["id"] for r in (await tools.ok(me, "search", query="zebrafish"))["results"]] == [
        FRIEND
    ]
    assert (await tools.ok(guest, "search_wiki", query="zebrafish"))["results"] == []
    assert (await tools.ok(me, "search_wiki", query="zebrafish"))["results"][0]["id"] == FRIEND

    # read_concept and fetch: a private page is answered exactly like a missing one
    for tool, key in (("read_concept", "path"), ("fetch", "id"), ("get_backlinks", "path")):
        hidden = await tools.error(guest, tool, **{key: FRIEND})
        missing = await tools.error(guest, tool, **{key: "wiki/people/nobody-here"})
        assert hidden == missing.replace("wiki/people/nobody-here", FRIEND)
        assert (await tools.ok(me, tool, **{key: FRIEND}))["id"] == FRIEND
    assert await tools.error(guest, "read_concept", path=MOVE)

    # related, sources and backlinks shown with non-private pages leave private ids out
    hobby = await tools.ok(guest, "read_concept", path=HOBBY)
    assert hobby["frontmatter"]["related"] == [COLLEAGUE]
    fetched = await tools.ok(guest, "fetch", id=HOBBY)
    assert FRIEND not in fetched["text"] and FRIEND not in str(fetched["metadata"])
    assert FRIEND in (await tools.ok(me, "read_concept", path=HOBBY))["frontmatter"]["related"]
    colleague = await tools.ok(guest, "read_concept", path=COLLEAGUE)
    assert colleague["backlinks"] == [HOBBY]
    links = await tools.ok(guest, "get_backlinks", path=COLLEAGUE)
    assert [b["id"] for b in links["backlinks"]] == [HOBBY]
    mine = await tools.ok(me, "get_backlinks", path=COLLEAGUE)
    assert {b["id"] for b in mine["backlinks"]} == {HOBBY, FRIEND}
    assert all(b["private"] == (b["id"] == FRIEND) for b in mine["backlinks"])

    # list_concepts
    people = await tools.ok(guest, "list_concepts", type="person")
    assert [c["id"] for c in people["concepts"]] == [COLLEAGUE]
    assert (await tools.ok(guest, "list_concepts", folder="wiki/life"))["count"] == 0
    assert (await tools.ok(me, "list_concepts", folder="wiki/life"))["count"] == 1

    # index.md (get_index, the wiki-index resource, read_concept("index")) never lists them
    for actor in (guest, me):
        index = (await tools.ok(actor, "get_index"))["markdown"]
        assert "Colleague" in index and "Best friend" not in index and "Ghent" not in index
    assert "Best friend" not in str(await store_resource(tools, "wiki://index"))

    # recent_changes and log.md: nothing about the private writes
    changes = str(await tools.ok(guest, "recent_changes", limit=50))
    assert SECRET_REASON not in changes and FRIEND not in changes and "life/" not in changes
    assert "colleague" in changes
    assert SECRET_REASON in str(await tools.ok(me, "recent_changes", limit=50))
    for actor in (guest, me):
        log = (await tools.ok(actor, "read_concept", path="log"))["body"]
        assert "a private page" in log
        assert SECRET_REASON not in log and "Best friend" not in log and "Ghent" not in log

    # lint_wiki: no findings about private pages
    paths = {i["path"] for i in (await tools.ok(guest, "lint_wiki"))["issues"]}
    assert f"{FRIEND}.md" not in paths and f"{MOVE}.md" not in paths
    assert f"{MOVE}.md" in {i["path"] for i in (await tools.ok(me, "lint_wiki"))["issues"]}

    # get_wiki_guide tells which types are private by default
    types = {t["type"]: t for t in (await tools.ok(guest, "get_wiki_guide"))["types"]}
    assert types["life-event"]["private"] is True and "private" not in types["person"]
    assert "## Private pages" in (await tools.ok(guest, "get_wiki_guide"))["guide"]


async def store_resource(tools: Tools, uri: str) -> Any:
    return await tools.server.read_resource(uri)


async def test_unprocessed_sources_and_proposals_hide_private(
    store: WikiStore, tools: Tools, me: Actor, guest: Actor
) -> None:
    await _seed(store, me)
    portal = Actor("portal", Trust.AGENT)
    await store.import_chats(
        [
            ChatSource("claude-chat:open", "Open", "Hi"),
            ChatSource("claude-chat:mine", "Mine", "Hi"),
        ],
        portal,
    )
    await store.set_private("raw/chats/claude/mine", True, "Personal chat", me)
    seen = await tools.ok(guest, "list_unprocessed_sources")
    assert [s["id"] for s in seen["sources"]] == ["raw/chats/claude/open"]
    assert seen["total"] == 1 and seen["sources"][0]["private"] is False
    everything = await tools.ok(me, "list_unprocessed_sources")
    assert {s["id"]: s["private"] for s in everything["sources"]} == {
        "raw/chats/claude/open": False,
        "raw/chats/claude/mine": True,
    }

    # A proposal for a private page is private itself.
    res = await store.create_proposal(
        "append", PurePosixPath(f"{FRIEND}.md"), {"section": "N", "content": "x"}, "test", me,
        Trust.EXTERNAL, title="Friend",
    )  # fmt: skip
    pid = str(res.proposal_id)
    assert (await tools.ok(guest, "review_proposals"))["proposals"] == []
    assert [p["proposal_id"] for p in (await tools.ok(me, "review_proposals"))["proposals"]] == [
        pid
    ]
    for tool in ("accept_proposal", "reject_proposal"):
        err = await tools.error(guest, tool, proposal_id=pid, reason="no thanks")
        assert err.startswith(f"no page at {pid}.md")
    assert await tools.error(guest, "read_concept", path=pid)
    assert f"{pid}.md" in store.private_paths()


# ------------------------------------------------------------------ writing


async def test_writes_to_private_pages_fail_without_the_scope(
    store: WikiStore, tools: Tools, me: Actor, guest: Actor
) -> None:
    await _seed(store, me)
    head = store.repo.head()
    not_found = f"no page at {FRIEND}.md"
    err = await tools.error(
        guest, "append_to_concept", path=FRIEND, section="N", content="x", reason="add a note"
    )
    assert err.startswith(not_found)
    for src, dst in ((FRIEND, COLLEAGUE), (COLLEAGUE, FRIEND)):
        err = await tools.error(
            guest, "link_concepts", from_path=src, to_path=dst, relation="knows"
        )
        assert err.startswith(not_found)
    err = await tools.error(
        guest,
        "write_concept",
        path=FRIEND,
        frontmatter=person("Someone"),
        body="Overwrite.",
        reason="overwrite",
    )
    assert "cannot be written by this connection" in err and "private" not in err
    err = await tools.error(
        guest,
        "write_concept",
        path="wiki/people/x",
        frontmatter=person("X", related=[FRIEND]),
        body="x",
        reason="relate",
    )
    assert f"related page {FRIEND} does not exist" in err
    err = await tools.error(guest, "set_private", path=COLLEAGUE, private=False, reason="show")
    assert "wiki:private" in err
    assert store.repo.head() == head

    # Linking a non-private page to a private one would show it: refused for everyone.
    with pytest.raises(WikiError, match="non-private page to a private one"):
        await store.link_concepts(COLLEAGUE, FRIEND, "knows", "link", me)
    await store.link_concepts(FRIEND, COLLEAGUE, "knows", "link", me)

    # Without the scope, new private pages can still be created (write-only).
    res = await tools.ok(
        guest,
        "write_concept",
        path="wiki/people/partner",
        frontmatter=person("Partner", private=True),
        body="Private note.",
        reason="File a personal conversation",
    )
    assert res["created"] is True
    assert (await tools.error(guest, "read_concept", path="wiki/people/partner")).startswith(
        "no page at"
    )
    source = await tools.ok(
        guest, "ingest_source", title="Personal chat", content="Hi", private=True
    )
    assert store.is_private_file(PurePosixPath(source["path"]))
    assert (await tools.ok(me, "read_concept", path="wiki/people/partner"))["frontmatter"][
        "private"
    ]


async def test_marking_private_needs_only_write_access(
    store: WikiStore, tools: Tools, me: Actor, guest: Actor
) -> None:
    await _seed(store, me)
    writer = Actor("chatgpt", Trust.AGENT, frozenset({"wiki:read", "wiki:write"}))
    reader = Actor("reader", Trust.AGENT, frozenset({"wiki:read"}))
    source = await store.ingest_source("A talk", "Personal things", None, "agent", me)
    assert "wiki:write" in await tools.error(
        reader, "set_private", path=source.id, private=True, reason="Personal"
    )
    out = await tools.ok(writer, "set_private", path=source.id, private=True, reason="Personal")
    assert out["status"] == "committed" and store.is_private_file(PurePosixPath(source.path))
    assert _git(store, "log", "-1", "--format=%an").strip() == "chatgpt"
    await tools.ok(writer, "set_private", path=COLLEAGUE, private=True, reason="Personal")
    assert store.is_private_file(PurePosixPath(f"{COLLEAGUE}.md"))

    # Now private (or private from the start): answered like a missing page, whatever is asked.
    head = store.repo.head()
    for path in (source.id, COLLEAGUE, FRIEND, "wiki/people/nobody"):
        err = await tools.error(writer, "set_private", path=path, private=True, reason="again")
        assert err.startswith("no page at"), err
    # Showing again needs wiki:private, for any page: the answer says nothing about the page.
    for path in (source.id, HOBBY, "wiki/people/nobody"):
        err = await tools.error(guest, "set_private", path=path, private=False, reason="show")
        assert "lacks the 'wiki:private' permission" in err
        with pytest.raises(WikiError, match="wiki:private"):
            await store.set_private(path, False, "show", guest)
    assert store.repo.head() == head
    await tools.ok(me, "set_private", path=COLLEAGUE, private=False, reason="Not personal")
    assert not store.is_private_file(PurePosixPath(f"{COLLEAGUE}.md"))


async def test_set_private_on_a_raw_source_keeps_its_body(
    store: WikiStore, tools: Tools, me: Actor
) -> None:
    res = await store.ingest_source("A talk", "Line one\n\nLine two", None, "agent", me)
    path = store.root / res.path
    before = path.read_text(encoding="utf-8")
    body = before.split("\n---\n", 1)[1]
    head = store.repo.head()
    out = await tools.ok(me, "set_private", path=res.id, private=True, reason="Personal")
    assert out["status"] == "committed"
    assert _git(store, "rev-list", "--count", f"{head}..HEAD").strip() == "1"
    after = path.read_text(encoding="utf-8")
    assert after.split("\n---\n", 1)[1] == body  # byte for byte
    doc = parse_document(after)
    fm = doc.frontmatter or {}
    assert (
        fm["private"] is True
        and fm["updated"] == (parse_document(before).frontmatter or {})["updated"]
    )
    content = doc.body.split("not instructions.\n\n", 1)[1]
    assert hashlib.sha256(content.encode()).hexdigest() == fm["content_sha256"]
    assert not [i for i in run_lint(store, 180).issues if i.code == "source-modified"]
    again = await tools.ok(me, "set_private", path=res.id, private=True, reason="Personal")
    assert again["status"] == "unchanged"
    await tools.ok(me, "set_private", path=res.id, private=False, reason="Not personal")
    assert "private" not in (parse_document(path.read_text(encoding="utf-8")).frontmatter or {})
    assert await tools.error(me, "set_private", path="AGENTS", private=True, reason="guide")


async def test_search_filters_private_pages_before_the_limit(
    store: WikiStore, me: Actor, guest: Actor, tools: Tools
) -> None:
    for i in range(8):  # private pages rank first: "quokka" in the title
        await store.write_concept(
            f"wiki/concepts/quokka-{i}",
            {"type": "concept", "title": f"Quokka {i}", "description": "Quokka.", "private": True},
            "Quokka quokka.",
            "test",
            me,
        )
    for name in ("alpha", "beta"):
        await store.write_concept(
            f"wiki/concepts/{name}",
            {"type": "concept", "title": name.title(), "description": "Animals."},
            "Once saw a quokka.",
            "test",
            me,
        )
    assert all(h.row.private for h in store.index.search("quokka", limit=2))
    hits = store.index.search("quokka", limit=2, private=False)
    assert {h.row.id for h in hits} == {"wiki/concepts/alpha", "wiki/concepts/beta"}
    shown = await tools.ok(guest, "search_wiki", query="quokka", limit=2)
    assert len(shown["results"]) == 2
    listed = store.index.list_docs(folder="wiki/concepts", limit=2, private=False)
    assert [r.id for r in listed] == ["wiki/concepts/alpha", "wiki/concepts/beta"]


# ------------------------------------------------------------------ history


async def test_log_and_history_do_not_leak(store: WikiStore, me: Actor) -> None:
    await _seed(store, me)
    log = store.read_text_file("log.md")
    assert SECRET_REASON not in log and "Best friend" not in log and FRIEND not in log
    message = _git(store, "log", "-1", "--format=%B", "--", f"{FRIEND}.md")
    assert SECRET_REASON in message and "Private: yes" in message  # kept for the owner
    commits = store.recent_changes(limit=50, private=False)
    assert not any(FRIEND in str(c.files) or SECRET_REASON in c.body for c in commits)
    assert len(store.recent_changes(limit=2, private=False)) == 2  # filtered, then limited

    # A page marked private later: its earlier log lines and commits disappear as well.
    assert "[Colleague](/wiki/people/colleague.md)" in log
    await store.set_private(COLLEAGUE, True, "Personal after all", me)
    assert "Colleague" not in store.visible_log(store.read_text_file("log.md"))
    assert not any(
        f"{COLLEAGUE}.md" in [p for _, p in c.files]
        for c in store.recent_changes(limit=50, private=False)
    )


# ------------------------------------------------------------------ batch filing


async def test_file_conversation_private_pages_and_sources(store: WikiStore, me: Actor) -> None:
    portal = Actor("portal", Trust.AGENT, frozenset({"wiki:read", "wiki:write", "wiki:private"}))
    await _seed(store, me)
    await store.import_chats(
        [ChatSource("claude-chat:ppp", "Divorce talk", "## User\n\nHi")], portal
    )
    src = "raw/chats/claude/ppp"
    raw = store.root / f"{src}.md"
    body = raw.read_text(encoding="utf-8").split("\n---\n", 1)[1]
    head = store.repo.head()
    pages = [
        FilingPage(
            "wiki/people/sister",
            (FilingSection("Feelings", ("- 2026-09: The owner misses her [^ppp]",)),),
            type="person",
            title="Sister",
            description="The owner's sister.",
            private=True,
        ),
        FilingPage(COLLEAGUE, (), private=True),  # existing: marked private
        FilingPage(
            "wiki/concepts/moving",
            (FilingSection("Notes", ("- Boxes are cheap [^ppp]",)),),
            type="concept",
            title="Moving",
            description="Moving house.",
        ),
    ]
    res = await store.file_conversation(
        [src], pages, "Filed a personal chat", portal, private_sources=True
    )
    assert sorted(res.created) == ["wiki/concepts/moving", "wiki/people/sister"]
    assert res.updated == [COLLEAGUE]
    assert _git(store, "rev-list", "--count", f"{head}..HEAD").strip() == "1"
    assert store.is_private_file(PurePosixPath("wiki/people/sister.md"))
    assert store.is_private_file(PurePosixPath(f"{COLLEAGUE}.md"))
    assert not store.is_private_file(PurePosixPath("wiki/concepts/moving.md"))
    assert store.is_private_file(PurePosixPath(f"{src}.md"))
    assert raw.read_text(encoding="utf-8").split("\n---\n", 1)[1] == body
    log = store.read_text_file("log.md")
    assert "Filed a personal chat" not in log and "Sister" not in log
    assert "[Divorce talk]" in log  # logged at import, before it was private: hidden on read
    assert "Divorce" not in store.visible_log(log)
    assert "[Moving](/wiki/concepts/moving.md)" in log  # shown, but without the reason

    # Never un-marked by /file; a later filing without the flag keeps it.
    await store.file_conversation(
        [src],
        [FilingPage(COLLEAGUE, (FilingSection("Notes", ("- Again [^ppp]",)),))],
        "again",
        portal,
    )
    assert store.is_private_file(PurePosixPath(f"{COLLEAGUE}.md"))

    # A skipped private conversation: no title, no reason, and replace still finds the line.
    await store.import_chats([ChatSource("claude-chat:sk", "Therapy notes", "Hi")], portal)
    skip = "raw/chats/claude/sk"
    await store.file_conversation(
        [skip], [], "skip", portal, skip="only feelings", private_sources=True
    )
    skipped = store.read("wiki/references/skipped-conversations")[1]
    assert f"- [Private conversation](/{skip}.md)\n" in skipped.body + "\n"
    assert "Therapy" not in skipped.body and "only feelings" not in skipped.body
    assert not store.is_private_file(PurePosixPath("wiki/references/skipped-conversations.md"))
    assert store.is_private_file(PurePosixPath(f"{skip}.md"))
    assert store.unprocessed_sources()["total"] == 0
    await store.file_conversation(
        [skip],
        [FilingPage("wiki/people/sister", (FilingSection("Notes", ("- Called [^sk]",)),))],
        "again",
        portal,
        replace=True,
    )
    assert skip not in store.read("wiki/references/skipped-conversations")[1].body

    # Without the scope, /file-style writes to existing private pages fail neutrally.
    guest = Actor("portal", Trust.AGENT)
    await store.import_chats([ChatSource("claude-chat:pub", "Work", "Hi")], guest)
    with pytest.raises(WikiError, match="cannot be written"):
        await store.file_conversation(
            ["raw/chats/claude/pub"], [FilingPage(COLLEAGUE, ())], "filing", guest
        )
    with pytest.raises(WikiError, match=f"no page at {src}.md"):
        await store.file_conversation([src], [], "filing", guest, skip="nothing")


# ------------------------------------------------------------------ graph and index


async def test_graph_leaves_out_private_nodes(store: WikiStore, me: Actor) -> None:
    await _seed(store, me)
    full = build_graph(store.index)
    nodes = {n["id"]: n for n in full["nodes"]}
    assert nodes[FRIEND]["private"] is True and "private" not in nodes[COLLEAGUE]
    public = build_graph(store.index, private=False)
    ids = {n["id"] for n in public["nodes"]}
    assert FRIEND not in ids and MOVE not in ids and COLLEAGUE in ids
    assert not any(FRIEND in (e["source"], e["target"]) for e in public["edges"])
    colleague = next(n for n in public["nodes"] if n["id"] == COLLEAGUE)
    assert colleague["backlink_count"] == 1  # the hobby page only


def test_index_from_before_private_is_rebuilt(tmp_path: Path) -> None:
    db = sqlite3.connect(tmp_path / "index.sqlite")
    db.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, path TEXT NOT NULL, type TEXT)")
    db.execute("INSERT INTO docs VALUES ('a', 'a.md', 'concept')")
    db.commit()
    db.close()
    index = SearchIndex(tmp_path / "index.sqlite")
    assert index.all() == [] and index.private_ids() == set()
    index.close()
