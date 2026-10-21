from __future__ import annotations

import asyncio
import subprocess
from pathlib import PurePosixPath

import pytest

from tests.conftest import SEED_DIR, person
from wiki_mcp.config import Trust
from wiki_mcp.wiki.actor import Actor
from wiki_mcp.wiki.lint import lint_bundle
from wiki_mcp.wiki.okf import parse_document
from wiki_mcp.wiki.store import (
    ChatSource,
    FilingPage,
    FilingSection,
    WikiError,
    WikiStore,
    insert_into_section,
    markdown_section,
    text_window,
)


def git(store: WikiStore, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", f"safe.directory={store.root}", "-C", str(store.root), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


async def test_seed_initialised_and_lint_clean(store: WikiStore, registry) -> None:  # type: ignore[no-untyped-def]
    assert store.repo.head() is not None
    assert "Initialize wiki from seed bundle" in git(store, "log", "--format=%s")
    assert not store.repo.is_dirty()
    index_md = store.read_text_file("index.md")
    assert index_md.startswith('---\nokf_version: "0.2"\n---')
    assert "[Company Brain](/wiki/concepts/company-brain.md)" in index_md
    report = lint_bundle(list(store.all_markdown()), registry)
    assert report.ok, report.to_markdown()
    assert not [i for i in report.issues if i.severity == "warning"], report.to_markdown()


async def test_restart_is_idempotent(store: WikiStore) -> None:
    head = store.repo.head()
    await store.startup(None)
    assert store.repo.head() == head


async def test_write_commits_with_author_and_reason(store: WikiStore, agent: Actor) -> None:
    res = await store.write_concept(
        "wiki/people/jane-doe", person(), "Jane is a test person.", "Met Jane at conf", agent
    )
    assert res.status == "committed" and res.created and res.id == "wiki/people/jane-doe"
    log = git(store, "log", "-1", "--format=%an <%ae>%n%s%n%b")
    assert "claude.ai <claude.ai@clients.wiki.local>" in log
    assert "Create wiki/people/jane-doe.md" in log
    assert "Met Jane at conf" in log and "Trust: agent" in log
    assert not store.repo.is_dirty()
    _, doc, _ = store.read("wiki/people/jane-doe")
    fm = doc.frontmatter or {}
    assert fm["id"] == "wiki/people/jane-doe"
    assert fm["generated"]["by"] == "claude.ai/wiki-mcp"
    assert fm["trust"] == "agent" and fm["created"] == fm["updated"]
    assert "[Jane Doe](/wiki/people/jane-doe.md)" in store.read_text_file("index.md")
    assert (
        "**Creation**: [Jane Doe](/wiki/people/jane-doe.md) — Met Jane at conf _(by claude.ai)_"
        in store.read_text_file("log.md")
    )


async def test_update_keeps_created(store: WikiStore, agent: Actor) -> None:
    await store.write_concept("wiki/people/jane-doe", person(), "v1", "create", agent)
    _, d1, _ = store.read("wiki/people/jane-doe")
    await asyncio.sleep(1.1)
    res = await store.write_concept("wiki/people/jane-doe", person(), "v2", "update", agent)
    assert not res.created
    _, d2, _ = store.read("wiki/people/jane-doe")
    assert d2.frontmatter["created"] == d1.frontmatter["created"]  # type: ignore[index]
    assert d2.frontmatter["updated"] != d1.frontmatter["updated"]  # type: ignore[index]
    assert d2.body.strip() == "v2"


async def test_search_index_in_sync(store: WikiStore, agent: Actor) -> None:
    await store.write_concept(
        "wiki/people/jane-doe", person(), "Works on quantum widgets.", "test change", agent
    )
    hits = store.index.search("quantum widget")
    assert [h.row.id for h in hits] == ["wiki/people/jane-doe"]
    assert "**quantum**" in hits[0].snippet
    await store.write_concept(
        "wiki/people/jane-doe", person(), "Now works on gardening.", "test change", agent
    )
    assert store.index.search("quantum") == []
    assert store.index.search("gardening")[0].row.id == "wiki/people/jane-doe"
    assert store.index.search("gardening", type="concept") == []
    assert store.index.search("gardening", tags=["test"])[0].row.title == "Jane Doe"
    store.index.search('"; DROP TABLE docs; --')  # quoted tokens: harmless
    assert store.index.get("wiki/people/jane-doe") is not None


async def test_validation_errors_do_not_commit(store: WikiStore, agent: Actor) -> None:
    head = store.repo.head()
    with pytest.raises(WikiError, match="must live under wiki/people"):
        await store.write_concept("wiki/projects/jane", person(), "x", "reason", agent)
    with pytest.raises(WikiError, match="reason"):
        await store.write_concept("wiki/people/jane", person(), "x", "", agent)
    with pytest.raises(WikiError, match="related page"):
        await store.write_concept(
            "wiki/people/jane", person(related=["wiki/people/nobody"]), "x", "reason", agent
        )
    with pytest.raises(WikiError, match="does not exist"):
        await store.write_concept(
            "wiki/people/jane", person(sources=["raw/missing"]), "x", "reason", agent
        )
    with pytest.raises(WikiError, match="limit"):
        await store.write_concept("wiki/people/jane", person(), "x" * 300_000, "reason", agent)
    with pytest.raises(WikiError, match="credential"):
        await store.write_concept(
            "wiki/people/jane",
            person(),
            "key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
            "reason",
            agent,
        )
    with pytest.raises(WikiError):
        await store.write_concept("../../etc/passwd", person(), "x", "reason", agent)
    assert store.repo.head() == head and not store.repo.is_dirty()


async def test_rollback_on_commit_failure(
    store: WikiStore, agent: Actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: object, **k: object) -> str:
        raise RuntimeError("disk full")

    monkeypatch.setattr(store.repo, "commit", boom)
    with pytest.raises(RuntimeError):
        await store.write_concept("wiki/people/jane-doe", person(), "x", "reason", agent)
    monkeypatch.undo()
    assert not (store.root / "wiki/people/jane-doe.md").exists()
    assert not store.repo.is_dirty()
    assert store.index.get("wiki/people/jane-doe") is None


async def test_concurrent_writes_are_serialised(store: WikiStore, agent: Actor) -> None:
    head_count = len(git(store, "log", "--format=%H").split())
    await asyncio.gather(
        *[
            store.write_concept(
                f"wiki/people/p{i}", person(f"Person {i}"), f"body {i}", f"add {i}", agent
            )
            for i in range(8)
        ]
    )
    assert len(git(store, "log", "--format=%H").split()) == head_count + 8
    assert not store.repo.is_dirty()
    log_md = store.read_text_file("log.md")
    assert all(f"Person {i}" in log_md for i in range(8))


async def test_append_to_concept(store: WikiStore, agent: Actor) -> None:
    await store.write_concept(
        "wiki/people/jane-doe",
        person(),
        "# Jane\n\n## Notes\n\nfirst\n\n## Other\n\nx",
        "test change",
        agent,
    )
    await store.append_to_concept("wiki/people/jane-doe", "Notes", "second", "add note", agent)
    await store.append_to_concept(
        "wiki/people/jane-doe", "Timeline", "- 2026: joined", "add timeline", agent
    )
    _, doc, _ = store.read("wiki/people/jane-doe")
    assert doc.body.index("first") < doc.body.index("second") < doc.body.index("## Other")
    assert doc.body.rstrip().endswith("## Timeline\n\n- 2026: joined")
    with pytest.raises(WikiError, match="editable"):
        await store.append_to_concept("AGENTS", "Notes", "x", "reason", agent)


def test_insert_into_section_ignores_code_fences() -> None:
    body = "## Notes\n\n```\n## Not a heading\n```\n\n## End\n"
    out = insert_into_section(body, "Notes", "added")
    assert out.index("added") < out.index("## End")
    assert out.index("## Not a heading") < out.index("added")


async def test_link_and_backlinks(store: WikiStore, agent: Actor) -> None:
    await store.write_concept("wiki/people/jane-doe", person(), "Jane.", "test change", agent)
    await store.write_concept(
        "wiki/organizations/acme",
        {"type": "organization", "title": "Acme", "description": "A company."},
        "Acme.",
        "test change",
        agent,
    )
    await store.link_concepts(
        "wiki/people/jane-doe", "wiki/organizations/acme", "works at", "job", agent
    )
    await store.link_concepts(
        "wiki/people/jane-doe", "wiki/organizations/acme", "works at", "again", agent
    )
    _, doc, _ = store.read("wiki/people/jane-doe")
    assert doc.frontmatter["related"] == ["wiki/organizations/acme"]  # type: ignore[index]
    assert doc.body.count("- works at: [Acme](/wiki/organizations/acme.md)") == 1
    assert [r.id for r in store.index.backlinks("wiki/organizations/acme")] == [
        "wiki/people/jane-doe"
    ]


async def test_ingest_source_and_trust(store: WikiStore, agent: Actor) -> None:
    res = await store.ingest_source(
        "Conf notes", "Jane said hi.", "https://example.com/notes", "agent", agent
    )
    assert res.path.startswith("raw/") and res.path.endswith("-conf-notes.md")
    again = await store.ingest_source("Conf notes", "Other", None, None, agent)
    assert again.path.endswith("-conf-notes-2.md") and again.trust == "external"
    with pytest.raises(WikiError, match="trust up to 'agent'"):
        await store.ingest_source("x", "y", None, "owner", agent)
    _, doc, _ = store.read(res.id)
    assert doc.type == "source" and doc.frontmatter["resource"] == "https://example.com/notes"  # type: ignore[index]
    assert "untrusted data" in doc.body
    # citing an agent-trust source writes directly
    ok = await store.write_concept(
        "wiki/people/jane-doe", person(sources=[res.id]), "Jane.[^x]", "test change", agent
    )
    assert ok.status == "committed"


async def test_external_source_creates_proposal(
    store: WikiStore, agent: Actor, owner: Actor, registry
) -> None:  # type: ignore[no-untyped-def]
    src = await store.ingest_source(
        "Web page", "Acme was founded in 1999.", "https://example.com", "external", agent
    )
    fm = {"type": "organization", "title": "Acme", "description": "A company.", "sources": [src.id]}
    res = await store.write_concept(
        "wiki/organizations/acme", fm, "Founded 1999.", "from web", agent
    )
    assert res.status == "proposed" and res.proposal_id and res.proposal_id.startswith("proposals/")
    assert not (store.root / "wiki/organizations/acme.md").exists()
    proposals = store.list_proposals()
    assert (
        proposals[0]["target"] == "wiki/organizations/acme.md"
        and proposals[0]["proposed_by"] == "claude.ai"
    )
    assert lint_bundle(list(store.all_markdown()), registry).ok

    accepted = await store.accept_proposal(res.proposal_id, "looks right", owner)
    assert accepted.status == "committed"
    _, doc, _ = store.read("wiki/organizations/acme")
    assert doc.frontmatter["verified"][0]["by"] == "human:owner"  # type: ignore[index]
    assert doc.frontmatter["generated"]["by"] == "claude.ai/wiki-mcp"  # type: ignore[index]
    assert store.list_proposals() == []
    assert "Proposed-by: claude.ai" in git(store, "log", "-1", "--format=%b")
    assert lint_bundle(list(store.all_markdown()), registry).ok


async def test_reject_proposal(store: WikiStore, owner: Actor) -> None:
    ext = Actor("chatgpt", Trust.EXTERNAL)
    res = await store.write_concept("wiki/people/x", person("X"), "x", "untrusted", ext)
    assert res.status == "proposed"
    out = await store.reject_proposal(res.proposal_id or "", "not true", owner)
    assert out.status == "rejected" and store.list_proposals() == []
    with pytest.raises(WikiError):
        await store.accept_proposal("wiki/people/x", "nope", owner)


async def test_proposals_disabled(store: WikiStore) -> None:
    store.proposal_threshold = None
    res = await store.write_concept(
        "wiki/people/x", person("X"), "x", "test change", Actor("chatgpt", Trust.EXTERNAL)
    )
    assert res.status == "committed" and res.trust == "external"


async def test_recent_changes(store: WikiStore, agent: Actor) -> None:
    await store.write_concept("wiki/people/jane-doe", person(), "x", "why", agent)
    changes = store.recent_changes(limit=5)
    assert changes[0].author == "claude.ai"
    assert ("A", "wiki/people/jane-doe.md") in changes[0].files
    assert store.recent_changes(since="2000-01-01", limit=100)
    with pytest.raises(WikiError):
        store.recent_changes(since="--output=/tmp/x")


async def test_out_of_band_edits_recorded(store: WikiStore) -> None:
    path = store.root / "wiki/concepts/manual.md"
    path.write_text("---\ntype: concept\ntitle: Manual\ndescription: Hand edited.\n---\nHello\n")
    await store.startup(None)
    assert not store.repo.is_dirty()
    assert store.index.get("wiki/concepts/manual") is not None
    assert "Record out-of-band changes" in git(store, "log", "-1", "--format=%s")


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("raw/x", "source"),
        ("wiki/inferred/x", "inference"),
        ("wiki/briefing/hot-memory", "briefing"),
    ],
)
async def test_managed_types_not_writable(
    store: WikiStore, agent: Actor, owner: Actor, path: str, kind: str
) -> None:
    for actor in (agent, owner):
        with pytest.raises(WikiError, match="managed"):
            await store.write_concept(
                path, {"type": kind, "title": "x", "description": "x"}, "x", "test change", actor
            )


