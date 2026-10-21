"""Personal patterns: event clustering, span, the confidence cap, the page rules and feedback
(wiki/patterns.py), and the pass's material and checks (dream/patterns.py)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from wiki_mcp.dream import patterns as pass_
from wiki_mcp.wiki.patterns import (
    Evidence,
    PatternError,
    answer,
    build,
    clinical,
    evidence,
    pattern_ceiling,
    span_of,
    with_feedback,
)

TODAY = dt.date(2026, 9, 30)
PID = "wiki/inferred/patterns/delays-releases"


def o(
    date: str,
    summary: str,
    *,
    when: str = "unknown",
    key: str = "",
    context: str = "work",
    valence: str = "difficulty",
    basis: str = "reported",
) -> dict[str, Any]:
    return {
        "kind": "action",
        "basis": basis,
        "text": f"The owner: {summary}.",
        "msgs": [1],
        "quote": summary.lower(),
        "message_at": f"{date}T09:00:00Z",
        "event": {"summary": summary, "when": when, "key": key},
        "span": "state",
        "context": context,
        "valence": valence,
    }


def page(conv: str, *items: dict[str, Any], pid: str = "") -> tuple[str, dict[str, Any]]:
    return (
        pid or f"wiki/observations/claude-{conv}",
        {
            "conversation": f"claude-chat:{conv}",
            "source": f"raw/chats/claude/{conv}",
            "observations": [{"id": f"o{i}", **item} for i, item in enumerate(items, 1)],
        },
    )


def ref(conv: str, n: int = 1) -> str:
    return f"wiki/observations/claude-{conv}#o{n}"


def asked(*supports: str, **over: Any) -> dict[str, Any]:
    return {
        "title": "Delays releases",
        "hypothesis": "In work, when tests fail before a release, the owner tends to delay it.",
        "contexts": ["work"],
        "span": "tendency",
        "supports": list(supports),
        "counterevidence": [],
        "alternatives": ["Failing tests may simply have been common in that project."],
        "confidence": "high",
        "reasoning": "Several releases were postponed.",
    } | over


# --- events -------------------------------------------------------------------------------


def test_the_same_event_told_in_two_chats_counts_once() -> None:
    ev = evidence(
        [
            page("a", o("2026-03-02", "Release postponed for failing tests", when="2026-03")),
            page("b", o("2026-03-20", "Postponed the release: failing tests", when="2026-03-14")),
            page("c", o("2026-03-21", "Release postponed for failing tests", when="2026-01")),
            page("d", o("2026-03-22", "Went hiking with friends")),
        ]
    )
    a, b, c, d = (ev.clusters[ref(x)] for x in "abcd")
    assert a == b  # compatible dates, the same words
    assert c != a  # the same words, but another month
    assert d not in (a, c)
    assert ev.coverage["events"] == 3 and ev.coverage["observations"] == 4


def test_continuations_share_their_conversations_event_keys() -> None:
    ev = evidence(
        [
            page("x", o("2026-03-02", "Argument with sister", key="argument")),
            page(
                "x",
                o("2026-04-02", "Talked it over later on", key="argument"),
                pid="wiki/observations/claude-x-after-8",
            ),
            page("y", o("2026-04-03", "Talked it over later on", key="argument", when="2026-05")),
        ]
    )
    first, later = ev.clusters[ref("x")], ev.clusters["wiki/observations/claude-x-after-8#o1"]
    assert first == later  # the same conversation, the same key
    assert ev.clusters[ref("y")] == first  # same summary words, a compatible (unknown) date


def test_coverage_counts_contexts_quarters_and_skew() -> None:
    troubles = (
        "Missed the deadline",
        "Clashed with manager",
        "Server outage at night",
        "Forgot an invoice",
        "Late for standup",
        "Budget overrun",
    )
    ev = evidence(
        [page(f"c{i}", o(f"2026-0{1 + i % 3}-10", s)) for i, s in enumerate(troubles)]
        + [page("h", o("2026-05-01", "Proud of the garden", context="hobbies", valence="strength"))]
    )
    cov = ev.coverage
    assert cov["contexts"]["work"] == {"observations": 6, "events": 6}
    assert set(cov["quarters"]) == {"2026-Q1", "2026-Q2"}
    assert cov["valence"]["strength"]["events"] == 1
    assert any("difficulties" in s for s in cov["skew"])
    assert any("% of events are about work" in s for s in cov["skew"])


# --- span and confidence --------------------------------------------------------------------


def test_a_tendency_needs_two_events_at_least_two_weeks_apart() -> None:
    assert span_of("tendency", ["2026-03-01"]) == "state"
    assert span_of("tendency", ["2026-03-01", "2026-03-04"]) == "state"
    assert span_of("tendency", ["2026-03-01", "2026-03-20"]) == "tendency"
    assert span_of("state", ["2026-03-01", "2026-06-01"]) == "state"


@pytest.mark.parametrize(
    ("args", "ceiling"),
    [
        ((4, 3, 2, 120, 0, False), "high"),
        ((4, 3, 2, 120, 1, False), "medium"),  # counterevidence
        ((4, 3, 1, 120, 0, False), "medium"),  # one context
        ((4, 3, 2, 60, 0, False), "medium"),  # too short a period
        ((3, 2, 1, 10, 0, False), "medium"),
        ((3, 1, 1, 10, 0, False), "low"),  # one chat
        ((2, 2, 2, 200, 0, False), "low"),
        ((9, 9, 5, 900, 0, True), "low"),  # disputed
    ],
)
def test_confidence_ceiling(args: tuple[int, int, int, int, int, bool], ceiling: str) -> None:
    assert pattern_ceiling(*args)[0] == ceiling


def _wide() -> Evidence:
    """Four events in two contexts over half a year, and a counterexample."""
    return evidence(
        [
            page("a", o("2026-01-05", "Release postponed for tests", key="r1")),
            page("b", o("2026-02-10", "Slipped the data migration", key="m")),
            page("c", o("2026-04-15", "Delayed the garden shed build", context="hobbies")),
            page("d", o("2026-07-01", "Held back the website launch", key="w")),
            page("e", o("2026-07-02", "Shipped the invoice tool early", valence="strength")),
            page("f", o("2026-07-03", "Release postponed for tests", key="r1")),
        ]
    )


def test_confidence_is_capped_by_independent_events_not_mentions() -> None:
    ev = _wide()
    fm, _ = build(PID, asked(ref("a"), ref("f")), None, [], ev, 7, TODAY)
    # a and f: one event (another chat, same summary, dates unknown), told twice
    assert fm["independent_events"] == 1 and fm["confidence"] == "low"
    assert fm["span"] == "state"
    fm, _ = build(PID, asked(*(ref(x) for x in "abcd")), None, [], ev, 7, TODAY)
    assert fm["independent_events"] == 4 and fm["confidence"] == "high"
    assert fm["span"] == "tendency" and fm["span_days"] >= 90
    assert "4 independent event(s) from 4 chat(s) in 2 context(s)" in fm["confidence_rationale"]


def test_counterevidence_is_kept_shown_and_lowers_the_ceiling() -> None:
    ev = _wide()
    fm, body = build(
        PID, asked(*(ref(x) for x in "abcd"), counterevidence=[ref("e")]), None, [], ev, 7, TODAY
    )
    assert fm["confidence"] == "medium"
    assert [c["page"] for c in fm["counterevidence"]] == ["wiki/observations/claude-e"]
    counter = body.split("## Counterevidence")[1].split("##")[0]
    assert '"shipped the invoice tool early"' in counter


@pytest.mark.parametrize(
    ("over", "why"),
    [
        ({"supports": ["wiki/inferred/patterns/other#o1", ref("a")]}, "inference"),
        ({"supports": ["wiki/inferred/works-late", ref("a")]}, "inference"),
        ({"supports": [ref("a"), ref("zz")]}, "not an existing observation"),
        ({"supports": [ref("a"), "wiki/people/jane"]}, "not an existing observation"),
        ({"supports": [ref("a")]}, "at least 2"),
        ({"alternatives": []}, "alternative"),
        ({"hypothesis": "The owner has ADHD at work."}, "diagnoses"),
        ({"title": "Narcissistic streak"}, "diagnoses"),
    ],
)
def test_pages_are_refused_without_real_evidence(over: dict[str, Any], why: str) -> None:
    fm_in = asked(ref("a"), ref("b")) | over
    with pytest.raises(PatternError, match=why):
        build(PID, fm_in, None, [], _wide(), 7, TODAY)


def test_clinical_terms() -> None:
    assert clinical("tends to feel depressed on Mondays") == "depress"
    assert clinical("In work, when deadlines slip, he delays") is None


# --- feedback -------------------------------------------------------------------------------


def test_feedback_survives_regeneration() -> None:
    ev = _wide()
    fm, _ = build(PID, asked(*(ref(x) for x in "abcd")), None, [], ev, 7, TODAY)
    disputed, _ = with_feedback(fm, "dispute", "Only at that job.", "2026-09-30T10:00:00Z", [])
    assert disputed["status"] == "disputed" and disputed["confidence"] == "low"
    again, body = build(PID, asked(*(ref(x) for x in "abcd")), disputed, [], ev, 8, TODAY)
    assert again["status"] == "disputed" and again["confidence"] == "low"
    assert again["feedback"] == disputed["feedback"]
    assert "- 2026-09-30: dispute: Only at that job." in body
    with pytest.raises(PatternError, match="never retired"):
        build(PID, {"status": "deprecated"}, again, [], ev, 9, TODAY)

    wording = "At that job, he delayed releases when tests failed."
    corrected, _ = with_feedback(again, "correct", wording, "2026-10-01T10:00:00Z", [])
    assert corrected["status"] == "stable"  # the owner changed his verdict
    regenerated, body = build(PID, asked(*(ref(x) for x in "abcd")), corrected, [], ev, 10, TODAY)
    assert regenerated["hypothesis"] == wording
    assert regenerated["model_hypothesis"].startswith("In work, when tests fail")
    assert [f["verdict"] for f in regenerated["feedback"]] == ["dispute", "correct"]
    assert wording in body and regenerated["confidence"] == "high"


def test_feedback_needs_a_known_verdict_and_a_correction_its_wording() -> None:
    fm, _ = build(PID, asked(ref("a"), ref("b")), None, [], _wide(), 7, TODAY)
    with pytest.raises(PatternError):
        with_feedback(fm, "love", "", "2026-09-30T10:00:00Z", [])
    with pytest.raises(PatternError, match="corrected wording"):
        with_feedback(fm, "correct", " ", "2026-09-30T10:00:00Z", [])


def test_retiring_a_pattern_without_feedback() -> None:
    ev = _wide()
    fm, _ = build(PID, asked(ref("a"), ref("b")), None, [], ev, 7, TODAY)
    retired, body = build(PID, {"status": "deprecated", "reasoning": "gone"}, fm, [], ev, 8, TODAY)
    assert retired["status"] == "deprecated" and "retired by dream 8: gone" in body


# --- the pass -------------------------------------------------------------------------------


def test_the_sample_is_balanced_and_each_event_counts_once() -> None:
    pages = [
        page(f"w{i}", o(f"2026-03-{1 + i:02d}", f"Work trouble {i}", key="t")) for i in range(9)
    ]
    pages += [page("s", o("2026-01-01", "Proud of fixing the roof", valence="strength"))]
    pages += [page("same", *[o("2026-02-01", "Same event", key="k")] * 5)]
    ev = evidence(pages)
    sampled = pass_.sample(ev)
    assert ref("s") in [x.ref for x in sampled[:3]]  # strengths are not crowded out
    same = [x for x in sampled if x.page.endswith("claude-same")]
    assert len(same) == pass_.PER_EVENT
    found = pass_.material(ev, [])
    assert "<coverage>" in found.text and "None yet." in found.text
    assert found.refs == [x.ref for x in sampled]


def test_review_keeps_only_observation_evidence() -> None:
    ev = _wide()
    existing = [
        ("wiki/inferred/patterns/old", {"hypothesis": "Old.", "feedback": [{"verdict": "agree"}]}),
        ("wiki/inferred/patterns/plain", {"hypothesis": "Plain.", "feedback": []}),
    ]
    found = pass_.material(ev, existing)
    good = asked(ref("a"), ref("b"), ref("zz")) | {"action": "create"}
    answer_ = pass_.Answer(
        [
            good,
            asked(ref("a"), "wiki/inferred/patterns/old") | {"action": "create"},
            asked(ref("a"), ref("b"), hypothesis="He is an introvert at work."),
            asked(ref("a")) | {"action": "create"},
            {"action": "retire", "id": "wiki/inferred/patterns/old"},
            {"action": "retire", "id": "wiki/inferred/patterns/plain"},
            {"action": "update", "id": "wiki/inferred/patterns/nope"},
        ]
    )
    plan = pass_.review(answer_, found)
    assert [op["page"] for op in plan.ops] == [
        "wiki/inferred/patterns/delays-releases",
        "wiki/inferred/patterns/plain",
    ]
    assert plan.ops[0]["frontmatter"]["supports"] == [ref("a"), ref("b")]
    assert plan.ops[1]["frontmatter"] == {"status": "deprecated", "reasoning": ""}
    assert any("not in the material" in f for f in plan.flags)
    reasons = " | ".join(plan.dropped)
    for why in ("inference", "diagnosis", "fewer than 2", "owner feedback", "existing pattern"):
        assert why in reasons
    assert "Delays" not in reasons and "tests fail" not in reasons  # never the hypothesis


def test_the_answer_shows_caveats_both_valences_and_orders_by_events() -> None:
    ev = _wide()
    small, _ = build(PID + "-2", asked(ref("a"), ref("b")), None, [], ev, 7, TODAY)
    big, _ = build(PID, asked(*(ref(x) for x in "abcd")), None, [], ev, 7, TODAY)
    text = answer([(PID + "-2", small), (PID, big)], None, 6)
    assert "not facts, and never diagnoses" in text
    assert "## Strengths" in text and "None found yet (unknown, not absent)." in text
    difficulties = text.split("## Difficulties")[1]
    assert difficulties.index(PID + ".") < difficulties.index(PID + "-2.")
    assert "Counterevidence: none found (which is not proof)" in text
    assert "No personal patterns in the context family" in answer([(PID, big)], "family", 6)
