"""Observations: the transcript numbering and the provenance checks (wiki/observations.py)."""

from __future__ import annotations

from typing import Any

import pytest

from wiki_mcp.wiki.observations import (
    ObservationError,
    base_source,
    check,
    page_id,
    parse,
    quoted_in,
    render,
)


def transcript(*messages: tuple[str, str], start: int = 0, day: int = 10) -> str:
    """A transcript body as the portal renders it (a continuation from message `start`)."""
    head = "> [!NOTE]\n> Immutable raw source.\n\n# Chat\n\n- Source: Claude.ai conversation\n"
    if start:
        head += f"- Continuation: messages {start} to 99; the first {start - 1} are elsewhere\n"
    return (
        head
        + "\n"
        + "\n".join(
            f"## {role} · 2026-03-{day + i:02d} 09:0{i % 10} UTC\n\n{text}\n"
            for i, (role, text) in enumerate(messages)
        )
    )


CHAT = transcript(
    ("User", "I postponed the release again because the tests kept failing."),
    ("Assistant", "You could set a fixed release day and ship what is ready."),
    ("User", "Yes, let's do that: a fixed release day every other Friday."),
    ("Assistant", "Great. You always seem to overwork before deadlines."),
    ("User", "Should I tell my manager? Would that look bad?"),
    ("User", "I'm honestly **proud** of how the team handled it."),
)


def obs(**over: Any) -> dict[str, Any]:
    base = {
        "kind": "action",
        "basis": "reported",
        "text": "The owner postponed a release because tests failed.",
        "msgs": [1],
        "quote": "I postponed the release again",
        "event": {"summary": "Release postponed for failing tests", "when": "2026-03", "key": "r"},
        "span": "state",
        "context": "work",
        "valence": "difficulty",
    }
    return base | over


def test_numbering_runs_on_across_parts_and_starts_at_a_continuation() -> None:
    parts = parse([CHAT, transcript(("User", "More."), ("Assistant", "Ok."))])
    assert [m.number for m in parts] == list(range(1, 9))
    assert parts[6].role == "User" and parts[6].text == "More."
    assert parts[0].at == "2026-03-10T09:00:00Z"
    later = parse([transcript(("User", "Back again."), start=12)])
    assert [(m.number, m.role) for m in later] == [(12, "User")]


def test_a_kept_observation_links_the_owners_message_quote_and_date() -> None:
    out = check({"classification": {"types": ["work"]}, "observations": [obs()]}, parse([CHAT]))
    assert not out.dropped
    [kept] = out.observations
    assert kept["id"] == "o1" and kept["msgs"] == [1]
    assert kept["quote"] == "I postponed the release again"
    assert kept["message_at"] == "2026-03-10T09:00:00Z"
    assert kept["event"] == {
        "summary": "Release postponed for failing tests",
        "when": "2026-03",
        "key": "r",
    }


@pytest.mark.parametrize(
    ("over", "why"),
    [
        ({"msgs": [2]}, "not the owner's"),  # cites an assistant message
        ({"msgs": [99]}, "not the owner's"),  # no such message
        ({"msgs": []}, "not the owner's"),
        ({"msgs": [3], "quote": "overwork before deadlines"}, "quote is not"),  # the assistant's
        ({"quote": "I always postpone releases"}, "quote is not"),  # not in the message
        ({"kind": "trait"}, "known kind"),
        ({"basis": "assistant"}, "known kind"),
    ],
)
def test_assistant_claims_and_bad_provenance_are_dropped(over: dict[str, Any], why: str) -> None:
    out = check({"observations": [obs(**over)]}, parse([CHAT]))
    assert out.observations == []
    assert len(out.dropped) == 1 and why in out.dropped[0]
    assert "postponed" not in out.dropped[0]  # reports say why, never the text


def test_quotes_match_despite_spacing_case_marks_and_emphasis() -> None:
    messages = parse([CHAT])
    left, apostrophe, right, ellipsis = chr(0x201C), chr(0x2019), chr(0x201D), chr(0x2026)
    assert quoted_in(f"{left}i{apostrophe}m honestly  PROUD of how{right}", messages)
    assert quoted_in(f"... the tests kept failing {ellipsis}", messages)
    assert not quoted_in("ok", messages)  # too short to tell


