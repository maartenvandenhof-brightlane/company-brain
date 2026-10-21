"""Observation and pattern pages in the store: provenance, privacy, idempotence, the patterns
pass's commits and the owner's feedback across regenerations."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

import pytest

from tests.conftest import SEED_DIR
from wiki_mcp.config import Trust
from wiki_mcp.dream.induction import existing_inferences
from wiki_mcp.dream.local import LocalReader
from wiki_mcp.dream.pages import reviewed_pages
from wiki_mcp.wiki.actor import PRIVATE_SCOPE, Actor
from wiki_mcp.wiki.lint import lint_bundle
from wiki_mcp.wiki.store import PRIVATE_TRAILER, ChatSource, WikiError, WikiStore
from wiki_mcp.wiki.types import TypeRegistry

PORTAL = Actor("portal", Trust.AGENT, frozenset({"wiki:read", "wiki:write", PRIVATE_SCOPE}))
OWNER = Actor("portal", Trust.OWNER, frozenset({"wiki:read", "wiki:write"}), human="owner")
PATTERN = "wiki/inferred/patterns/delays-releases"


def chat(key: str, title: str, *messages: tuple[str, str], day: int = 10) -> ChatSource:
    body = (
        f"# {title}\n\n- Source: Claude.ai conversation\n- Started: 2026-03-{day:02d} 09:00 UTC\n\n"
    )
    body += "\n".join(
        f"## {role} · 2026-03-{day:02d} 09:0{i} UTC\n\n{text}\n"
        for i, (role, text) in enumerate(messages)
    )
    return ChatSource(key, title, body, messages=len(messages))


DIVORCE = chat(
    "claude-chat:abc",
    "Secret divorce talk",
    ("User", "I postponed telling my sister about the divorce again."),
    ("Assistant", "You always avoid hard conversations."),
    ("User", "Maybe. I felt ashamed when she found out from mum."),
)


def observation(msgs: list[int], quote: str, summary: str, **over: Any) -> dict[str, Any]:
    return {
        "kind": "action",
        "basis": "reported",
        "text": "The owner postponed a difficult talk.",
        "msgs": msgs,
        "quote": quote,
        "event": {"summary": summary, "when": "unknown", "key": ""},
        "span": "state",
        "context": "family",
        "valence": "difficulty",
    } | over


PAYLOAD = {
    "classification": {"types": ["relationships"], "contexts": ["family"]},
    "observations": [
        observation([1], "postponed telling my sister", "Postponed telling sister about divorce"),
        observation([2], "You always avoid hard conversations", "Avoids hard conversations"),
    ],
}


async def _observed(store: WikiStore) -> dict[str, Any]:
    await store.import_chats([DIVORCE], PORTAL)
    return await store.write_observations("raw/chats/claude/abc", "observe-1", PAYLOAD, PORTAL)


def _errors(store: WikiStore, registry: TypeRegistry, prefix: str) -> list[str]:
    report = lint_bundle(list(store.all_markdown()), registry)
    return [
        f"{i.code}: {i.message}"
        for i in report.issues
        if i.severity == "error" and i.path.startswith(prefix)
    ]


async def test_observations_keep_provenance_and_stay_private(
    store: WikiStore, agent: Actor, registry: TypeRegistry
) -> None:
    await store.import_chats([DIVORCE], PORTAL)
    raw = (store.root / "raw/chats/claude/abc.md").read_bytes()

    result = await store.write_observations("raw/chats/claude/abc", "observe-1", PAYLOAD, PORTAL)

    assert result["status"] == "committed" and result["observations"] == 1
    assert result["dropped"] == ["observation 2: cites a message that is not the owner's"]
    pid = "wiki/observations/claude-abc"
    _, doc, _ = store.read(pid)
    fm = doc.frontmatter or {}
    assert fm["type"] == "observation" and fm["private"] is True and fm["status"] == "draft"
    assert fm["source"] == "raw/chats/claude/abc" and fm["conversation"] == "claude-chat:abc"
    assert fm["extractor"] == "observe-1" and fm["messages_from"] == "2026-03-10T09:00:00Z"
    [kept] = fm["observations"]
    assert kept["msgs"] == [1] and kept["quote"] == "postponed telling my sister"
    assert kept["message_at"] == "2026-03-10T09:00:00Z"
    assert "Secret divorce" not in doc.body and "avoid hard" not in doc.body
    # the raw transcript is never touched
    assert (store.root / "raw/chats/claude/abc.md").read_bytes() == raw
    # a neutral, private commit that apps without wiki:private never see
    head = store.recent_changes(None, 1)[0]
    assert head.subject == "Record observations of an imported chat"
    assert PRIVATE_TRAILER in head.body
    for secret in ("sister", "divorce", "Secret"):
        assert secret not in head.subject and secret not in head.body
    assert head.sha not in [c.sha for c in store.recent_changes(None, 50, private=False)]
    log = store.read_text_file("log.md")
    assert "a private page" in log and "sister" not in log and "claude-abc" not in log
    with pytest.raises(WikiError, match="no page at"):
        store.read(pid, agent)
    assert pid not in [h.row.id for h in store.index.search("postponed sister", private=False)]
    assert pid in [h.row.id for h in store.index.search("postponed sister", private=True)]
    # an observation page never marks a chat processed
    assert store.unprocessed_sources()["total"] == 1
    assert _errors(store, registry, "wiki/observations") == []


async def test_observing_twice_writes_nothing_new(store: WikiStore) -> None:
    await _observed(store)
    head = store.recent_changes(None, 1)[0].sha
    again = await store.write_observations("raw/chats/claude/abc", "observe-1", PAYLOAD, PORTAL)
    assert again == {"status": "exists", "id": "wiki/observations/claude-abc"}
    assert store.recent_changes(None, 1)[0].sha == head
    assert store.observations_pending()["total"] == 0
    outdated = store.observations_pending(outdated="observe-2")
    assert outdated["total"] == 1 and outdated["sources"][0]["observed"] is True
    created = (store.read("wiki/observations/claude-abc")[1].frontmatter or {})["created"]
    newer = await store.write_observations(
        "raw/chats/claude/abc-part-1", "observe-2", {"observations": []}, PORTAL
    )
    assert newer["status"] == "committed" and newer["observations"] == 0
    fm = store.read("wiki/observations/claude-abc")[1].frontmatter or {}
    assert fm["extractor"] == "observe-2" and fm["created"] == created
    assert store.observations_pending(outdated="observe-2")["total"] == 0


async def test_observations_need_a_chat_and_a_version(store: WikiStore) -> None:
    await store.import_chats([DIVORCE], PORTAL)
    with pytest.raises(WikiError, match="extractor"):
        await store.write_observations("raw/chats/claude/abc", "Observe 1!", PAYLOAD, PORTAL)
    with pytest.raises(WikiError, match="not an imported chat"):
        await store.write_observations("wiki/owner/profile", "observe-1", PAYLOAD, PORTAL)
    with pytest.raises(WikiError, match="no imported chat"):
        await store.write_observations("raw/chats/claude/nope", "observe-1", PAYLOAD, PORTAL)


# --- patterns -------------------------------------------------------------------------------


def _chat(n: int, day: int) -> ChatSource:
    return chat(
        f"claude-chat:c{n}",
        f"Chat {n}",
        ("User", f"I delayed release {n} because the tests were red."),
        ("Assistant", "Sounds careful."),
        day=day,
    )


EVENTS = (
    "Delayed the spring release",
    "Held back the mobile app",
    "Pushed the billing launch",
    "Postponed the portal rollout",
)


async def _evidence(store: WikiStore) -> None:
    """Four chats, each one release delayed: four events."""
    days = ((1, 1), (2, 5), (3, 20), (4, 28))
    await store.import_chats([_chat(n, day) for n, day in days], PORTAL)
    for n in range(1, 5):
        payload = {
            "observations": [
                observation([1], f"I delayed release {n}", EVENTS[n - 1], context="work")
            ]
        }
        await store.write_observations(f"raw/chats/claude/c{n}", "observe-1", payload, PORTAL)


def _op(**over: Any) -> dict[str, Any]:
    fm = {
        "title": "Delays releases",
        "hypothesis": "In work, when tests are red before a release, the owner tends to delay it.",
        "contexts": ["work"],
        "span": "tendency",
        "supports": [f"wiki/observations/claude-c{n}#o1" for n in range(1, 5)],
        "counterevidence": [],
        "alternatives": ["That project had flaky tests."],
        "confidence": "high",
        "reasoning": "Four releases.",
        "status": "stable",
    } | over
    return {"op": "write_pattern", "page": PATTERN, "frontmatter": fm}


async def test_the_patterns_pass_commits_capped_private_pages(
    store: WikiStore, agent: Actor, registry: TypeRegistry
) -> None:
    await _evidence(store)
    sha, pages = await store.dream_commit(5, "patterns", "Reassessed patterns", [_op()])
    assert pages == [PATTERN] and sha
    _, doc, _ = store.read(PATTERN)
    fm = doc.frontmatter or {}
    assert fm["kind"] == "pattern" and fm["private"] is True
    assert fm["about"] == ["wiki/owner/personal"]
    # four events in one context over 27 days: medium at most
    assert fm["independent_events"] == 4 and fm["confidence"] == "medium"
    assert fm["span"] == "tendency"
    assert fm["supports"][0]["quote"] == "I delayed release 1"
    assert fm["supports"][0]["source"] == "raw/chats/claude/c1"
    assert fm["supports"][0]["msg"] == 1 and fm["supports"][0]["date"] == "2026-03-01"
    assert doc.body.split("\n")[2].startswith("**Inferred, not stated.**")
    head = store.recent_changes(None, 1)[0]
    assert head.subject == "Dream 5: patterns" and "Dream: 5" in head.body
    assert PRIVATE_TRAILER in head.body
    assert head.sha not in [c.sha for c in store.recent_changes(None, 50, private=False)]
    with pytest.raises(WikiError, match="no page at"):
        store.read(PATTERN, agent)
    hits = [h.row.id for h in store.index.search("tends to delay", private=False, limit=50)]
    assert PATTERN not in hits and not [h for h in hits if h.startswith("wiki/observations")]
    assert _errors(store, registry, "wiki/inferred") == []
    assert _errors(store, registry, "wiki/observations") == []


@pytest.mark.parametrize(
    ("supports", "why"),
    [
        (["wiki/inferred/works-late", "wiki/observations/claude-c1#o1"], "inference"),
        (["wiki/owner/profile", "wiki/observations/claude-c1#o1"], "not an existing observation"),
        (["wiki/observations/claude-c1#o9", "wiki/observations/claude-c2#o1"], "not an existing"),
    ],
)
async def test_only_observations_are_evidence(
    store: WikiStore, supports: list[str], why: str
) -> None:
    await _evidence(store)
    with pytest.raises(WikiError, match=why):
        await store.dream_commit(5, "patterns", "Reassessed patterns", [_op(supports=supports)])
    assert not store.exists(PurePosixPath(PATTERN + ".md"))


async def test_owner_feedback_survives_regeneration(store: WikiStore, agent: Actor) -> None:
    await _evidence(store)
    await store.dream_commit(5, "patterns", "Reassessed patterns", [_op()])
    with pytest.raises(WikiError, match="only the owner"):
        await store.pattern_feedback(PATTERN, "dispute", "", agent)
    result = await store.pattern_feedback(PATTERN, "dispute", "Only with that team.", OWNER)
    assert result.status == "committed"
    head = store.recent_changes(None, 1)[0]
    assert head.subject == "Record owner feedback on a pattern" and "team" not in head.body

    await store.dream_commit(6, "patterns", "Reassessed patterns", [_op()])
    fm = store.read(PATTERN)[1].frontmatter or {}
    assert fm["status"] == "disputed" and fm["confidence"] == "low"
    assert [f["verdict"] for f in fm["feedback"]] == ["dispute"]
    with pytest.raises(WikiError, match="never retired"):
        await store.dream_commit(7, "patterns", "Retired", [_op(status="deprecated")])
    assert store.exists(PurePosixPath(PATTERN + ".md"))

    wording = "With that team, he delayed releases on red tests."
    await store.pattern_feedback(PATTERN, "correct", wording, OWNER)
    await store.dream_commit(8, "patterns", "Reassessed patterns", [_op()])
    _, doc, _ = store.read(PATTERN)
    fm = doc.frontmatter or {}
    assert fm["hypothesis"] == wording and fm["status"] == "stable"
    assert fm["model_hypothesis"].startswith("In work, when tests are red")
    assert [f["verdict"] for f in fm["feedback"]] == ["dispute", "correct"]
    assert "Only with that team." in doc.body and wording in doc.body
    history = doc.body.split("## History")[1]
    assert history.count("by dream") == 3 and history.count("owner feedback") == 2


async def test_patterns_change_only_through_write_pattern(store: WikiStore) -> None:
    await _evidence(store)
    await store.dream_commit(5, "patterns", "Reassessed patterns", [_op()])
    op = {"op": "deprecate", "page": PATTERN, "note": "Gone."}
    with pytest.raises(WikiError, match="write_pattern"):
        await store.dream_proposal(5, "induction", "Retire", "Gone", [op])
    inference = {
        "op": "write_inference",
        "page": PATTERN,
        "frontmatter": {"title": "x"},
        "body": "**Inferred, not stated.** x",
    }
    with pytest.raises(WikiError, match="write_pattern"):
        await store.dream_proposal(5, "induction", "Rewrite", "Rewrite it", [inference])


async def test_other_dream_passes_never_read_observations_or_patterns(store: WikiStore) -> None:
    await _evidence(store)
    await store.dream_commit(5, "patterns", "Reassessed patterns", [_op()])
    reader = LocalReader(store)
    assert PATTERN not in await existing_inferences(reader)
    assert not [p for p in await reviewed_pages(reader) if p.type == "observation"]


def test_every_page_type_the_guide_names_is_writable_or_read_only(registry: TypeRegistry) -> None:
    guide = (SEED_DIR / "AGENTS.md").read_text(encoding="utf-8")
    named = set(re.findall(r"type `([a-z-]+)`", guide))
    assert "inference" in named  # the Company Brain's guide has no observations section
    writable = {t.name for t in registry.writable()}
    read_only = {t.name for t in registry.read_only()}
    assert named <= writable | read_only
    assert {"inference", "observation"} <= read_only
    assert all(t.written_by for t in registry.read_only())