def test_documents_parse(store: WikiStore) -> None:
    for rel, doc in store.iter_documents():
        assert doc.type, rel
        assert parse_document(store.read_text_file(str(rel))).frontmatter
    assert PurePosixPath("AGENTS.md") in [r for r, _ in store.iter_documents()]


async def test_new_guide_sections_reach_existing_wikis(store: WikiStore) -> None:
    agents = store.root / "AGENTS.md"
    seed_text = (SEED_DIR / "AGENTS.md").read_text(encoding="utf-8")
    section = markdown_section(seed_text, "Filing a conversation")
    facts = markdown_section(seed_text, "Facts and inferences")
    assert section and section.startswith("## Filing a conversation")
    assert facts and "superseded YYYY-MM-DD" in facts and "wiki/inferred/" in facts
    personal = markdown_section(seed_text, "Duplicates and aliases")
    assert personal and "merge_identity" in personal
    # A wiki created before the sections existed, with an edit by the owner.
    old = agents.read_text(encoding="utf-8").replace(section + "\n\n", "")
    old = old.replace(facts + "\n\n", "").replace(personal + "\n\n", "")
    assert "## Facts and inferences" not in old
    assert "## Duplicates and aliases" not in old
    agents.write_text(old.replace("Keep it accurate", "Keep it VERY accurate"), encoding="utf-8")
    git(store, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "old")
    await store.startup(SEED_DIR)
    text = agents.read_text(encoding="utf-8")
    assert "Keep it VERY accurate" in text and text.count("## Filing a conversation") == 1
    assert text.count("## Facts and inferences") == 1
    assert text.count("## Duplicates and aliases") == 1
    assert (
        git(store, "log", "-1", "--format=%s").strip() == "Add new house-rule sections to AGENTS.md"
    )
    assert not store.repo.is_dirty()
    head = store.repo.head()
    await store.startup(SEED_DIR)
    assert store.repo.head() == head