def test_endorsed_needs_an_assistant_suggestion_right_before() -> None:
    accepted = obs(msgs=[3], quote="a fixed release day every other Friday", endorsed=True)
    not_after = obs(endorsed=True)
    out = check({"observations": [accepted, not_after]}, parse([CHAT]))
    assert [o["endorsed"] for o in out.observations] == [True, False]
    assert out.adjusted == ["observation 2: endorsed taken off (no assistant message before)"]


def test_questions_only_show_behaviour_in_the_chat() -> None:
    q = {"msgs": [5], "quote": "Should I tell my manager?"}
    action = obs(**q)  # "he did not tell" from a question: never
    value = obs(**q, kind="value", basis="chat")
    reassurance = obs(**q, kind="response", basis="chat", text="The owner asks for reassurance.")
    out = check({"observations": [action, value, reassurance]}, parse([CHAT]))
    assert [o["text"] for o in out.observations] == ["The owner asks for reassurance."]
    assert len(out.dropped) == 2 and all("questions only" in d for d in out.dropped)


def test_a_single_report_is_a_state_not_a_tendency() -> None:
    reported = obs(span="tendency")
    stated = obs(span="tendency", basis="statement")
    out = check({"observations": [reported, stated]}, parse([CHAT]))
    assert [o["span"] for o in out.observations] == ["state", "tendency"]


def test_emotions_are_specific_stated_or_tone_and_the_owners() -> None:
    emotions = [
        {"emotion": "Proud", "target": "the team", "kind": "stated", "msg": 6, "quote": "proud"},
        {"emotion": "anxious", "target": "manager", "kind": "tone", "msg": 5, "quote": "look bad"},
        {"emotion": "negative", "kind": "tone", "msg": 1, "quote": "postponed"},
        {"emotion": "tired", "kind": "tone", "msg": 4, "quote": "overwork"},  # assistant
        {"emotion": "sad", "kind": "felt", "msg": 1, "quote": "postponed"},
    ]
    out = check({"classification": {"emotions": emotions}}, parse([CHAT]))
    kept = out.classification["emotions"]
    assert [(e["emotion"], e["kind"]) for e in kept] == [("proud", "stated"), ("anxious", "tone")]
    assert len(out.dropped) == 3


def test_classification_vocabularies() -> None:
    raw = {
        "types": ["work", "gossip"],
        "contexts": ["work", "space"],
        "intents": ["ship it", 3],
        "sentiment": "meh",
    }
    out = check({"classification": raw}, parse([CHAT]))
    assert out.classification == {
        "types": ["work"],
        "contexts": ["work"],
        "intents": ["ship it"],
        "emotions": [],
    }
    assert check({}, []).classification["types"] == ["other"]
    with pytest.raises(ObservationError):
        check({"observations": "all of them"}, [])


def test_page_ids_per_transcript_never_collide_between_sources() -> None:
    assert page_id("raw/chats/claude/abc-part-2") == "wiki/observations/claude-abc"
    assert page_id("raw/chats/chatgpt/abc") == "wiki/observations/chatgpt-abc"
    assert page_id("raw/chats/claude/abc-after-5") == "wiki/observations/claude-abc-after-5"
    assert base_source("/raw/chats/claude/abc-part-3.md") == "raw/chats/claude/abc"
    with pytest.raises(ObservationError):
        page_id("raw/2026-01-01-notes")


def test_rendering_marks_evidence_and_inferred_emotions() -> None:
    out = check(
        {
            "classification": {
                "emotions": [{"emotion": "anxious", "kind": "tone", "msg": 5, "quote": "look bad"}]
            },
            "observations": [obs()],
        },
        parse([CHAT]),
    )
    fm = {
        "title": "Observations: Claude.ai chat of 2026-03-10",
        "source": "raw/chats/claude/abc",
        "extractor": "observe-1",
        "observed": "2026-09-30",
        "classification": out.classification,
        "observations": out.observations,
    }
    body = render(fm)
    assert "**Evidence, not facts.**" in body and "inferred from tone, not stated" in body
    assert "o1 (m1, 2026-03-10; reported action, work, difficulty, state)" in body
    empty = render({**fm, "observations": []})
    assert "Nothing personal was observed in this chat." in empty
