"""The link graph for the portal (wiki.graph.build_graph) and the index reads it uses."""

from __future__ import annotations

from typing import Any

from tests.conftest import person
from wiki_mcp.config import Trust
from wiki_mcp.wiki.actor import Actor
from wiki_mcp.wiki.graph import build_graph, conversation_of
from wiki_mcp.wiki.store import ChatSource, FilingPage, FilingSection, WikiStore

PORTAL = Actor("portal", Trust.AGENT)
SKIPPED = "wiki/references/skipped-conversations"
TRIP = "raw/chats/claude/trip-1"
IDLE = "raw/chats/claude/idle-2"
CHITCHAT = "raw/chats/chatgpt/chit-3"


def _chat(key: str, **extra: Any) -> ChatSource:
    return ChatSource(key, "Chat", "## User\n\nHello", **extra)


async def _wiki(store: WikiStore, agent: Actor) -> None:
    """A page citing a two-part chat that was continued, an uncited chat, a chat filed as
    skipped, a draft from an imported chat and a page with a broken link."""
    await store.import_chats(
        [
            _chat(
                "claude-chat:trip-1", parts=2, messages=4, last_message_at="2026-03-01T10:00:00Z"
            ),
            _chat("claude-chat:trip-1", part=2, parts=2, messages=4),
            _chat("claude-chat:idle-2", messages=1, last_message_at="2026-04-02T10:00:00Z"),
            _chat("chatgpt-chat:chit-3", messages=1),
        ],
        PORTAL,
    )
    await store.import_chats(
        [_chat("claude-chat:trip-1", messages=6, after=4, last_message_at="2026-05-01T10:00:00Z")],
        PORTAL,
    )
    await store.write_concept(
        "wiki/people/jane-doe",
        person(sources=[TRIP], status="stable"),
        "Jane went to [Lisbon](/wiki/projects/lisbon.md) and [nowhere](/wiki/places/atlantis.md).",
        "from a chat",
        agent,
    )
    await store.file_conversation(
        [
            "raw/chats/claude/trip-1-after-4",
        ],
        [
            FilingPage(
                "wiki/projects/lisbon",
                (FilingSection("Timeline", ("- 2026-05: Booked the flights",)),),
                type="project",
                title="Lisbon",
                description="A trip to Lisbon.",
            )
        ],
        "filed",
        PORTAL,
    )
    await store.file_conversation([CHITCHAT], [], "nothing to file", PORTAL, skip="small talk")


def _by_id(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["id"]: n for n in graph["nodes"]}


async def test_wiki_pages_only_by_default(store: WikiStore, agent: Actor) -> None:
    await _wiki(store, agent)
    graph = build_graph(store.index, skipped_page=SKIPPED)
    nodes = _by_id(graph)
    assert not [i for i in nodes if i.startswith("raw/")]
    jane = nodes["wiki/people/jane-doe"]
    assert jane["type"] == "person" and jane["folder"] == "wiki/people"
    assert jane["title"] == "Jane Doe" and "test" in jane["tags"]
    assert "draft" not in jane and "unprocessed" not in jane
    # the draft written from an imported chat alone
    assert nodes["wiki/projects/lisbon"]["draft"] is True
    assert nodes["wiki/projects/lisbon"]["backlink_count"] == 1
    # a missing page is a ghost behind a broken edge; a left-out raw source is not
    ghost = nodes["wiki/places/atlantis"]
    assert ghost["ghost"] is True and ghost["type"] == ""
    edges = {(e["source"], e["target"]): e for e in graph["edges"]}
    assert edges[("wiki/people/jane-doe", "wiki/places/atlantis")]["broken"] is True
    assert "broken" not in edges[("wiki/people/jane-doe", "wiki/projects/lisbon")]
    assert not [e for e in graph["edges"] if e["target"].startswith("raw/")]
    assert all(e["source"] in nodes and e["target"] in nodes for e in graph["edges"])


async def test_raw_sources_on_request(store: WikiStore, agent: Actor) -> None:
    await _wiki(store, agent)
    nodes = _by_id(build_graph(store.index, raw=True, skipped_page=SKIPPED))
    trip, part2, after = nodes[TRIP], nodes[f"{TRIP}-part-2"], nodes[f"{TRIP}-after-4"]
    assert trip["conversation"] == part2["conversation"] == after["conversation"] == TRIP
    assert part2["part"] == 2 and after["after"] == 4 and "part" not in trip
    assert trip["folder"] == "raw/chats/claude" and trip["type"] == "source"
    # processed: cited by Jane (the parts count together) and by the filed draft
    assert "unprocessed" not in trip and "unprocessed" not in part2
    assert "unprocessed" not in after
    assert trip["backlink_count"] == 1
    # never cited
    assert nodes[IDLE]["unprocessed"] is True
    assert nodes[IDLE]["date"] == "2026-04-02T10:00:00Z"
    # only the skipped-conversations page cites it
    assert nodes[CHITCHAT]["skipped"] is True and "unprocessed" not in nodes[CHITCHAT]
    assert nodes[SKIPPED]["backlink_count"] == 0