def test_markdown_section() -> None:
    text = "# T\n\n## A\n\none\n\n### sub\n\n## B\n\ntwo\n"
    assert markdown_section(text, "A") == "## A\n\none\n\n### sub"
    assert markdown_section(text, "B") == "## B\n\ntwo"
    assert markdown_section(text, "C") is None


async def test_owner_profile_page(store: WikiStore, agent: Actor) -> None:
    fm = {"type": "owner", "title": "Owner profile", "description": "Who the owner is."}
    res = await store.write_concept("wiki/owner/profile", fm, "## Preferences\n", "test", agent)
    assert res.status == "committed" and res.id == "wiki/owner/profile"
    with pytest.raises(WikiError):
        await store.write_concept("wiki/people/me", fm, "x", "test change", agent)


async def test_cited_sources_are_backlinks_and_mark_processing(
    store: WikiStore, agent: Actor
) -> None:
    portal = Actor("portal", Trust.AGENT)
    await store.import_chats(
        [
            ChatSource("claude-chat:aaa", "Shed", "## User\n\nHi"),
            ChatSource("claude-chat:bbb", "Trip", "## User\n\npart one", 1, 2),
            ChatSource("claude-chat:bbb", "Trip", "## User\n\npart two", 2, 2),
        ],
        portal,
    )
    notes = await store.ingest_source("Notes", "text", None, "agent", agent)
    listing = store.unprocessed_sources()
    assert listing["total"] == 2
    shed, trip = listing["sources"]
    assert shed["id"] == "raw/chats/claude/aaa" and shed["parts"] == ["raw/chats/claude/aaa"]
    assert trip["parts"] == ["raw/chats/claude/bbb", "raw/chats/claude/bbb-part-2"]
    sizes = [(store.root / f"{i}.md").stat().st_size for i in trip["parts"]]
    assert trip["bytes"] == sum(sizes) and trip["title"] == "Trip (part 1 of 2)"
    everything = store.unprocessed_sources(tag="")
    assert notes.id in {s["id"] for s in everything["sources"]} and everything["total"] == 3
    assert store.unprocessed_sources(limit=1)["total"] == 2

    fm = {
        "type": "project",
        "title": "Trip",
        "description": "A trip.",
        "sources": ["raw/chats/claude/bbb-part-2"],
    }
    await store.write_concept("wiki/projects/trip", fm, "Planned.", "from a chat", agent)
    assert [r.id for r in store.index.backlinks("raw/chats/claude/bbb-part-2")] == [
        "wiki/projects/trip"
    ]
    assert "raw/chats/claude/bbb-part-2" in store.index.outlinks("wiki/projects/trip")
    assert [s["id"] for s in store.unprocessed_sources()["sources"]] == ["raw/chats/claude/aaa"]

    skipped = {"type": "reference", "title": "Skipped", "description": "Nothing to file."}
    body = "## Skipped\n\n- [Shed](/raw/chats/claude/aaa.md): small talk"
    await store.write_concept("wiki/references/skipped-conversations", skipped, body, "skip", agent)
    assert store.unprocessed_sources() == {"total": 0, "sources": []}
    await store.startup(None)  # the index is rebuilt from the files
    assert store.unprocessed_sources()["total"] == 0
    assert store.index.backlinks("raw/chats/claude/bbb-part-2")


async def test_a_continuation_is_unprocessed_until_cited(store: WikiStore, agent: Actor) -> None:
    portal = Actor("portal", Trust.AGENT)
    await store.import_chats(
        [ChatSource("claude-chat:ccc", "Chat", "## User\n\nHi", messages=2)], portal
    )
    fm = {
        "type": "concept",
        "title": "C",
        "description": "A concept.",
        "sources": ["raw/chats/claude/ccc"],
    }
    await store.write_concept("wiki/concepts/c", fm, "x", "from a chat", agent)
    assert store.unprocessed_sources()["total"] == 0
    tail = ChatSource("claude-chat:ccc", "Chat", "## User\n\nMore", messages=4, after=2)
    result = await store.import_chats([tail], portal)
    assert [did for _, did in result.created] == ["raw/chats/claude/ccc-after-2"]
    assert [s["id"] for s in store.unprocessed_sources()["sources"]] == [
        "raw/chats/claude/ccc-after-2"
    ]
    assert store.chat_state("claude-chat:ccc").messages == 4
    assert store.chat_state("claude-chat:ccc", before=2).messages == 2


def test_text_window() -> None:
    text = "".join(f"line {i:04d}\n" for i in range(1000))  # 10 characters per line
    first, nxt = text_window(text, 0, 95)
    assert first == text[:90] and nxt == 90  # ends after the last full line
    assert text_window(text, 9990, 95) == (text[9990:], None)
    assert text_window(text, 20000, 95) == ("", None)
    one_line = "x" * 300
    assert text_window(one_line, 0, 100) == ("x" * 100, 100)  # no line break: hard cut
    pieces, offset = [], 0
    while True:
        piece, nxt = text_window(text, offset, 1000)
        pieces.append(piece)
        if nxt is None:
            break
        offset = nxt
    assert "".join(pieces) == text