async def test_proposals_on_request(store: WikiStore, agent: Actor) -> None:
    src = await store.ingest_source(
        "Web", "Acme was founded in 1999.", "https://e.x", "external", agent
    )
    fm = {"type": "organization", "title": "Acme", "description": "A company.", "sources": [src.id]}
    res = await store.write_concept("wiki/organizations/acme", fm, "1999.", "from web", agent)
    assert res.proposal_id
    without = _by_id(build_graph(store.index))
    assert not [i for i in without if i.startswith("proposals/")]
    nodes = _by_id(build_graph(store.index, proposals=store.list_proposals()))
    proposal = nodes[res.proposal_id]
    assert proposal["type"] == "proposal" and proposal["folder"] == "proposals"
    assert proposal["title"].startswith("Proposal:")


def test_conversation_keys() -> None:
    assert conversation_of("raw/chats/claude/abc") == ("raw/chats/claude/abc", 0, 1)
    assert conversation_of("raw/chats/claude/abc-part-3") == ("raw/chats/claude/abc", 0, 3)
    assert conversation_of("raw/chats/chatgpt/abc-after-12-part-2") == (
        "raw/chats/chatgpt/abc",
        12,
        2,
    )
    assert conversation_of("raw/web/abc") is None
    assert conversation_of("wiki/people/jane") is None


async def test_index_generation_and_activity(store: WikiStore, agent: Actor) -> None:
    before = store.index.generation
    await store.write_concept("wiki/people/jane-doe", person(), "Jane.", "test", agent)
    assert store.index.generation > before
    dates = store.index.activity()
    assert dates["wiki/people/jane-doe"]  # its `updated`
    await store.import_chats(
        [_chat("claude-chat:x1", messages=1, last_message_at="2025-12-31T23:00:00Z")], PORTAL
    )
    assert store.index.activity()["raw/chats/claude/x1"] == "2025-12-31T23:00:00Z"
    counts = store.index.backlink_counts()
    assert all(v >= 1 for v in counts.values())


# ------------------------------------------------------------------ drafts: status decides

CHATGPT = Actor("chatgpt", Trust.AGENT)
PAGE = "wiki/people/jane-doe"


def _drafts(store: WikiStore) -> set[str]:
    """Drafts as the portal sees them: the graph's `draft` marks (explorer, graph, fragment
    list) and GET /docs?status=draft (the Drafts page and the home page's count)."""
    marked = {n["id"] for n in build_graph(store.index, raw=True)["nodes"] if n.get("draft")}
    listed = {r.id for r in store.index.list_docs(status="draft") if not r.path.startswith("raw/")}
    assert marked == listed
    return marked


def _provenance(**extra: Any) -> dict[str, Any]:
    return person(tags=["test", "from-chat-import"], **extra)


async def test_a_draft_agent_page_is_a_draft(store: WikiStore, agent: Actor) -> None:
    await store.write_concept(PAGE, person(status="draft"), "Text.", "draft", agent)
    assert store.index.get(PAGE).status == "draft"
    assert _drafts(store) == {PAGE}


async def test_a_stable_agent_page_is_not_a_draft(store: WikiStore) -> None:
    await store.write_concept(PAGE, _provenance(status="stable"), "Text.", "stable", CHATGPT)
    _, doc, _ = store.read(PAGE)
    fm = doc.frontmatter or {}
    assert fm["trust"] == "agent" and fm["generated"]["by"] == "chatgpt/wiki-mcp"
    assert "from-chat-import" in fm["tags"]
    assert _drafts(store) == set()


async def test_draft_to_stable_leaves_drafts_and_keeps_provenance(store: WikiStore) -> None:
    await store.write_concept(PAGE, _provenance(status="draft"), "Text.", "draft", CHATGPT)
    assert _drafts(store) == {PAGE}
    version = store.index.generation
    await store.write_concept(PAGE, _provenance(status="stable"), "Text.", "reviewed", CHATGPT)
    assert _drafts(store) == set()
    assert store.index.generation > version  # the portal's cached graph is revalidated
    _, doc, _ = store.read(PAGE)
    fm = doc.frontmatter or {}
    assert fm["status"] == "stable" and fm["trust"] == "agent"
    assert fm["generated"]["by"] == "chatgpt/wiki-mcp" and "from-chat-import" in fm["tags"]


async def test_stable_to_draft_joins_drafts(store: WikiStore, agent: Actor) -> None:
    await store.write_concept(PAGE, person(status="stable"), "Text.", "stable", agent)
    assert _drafts(store) == set()
    await store.write_concept(PAGE, person(status="draft"), "Text.", "to check", agent)
    assert _drafts(store) == {PAGE}


async def test_provenance_does_not_change_drafts(
    store: WikiStore, agent: Actor, owner: Actor
) -> None:
    await store.import_chats([_chat("claude-chat:trip-1", messages=1)], PORTAL)
    for status, expected in (("stable", set()), ("draft", {PAGE})):
        for fm, actor in (
            (person(status=status), owner),  # trust owner, no import tag
            (_provenance(status=status), CHATGPT),  # agent, chatgpt, from-chat-import
            (_provenance(status=status, sources=[TRIP]), agent),  # cites an imported chat
            (person(status=status, tags=["other"]), agent),
        ):
            await store.write_concept(PAGE, fm, "Text.", "provenance", actor)
            assert _drafts(store) == expected, (status, fm, actor)


async def test_raw_sources_are_never_drafts(store: WikiStore) -> None:
    await store.import_chats([_chat("claude-chat:trip-1", messages=1)], PORTAL)
    assert _drafts(store) == set()