async def test_writes_from_imported_chats_default_to_reviewable_drafts(
    store: WikiStore, agent: Actor
) -> None:
    portal = Actor("portal", Trust.AGENT)
    await store.import_chats([ChatSource("claude-chat:ddd", "Chat", "## User\n\nHi")], portal)
    notes = await store.ingest_source("Notes", "text", None, "agent", agent)
    chat = "raw/chats/claude/ddd"

    def fm(title: str, *sources: str, **extra: object) -> dict[str, object]:
        return {
            "type": "concept",
            "title": title,
            "description": "d.",
            "sources": list(sources),
            **extra,
        }

    def front(did: str) -> dict[str, object]:
        return store.read(did)[1].frontmatter or {}

    await store.write_concept("wiki/concepts/a", fm("A", chat), "x", "from a chat", agent)
    assert front("wiki/concepts/a")["status"] == "draft"
    assert front("wiki/concepts/a")["tags"] == ["from-chat-import"]
    await store.write_concept(
        "wiki/concepts/b", fm("B", chat, status="stable"), "x", "confirmed", agent
    )
    assert front("wiki/concepts/b")["status"] == "stable"  # an explicit status wins
    await store.write_concept("wiki/concepts/c", fm("C", chat, notes.id), "x", "mixed", agent)
    assert front("wiki/concepts/c")["status"] == "stable"  # not only imported chats
    await store.write_concept("wiki/concepts/d", fm("D"), "x", "no sources", agent)
    assert front("wiki/concepts/d")["status"] == "stable"
    # An existing page keeps its status: never demoted, never promoted.
    await store.write_concept("wiki/concepts/b", fm("B", chat), "y", "more from a chat", agent)
    assert front("wiki/concepts/b")["status"] == "stable"
    await store.write_concept("wiki/concepts/a", fm("A", chat), "y", "more from a chat", agent)
    assert front("wiki/concepts/a")["status"] == "draft"
    assert [r.id for r in store.index.list_docs(tag="from-chat-import")] == ["wiki/concepts/a"]


async def test_rerun_lists_chats_filed_before_the_playbook(store: WikiStore, agent: Actor) -> None:
    assert store.playbook_at is not None  # seeded with the playbook: nothing filed before
    portal = Actor("portal", Trust.AGENT)
    await store.import_chats(
        [
            ChatSource("claude-chat:old", "Old", "## User\n\nHi"),
            ChatSource("claude-chat:new", "New", "Hi"),
        ],
        portal,
    )
    # A wiki from before the playbook: a page filed from a chat, and no playbook yet.
    (store.root / "wiki/concepts/old.md").write_text(
        "---\ntype: concept\nid: wiki/concepts/old\ntitle: Old\ndescription: d.\n"
        "sources:\n- {id: old, resource: /raw/chats/claude/old.md}\n"
        "updated: '2026-01-01T00:00:00Z'\n---\nFiled long ago.\n",
        encoding="utf-8",
    )
    agents = store.root / "AGENTS.md"
    section = markdown_section(agents.read_text(encoding="utf-8"), "Filing a conversation")
    assert section
    agents.write_text(agents.read_text(encoding="utf-8").replace(section, ""), encoding="utf-8")
    git(store, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "old")
    git(store, "-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A")
    git(store, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "old page")
    await store.startup(SEED_DIR)  # the playbook arrives now
    assert store.playbook_at is not None and store.playbook_at > "2026-01-01T00:00:00Z"

    assert [s["id"] for s in store.unprocessed_sources()["sources"]] == ["raw/chats/claude/new"]
    rerun = store.unprocessed_sources(rerun=True)
    assert rerun["playbook_since"] == store.playbook_at and rerun["total"] == 2
    old = next(s for s in rerun["sources"] if s["id"] == "raw/chats/claude/old")
    assert old["filed_at"] == "2026-01-01T00:00:00Z"
    assert "filed_at" not in next(s for s in rerun["sources"] if s["id"] != old["id"])

    fm = {
        "type": "concept",
        "title": "Old",
        "description": "d.",
        "status": "draft",
        "tags": ["from-chat-import"],
        "sources": ["raw/chats/claude/old"],
    }
    await store.write_concept("wiki/concepts/old", fm, "Checked.", "re-processed", agent)
    assert [s["id"] for s in store.unprocessed_sources(rerun=True)["sources"]] == [
        "raw/chats/claude/new"
    ]


async def test_file_conversation_in_one_commit(store: WikiStore, agent: Actor) -> None:
    portal = Actor("portal", Trust.AGENT)
    await store.import_chats([ChatSource("claude-chat:fff", "Shed", "## User\n\nHi")], portal)
    await store.write_concept(
        "wiki/owner/profile",
        {"type": "owner", "title": "Owner", "description": "Me.", "status": "stable"},
        "# Owner\n\n## Preferences\n\n- Likes cedar",
        "set up",
        agent,
    )
    head = store.repo.head()
    src = "raw/chats/claude/fff"
    pages = [
        FilingPage(
            "wiki/projects/garden-shed",
            (
                FilingSection("Timeline", ("- 2026-01: Budget of €800 [^fff]",)),
                FilingSection("Sources", (f"[^fff]: [Shed](/{src}.md)",)),
            ),
            type="project",
            title="Garden shed",
            description="Building a shed in the garden.",
            related=("wiki/organizations/cedar-co",),
        ),
        FilingPage(
            "wiki/organizations/cedar-co",
            (FilingSection("Facts", ("- Sells shed kits [^fff]",)),),
            type="organization",
            title="Cedar Co",
            description="A shed kit seller.",
        ),
        FilingPage(
            "wiki/owner/profile",
            (
                FilingSection(
                    "Preferences", ("- likes   CEDAR", "- Prefers kits over plans [^fff]")
                ),
            ),
        ),
    ]
    res = await store.file_conversation([src], pages, "Filed a chat", portal)
    assert sorted(res.created) == ["wiki/organizations/cedar-co", "wiki/projects/garden-shed"]
    assert res.updated == ["wiki/owner/profile"] and res.commit
    assert git(store, "rev-list", "--count", f"{head}..HEAD").strip() == "1"

    _, shed, _ = store.read("wiki/projects/garden-shed")
    fm = shed.frontmatter or {}
    assert fm["status"] == "draft" and "from-chat-import" in fm["tags"]
    assert fm["related"] == ["wiki/organizations/cedar-co"] and fm["trust"] == "agent"
    assert fm["sources"] == [{"id": "fff", "resource": f"/{src}.md"}]
    assert "## Timeline\n\n- 2026-01: Budget of €800 [^fff]" in shed.body
    _, profile, _ = store.read("wiki/owner/profile")
    pfm = profile.frontmatter or {}
    assert pfm["status"] == "stable" and "from-chat-import" in pfm["tags"]  # kept, flagged
    assert profile.body.count("- Likes cedar") == 1 and "likes   CEDAR" not in profile.body
    assert "- Prefers kits over plans [^fff]" in profile.body
    assert store.unprocessed_sources()["total"] == 0
    assert "wiki/projects/garden-shed" in [r.id for r in store.index.backlinks(src)]

    again = await store.file_conversation([src], pages[2:], "Filed a chat again", portal)
    assert again.unchanged == ["wiki/owner/profile"] and again.commit is None


async def test_file_conversation_skip_and_errors(store: WikiStore, agent: Actor) -> None:
    portal = Actor("portal", Trust.AGENT)
    await store.import_chats(
        [ChatSource("claude-chat:s1", "Hello", "Hi"), ChatSource("claude-chat:s2", "Yo", "Hi")],
        portal,
    )
    await store.file_conversation(
        ["raw/chats/claude/s1"], [], "Nothing here", portal, skip="small talk"
    )
    await store.file_conversation(
        ["raw/chats/claude/s2"], [], "Nothing here", portal, skip="a greeting"
    )
    _, skipped, _ = store.read("wiki/references/skipped-conversations")
    assert "- [Hello](/raw/chats/claude/s1.md): small talk" in skipped.body
    assert "- [Yo](/raw/chats/claude/s2.md): a greeting" in skipped.body
    assert (skipped.frontmatter or {})["status"] == "stable"
    assert "from-chat-import" not in (skipped.frontmatter or {}).get("tags", [])
    assert store.unprocessed_sources()["total"] == 0

    head = store.repo.head()
    good = FilingPage("wiki/concepts/fine", (), type="concept", title="Fine", description="Ok.")
    bad = [
        [good, FilingPage("wiki/people/x", (), type="nope", title="X", description="x")],
        [good, FilingPage("wiki/concepts/y", ())],  # new without type/title
        [
            FilingPage(
                "wiki/concepts/z",
                (),
                type="concept",
                title="Z",
                description="z",
                related=("wiki/people/ghost",),
            )
        ],
        [good, good],
        [FilingPage("raw/chats/claude/s1", (FilingSection("Notes", ("- x",)),))],
    ]
    for pages in bad:
        with pytest.raises(WikiError):
            await store.file_conversation(["raw/chats/claude/s1"], pages, "Bad filing", portal)
    with pytest.raises(WikiError, match="not a raw source"):
        await store.file_conversation(["wiki/concepts/company-brain"], [good], "Bad source", portal)
    assert store.repo.head() == head and not store.exists(PurePosixPath("wiki/concepts/fine.md"))


async def test_filing_again_replaces_only_unreviewed_lines(store: WikiStore, agent: Actor) -> None:
    portal = Actor("portal", Trust.AGENT)
    await store.import_chats([ChatSource("claude-chat:rrr", "Chat", "## User\n\nHi")], portal)
    src = "raw/chats/claude/rrr"

    def page(path: str, line: str, **new: str) -> FilingPage:
        return FilingPage(path, (FilingSection("Facts", (line,)),), **new)

    new = {"type": "concept", "description": "d."}
    await store.file_conversation(
        [src],
        [
            page("wiki/concepts/draft", "- 2026-01: Old wording [^rrr]", title="Draft", **new),
            page("wiki/concepts/done", "- 2026-01: Reviewed fact [^rrr]", title="Done", **new),
        ],
        "first run",
        portal,
    )
    # The owner reviews "done": stable, without the review tag.
    await store.write_concept(
        "wiki/concepts/done",
        {
            "type": "concept",
            "title": "Done",
            "description": "d.",
            "status": "stable",
            "sources": [src],
        },
        store.read("wiki/concepts/done")[1].body,
        "reviewed",
        agent,
    )
    assert [s["id"] for s in store.unprocessed_sources(everything=True)["sources"]] == [src]
    head = store.repo.head()
    res = await store.file_conversation(
        [src],
        [page("wiki/concepts/draft", "- 2026-01: New wording [^rrr]")],
        "again",
        portal,
        replace=True,
    )
    assert git(store, "rev-list", "--count", f"{head}..HEAD").strip() == "1"
    assert sorted(res.updated) == ["wiki/concepts/done", "wiki/concepts/draft"]
    draft = store.read("wiki/concepts/draft")[1]
    assert "New wording" in draft.body and "Old wording" not in draft.body
    done = store.read("wiki/concepts/done")[1]
    assert "Reviewed fact [^rrr]" in done.body  # reviewed: never edited
    assert "from-chat-import" in (done.frontmatter or {})["tags"]  # but flagged again
    assert (done.frontmatter or {})["status"] == "stable"

    # Skipped before, filed now: the skip line goes.
    await store.import_chats([ChatSource("claude-chat:sss", "Hey", "Hi")], portal)
    await store.file_conversation(["raw/chats/claude/sss"], [], "skip", portal, skip="small talk")
    await store.file_conversation(
        ["raw/chats/claude/sss"],
        [page("wiki/concepts/draft", "- 2026-01: From hey [^sss]")],
        "again",
        portal,
        replace=True,
    )
    assert (
        "raw/chats/claude/sss.md" not in store.read("wiki/references/skipped-conversations")[1].body
    )
