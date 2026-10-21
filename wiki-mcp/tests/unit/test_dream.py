"""Dream passes without a wiki (wiki_mcp.dream): hot memory's checks and trimming, the log,
the page helpers, deduction's dates and line references, inference pages, and what the
checks drop and why."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

import pytest

from wiki_mcp.dream import Unparsable, deduction, hot_memory, hygiene, induction, inference, merge
from wiki_mcp.dream.pages import Page, lang, numbered
from wiki_mcp.dream.reader import WikiReader

AT = dt.datetime(2026, 10, 2, 7, 5, tzinfo=dt.UTC)
PROFILE = Page(
    "wiki/owner/profile",
    "wiki/owner/profile.md",
    "owner",
    "Owner profile",
    {"status": "draft"},
    "# Owner profile\n\n## Goals\n\n- Ship it (set 2026-09-29)\n"
    "- Old goal (set 2026-01-01) *(superseded 2026-09-29: dropped by owner)*\n",
)
SHED = Page("wiki/projects/shed", "wiki/projects/shed.md", "project", "Shed [kit]", {}, "x")
KNOWN = {p.id: p for p in (PROFILE, SHED)}


def answer(**sections: list[dict[str, object]]) -> str:
    return json.dumps({k: sections.get(k, []) for k, _, _ in hot_memory.SECTIONS})


def line(text: str, page: str = "wiki/projects/shed") -> dict[str, object]:
    return {"text": text, "page": page, "unreviewed": False}


def test_goals_are_the_lines_not_superseded() -> None:
    assert hot_memory.active_goals(PROFILE) == ["- Ship it (set 2026-09-29)"]
    assert hot_memory.active_goals(None) == []


@pytest.mark.parametrize("bad", ["no json", "[]", "{}", '{"who": "x"}'])
def test_not_the_contract(bad: str) -> None:
    with pytest.raises(Unparsable):
        hot_memory.parse(bad, KNOWN)


def test_checks_links_limits_and_marks_unreviewed() -> None:
    briefing = hot_memory.parse(
        answer(
            who=[line("He builds things.", "wiki/owner/profile")],
            projects=[
                line("The shed."),
                line("A pattern.", "wiki/inferred/prefers-kits"),
                line("Unknown.", "wiki/projects/unknown"),
                line("x" * 301),
            ],
            people=[line(f"Person {i}.") for i in range(7)],
        ),
        KNOWN,
    )
    assert [ln.text for ln in briefing.sections["projects"]] == ["The shed."]
    assert len(briefing.sections["people"]) == 5
    assert any("rests on an inference" in d for d in briefing.dropped)
    assert any("not given" in d for d in briefing.dropped)
    assert any("over its limit of 5" in d for d in briefing.dropped)
    text = hot_memory.render(briefing, KNOWN, 7, AT, 6000)
    assert text.startswith(
        "# Hot memory\n\nGenerated 2026-10-02 07:05 UTC by dream 7. Stated facts only; "
    )
    # the draft profile's line is marked, whatever the model said; the title's brackets go
    assert "- He builds things. ([Owner profile](/wiki/owner/profile.md)) (unreviewed)" in text
    assert "- The shed. ([Shed (kit)](/wiki/projects/shed.md))\n" in text
    assert "inferred" not in text


def test_trimmed_to_the_limit_recent_changes_first() -> None:
    long = "a fairly long line about something that happened recently in the wiki"
    briefing = hot_memory.parse(
        answer(
            recent=[line(f"{long} {i}") for i in range(6)],
            people=[line(f"{long} person {i}") for i in range(5)],
            projects=[line(f"{long} project {i}") for i in range(6)],
            goals=[line("Ship it.", "wiki/owner/profile")],
        ),
        KNOWN,
    )
    full = hot_memory.render(hot_memory.parse(answer(), KNOWN), KNOWN, 1, AT, 6000)
    limit = len(full) + 900
    text = hot_memory.render(briefing, KNOWN, 1, AT, limit)
    assert len(text) <= limit
    assert "## Recent changes (last 14 days)\n\n- Nothing current." in text  # gone first
    assert "person 0" in text and "project 5" in text  # the rest fits
    assert "Ship it." in text
    tiny = hot_memory.render(briefing, KNOWN, 1, AT, len(full) + 50)
    assert len(tiny) <= len(full) + 50 and "project 0" not in tiny and "Ship it." in tiny


def test_lang_and_numbered_lines() -> None:
    assert lang("Hij wil een schuur bouwen voor de tuin en dat is het plan") == "nl"
    assert lang("He wants to build a shed for the garden and that is the plan") == "en"
    page = Page("wiki/p", "wiki/p.md", "project", "P", {}, "# P\n\n- 2026-01: x [^a]")
    block, lines = numbered(page)
    assert block.startswith('<page id="wiki/p" type="project" status="stable" lang="en">\nL1: # P')
    assert lines["L3"] == "- 2026-01: x [^a]" and block.endswith("L3: - 2026-01: x [^a]\n</page>")


@pytest.mark.parametrize(
    ("a", "b", "is_later"),
    [
        ("2026-03", "2026-01", True),
        ("2026-03-15", "2026-03-14", True),
        ("2026-04", "2026-03-31", True),
        ("2026-03-15", "2026-03", False),  # the same month at the common precision
        ("2026-03", "2026-03-15", False),
        ("2026-03", "2026-03", False),
        ("2026-01", "2026-03", False),
        (None, "2026-01", False),  # undated: never later
        ("2026-03", None, False),
    ],
)
def test_later_compares_at_the_common_precision(
    a: str | None, b: str | None, is_later: bool
) -> None:
    assert deduction.later(a, b) is is_later


BODY = (
    "# Shed\n\n## Timeline\n\n- 2026-01: A kit. [^shed]\n- 2026-03: Own design. [^nowhere]\n"
    "- [ ] Order it (as of 2026-01)\n"
    "- 2026-02: Green. [^shed] *(superseded 2026-03-01: now red)*\n"
    "  - 2026-02: Nested, with a footnote. [^shed]"
)


def _material() -> tuple[deduction.Material, deduction.Dates]:
    material = deduction.Material()
    assert material.add(Page("wiki/projects/shed", "wiki/projects/shed.md", "project", "Shed",
                             {}, BODY))  # fmt: skip
    dates = deduction.Dates(cast(WikiReader, None), {"shed": "raw/chats/claude/shed"})
    return material, dates


def test_references_resolve_to_exact_stated_lines() -> None:
    material, dates = _material()
    kit = deduction.resolve("/wiki/projects/shed.md", "L5", material, dates)
    assert kit.text == "- 2026-01: A kit. [^shed]" and kit.source == "raw/chats/claude/shed"
    assert deduction.resolve("wiki/projects/shed", 5, material, dates) == kit
    assert deduction.resolve("wiki/projects/shed", "l5", material, dates) == kit
    nested = deduction.resolve("wiki/projects/shed", "L9", material, dates)
    assert nested.text == "  - 2026-02: Nested, with a footnote. [^shed]"  # kept as is
    bad: dict[tuple[Any, Any], str] = {
        ("wiki/projects/other", "L5"): "is not a reviewed page in the material",
        ("wiki/inferred/x", "L5"): "is not a reviewed page in the material",
        (None, "L5"): "a reference without a page id",
        ("wiki/projects/shed", "L99"): "has no line L99",
        ("wiki/projects/shed", "five"): "has no line five",
        ("wiki/projects/shed", "L1"): "L1 is not a stated line: # Shed",
        ("wiki/projects/shed", "L3"): "L3 is not a stated line: ## Timeline",
        ("wiki/projects/shed", "L7"): r"L7 is not a stated line: - \[ \] Order it",
        ("wiki/projects/shed", "L6"): "L6 cites no raw source",
        ("wiki/projects/shed", "L8"): "L8 is already marked superseded",
    }
    for (page, line), why in bad.items():
        with pytest.raises(deduction.Invalid, match=why):
            deduction.resolve(page, line, material, dates)
    with pytest.raises(deduction.Invalid, match="fewer than two distinct"):
        deduction.resolve_all(
            [{"page": "wiki/projects/shed", "line": "L5"}] * 2, material, dates, "premises"
        )


async def test_a_line_s_own_date_comes_first() -> None:
    material, dates = _material()
    kit = deduction.resolve("wiki/projects/shed", "L5", material, dates)
    assert await dates.of(kit) == "2026-01"  # no source read: the wiki is not needed


def test_the_answer_needs_one_of_the_lists() -> None:
    assert deduction.parse('{"supersede": [], "notes": ["a", "b"]}').notes == "a; b"
    for bad in ("[]", '{"ideas": []}', "not json"):
        with pytest.raises(Unparsable):
            deduction.parse(bad)


def _evidence(line: str, source: str) -> inference.Evidence:
    return inference.Evidence("wiki/projects/shed", "Shed [kit]", line, source)


def test_an_inference_page() -> None:
    supports = [
        _evidence("- 2026-02: Starts in April. [^shed]", "raw/chats/claude/shed"),
        _evidence("  - 2026-02: Takes three months. [^hello]", "raw/chats/claude/hello"),
    ]
    taken = {"wiki/inferred/done-by-july"}
    op = inference.write_op(
        title="Done by  July", statement="It is done by July.", kind="deduction",
        supports=supports, about=["wiki/projects/shed"], dream=7, taken=taken, at=AT,
    )  # fmt: skip
    assert op["op"] == "write_inference" and op["page"] == "wiki/inferred/done-by-july-2"
    assert "wiki/inferred/done-by-july-2" in taken
    fm = op["frontmatter"]
    assert fm["confidence"] == "medium" and fm["kind"] == "deduction" and fm["dream"] == 7
    assert fm["supports"][1] == {
        "page": "wiki/projects/shed",
        "quote": "- 2026-02: Takes three months. [^hello]",
        "source": "raw/chats/claude/hello",
    }
    assert fm["stale_after"] == "2026-12-31T07:05:00Z" and fm["status"] == "stable"
    assert op["body"] == (
        "# Done by July\n\n"
        "**Inferred, not stated.** Confidence: medium (2 supporting facts from 2 sources).\n\n"
        "It is done by July.\n\n"
        "## Supporting facts\n"
        '- [Shed (kit)](/wiki/projects/shed.md): "2026-02: Starts in April." [^shed]\n'
        '- [Shed (kit)](/wiki/projects/shed.md): "2026-02: Takes three months." [^hello]\n\n'
        "## Counterexamples\n\n"
        "## History\n"
        "- 2026-10-02: created by dream 7 (confidence medium).\n"
    )
    one_source = [_evidence(s.line, "raw/chats/claude/shed") for s in supports]
    low = inference.write_op(
        title="x", statement="y", kind="deduction", supports=one_source, about=[], dream=7,
        taken=set(), at=AT,
    )  # fmt: skip
    assert low["frontmatter"]["confidence"] == "low"
    assert "(2 supporting facts from 1 source)" in low["body"]


def test_a_speculative_inference_says_so() -> None:
    supports = [
        _evidence(f"- 2026-0{i}: Fact {i}. [^s{i}]", f"raw/chats/claude/s{i}") for i in range(1, 6)
    ]
    op = inference.write_op(
        title="Likes wood", statement="Speculative: he likes wood.", kind="induction",
        supports=supports, about=[], dream=7, taken=set(), at=AT, speculative=True,
    )  # fmt: skip
    assert op["frontmatter"]["confidence"] == "low" and op["frontmatter"]["speculative"] is True
    assert "\n\n**Speculative, inferred, not stated.** Confidence: low (5 supporting" in op["body"]
    with pytest.raises(ValueError, match="Speculative:"):
        inference.write_op(
            title="Likes wood", statement="He likes wood.", kind="induction", supports=supports,
            about=[], dream=7, taken=set(), at=AT, speculative=True,
        )  # fmt: skip
    with pytest.raises(ValueError, match="two supporting"):
        inference.write_op(
            title="x", statement="y", kind="deduction", supports=supports[:1], about=[],
            dream=7, taken=set(), at=AT,
        )  # fmt: skip


def test_a_requested_confidence_is_capped_at_the_rubric() -> None:
    supports = [
        _evidence(f"- 2026-0{i}: Fact {i}. [^s{i % 2}]", f"raw/chats/claude/s{i % 2}")
        for i in range(1, 5)
    ]
    entries = [s.entry() for s in supports]
    assert inference.capped("high", entries, []) == "high"
    assert inference.capped("high", entries, [entries[0]]) == "medium"
    assert inference.capped("low", entries, []) == "low"
    assert inference.capped(None, entries[:2], []) == "medium"
    assert inference.capped("high", entries, [], speculative=True) == "low"
    op = inference.write_op(
        title="x y", statement="y", kind="induction", supports=supports[:2], about=[],
        dream=7, taken=set(), at=AT, confidence="high",
    )  # fmt: skip
    assert op["frontmatter"]["confidence"] == "medium"


def test_an_update_keeps_every_line_and_adds_its_own() -> None:
    old = [
        _evidence("- 2026-02: Starts in April. [^shed]", "raw/chats/claude/shed"),
        _evidence("- 2026-02: Takes three months. [^hello]", "raw/chats/claude/hello"),
    ]
    created = inference.write_op(
        title="Plans", statement="He plans.", kind="induction", supports=old, about=[],
        dream=3, taken=set(), at=AT - dt.timedelta(days=30),
    )  # fmt: skip
    fm, body = created["frontmatter"], created["body"]
    new = _evidence("- 2026-03: A kit. [^shed]", "raw/chats/claude/shed")
    counter = _evidence("- 2026-04: Own design. [^hello]", "raw/chats/claude/hello")
    supports = inference.merged(fm["supports"], [old[0], new])
    counters = inference.merged(fm["counterexamples"], [counter])
    assert len(supports) == 3 and supports[2] == new.entry()
    op = inference.update_op(
        page=created["page"], frontmatter=fm, body=body, supports=supports,
        counterexamples=counters, added=[new], added_counter=[counter], confidence="medium",
        history="updated by dream 7.", at=AT,
    )  # fmt: skip
    assert op["page"] == created["page"] and op["op"] == "write_inference"
    after = op["body"].split("\n")
    for line in body.split("\n"):
        if line.startswith("- "):
            assert line in after, line
    assert op["body"] == (
        "# Plans\n\n"
        "**Inferred, not stated.** Confidence: medium (3 supporting facts from 2 sources).\n\n"
        "He plans.\n\n"
        "## Supporting facts\n"
        '- [Shed (kit)](/wiki/projects/shed.md): "2026-02: Starts in April." [^shed]\n'
        '- [Shed (kit)](/wiki/projects/shed.md): "2026-02: Takes three months." [^hello]\n'
        '- [Shed (kit)](/wiki/projects/shed.md): "2026-03: A kit." [^shed]\n\n'
        "## Counterexamples\n"
        '- [Shed (kit)](/wiki/projects/shed.md): "2026-04: Own design." [^hello]\n\n'
        "## History\n"
        "- 2026-09-02: created by dream 3 (confidence medium).\n"
        "- 2026-10-02: updated by dream 7.\n"
    )
    ofm = op["frontmatter"]
    assert ofm["dream"] == 3 and ofm["title"] == "Plans" and ofm["kind"] == "induction"
    assert ofm["stale_after"] == "2026-12-31T07:05:00Z" and ofm["counterexamples"] == counters
    with pytest.raises(ValueError, match="above what the supports allow"):
        inference.update_op(
            page=created["page"], frontmatter=fm, body=body, supports=supports,
            counterexamples=counters, added=[], added_counter=[], confidence="high",
            history="x", at=AT,
        )  # fmt: skip


def test_induction_answers_and_windows() -> None:
    assert induction.parse('{"patterns": [], "notes": ["a"]}').notes == "a"
    for bad in ("[]", '{"pairs": []}', '{"patterns": {}}', "not json"):
        with pytest.raises(Unparsable):
            induction.parse(bad)
    pages = [
        Page(f"wiki/projects/p{i}", f"wiki/projects/p{i}.md", "project", f"P{i}", {}, "x" * size)
        for i, size in enumerate((300, 300, 900, 300))
    ]
    found, too_long = induction.windows(pages, most=800)
    assert [list(w.pages) for w in found] == [
        ["wiki/projects/p0", "wiki/projects/p1"],
        ["wiki/projects/p3"],
    ]
    assert [p.id for p in too_long] == ["wiki/projects/p2"]


def test_an_inference_is_never_evidence() -> None:
    material, dates = _material()
    with pytest.raises(deduction.Invalid, match="rests on an inference: wiki/inferred/x"):
        induction.evidence(
            [{"page": "wiki/projects/shed", "line": "L5"}, {"page": "/wiki/inferred/x.md",
              "line": "L1"}], material, dates, "supports", 2,
        )  # fmt: skip
    with pytest.raises(deduction.Invalid, match="1 distinct stated line"):
        induction.evidence(
            [{"page": "wiki/projects/shed", "line": "L5"}] * 2, material, dates, "supports", 2
        )
    assert induction.evidence(None, material, dates, "counterexamples", 0) == []


def _page(pid: str, title: str, type_: str = "project", body: str = "") -> Page:
    return Page(pid, pid + ".md", type_, title, {}, body)


def test_candidate_pairs() -> None:
    shed = _page("wiki/projects/garden-shed", "Garden shed")
    the_shed = _page("wiki/projects/the-garden-shed", "The garden shed")  # contains the slug
    shed_kit = _page("wiki/projects/shed-kit", "Garden Shed, kit")  # title: 2 of 3 words
    kit_shed = _page("wiki/projects/kit-for-the-shed", "Kit shed garden")  # same words
    person = _page("wiki/people/garden-shed", "Garden shed", "person")  # another type
    other = _page("wiki/projects/boat", "Boat")
    pairs = merge.candidates([shed, the_shed, shed_kit, kit_shed, person, other])
    assert [(a.id.rsplit("/")[-1], b.id.rsplit("/")[-1]) for a, b in pairs] == [
        ("garden-shed", "the-garden-shed"),  # the slug is contained
        ("garden-shed", "shed-kit"),  # the title "garden shed" is in "garden shed kit"
        ("shed-kit", "kit-for-the-shed"),  # the titles' words are the same: Jaccard 1
    ]  # garden-shed and kit-for-the-shed: titles overlap 2/3, under 0.7; person: another type
    assert merge.near(["a", "b", "c", "d"], ["a", "b", "c", "e"]) is False  # 3/5
    assert merge.near(["a", "b", "c", "d", "e"], ["a", "b", "c", "d", "f"]) is False  # 4/6
    assert merge.near(["a", "b", "c", "d"], ["d", "c", "b", "a", "e"]) is True  # 4/5
    assert merge.near([], ["a"]) is False
    assert len(merge.candidates([_page(f"wiki/x/p-{i}", "Same") for i in range(9)])) == 20


def test_merge_operations() -> None:
    kept = Page(
        "wiki/projects/garden-shed", "wiki/projects/garden-shed.md", "project", "Garden [shed]",
        {"sources": [{"resource": "/raw/chats/claude/shed.md"}], "related": ["wiki/a"]},
        "# Garden shed\n\n## Timeline\n\n- 2026-01: A kit. [^shed]\n",
    )  # fmt: skip
    other = Page(
        "wiki/projects/shed", "wiki/projects/shed.md", "project", "Shed",
        {"sources": [{"resource": "/raw/chats/claude/shed.md"},
                     {"resource": "/raw/chats/claude/hello.md"}],
         "related": ["/wiki/a.md", "wiki/b", "wiki/projects/garden-shed", "wiki/gone"]},
        "# Shed\n\n- Loose line. [^hello]\n\n## Timeline\n\n-  2026-01: a KIT.  [^hello]\n"
        "- 2026-02: Green. [^hello]\n\n```\n- not a line\n```\n### Who\n\n  - Jane. [^hello]\n",
    )  # fmt: skip
    known = {"raw/chats/claude/hello", "wiki/a", "wiki/b", "wiki/projects/garden-shed"}
    ops = merge.merge_ops(kept, other, known, 7, AT)
    assert ops == [
        {"op": "append", "page": kept.id, "section": "Merged", "line": "- Loose line. [^hello]"},
        {"op": "append", "page": kept.id, "section": "Timeline",
         "line": "- 2026-02: Green. [^hello]"},
        {"op": "append", "page": kept.id, "section": "Who", "line": "- Jane. [^hello]"},
        {"op": "add_sources", "page": kept.id, "ids": ["raw/chats/claude/hello"]},
        {"op": "add_related", "page": kept.id, "ids": ["wiki/b"]},
        {"op": "deprecate", "page": other.id, "note": "Merged into [Garden (shed)]"
         "(/wiki/projects/garden-shed.md) on 2026-10-02 by dream 7."},
    ]  # fmt: skip
    assert merge.parse('{"pairs": []}').pairs == []
    with pytest.raises(Unparsable):
        merge.parse('{"patterns": []}')


# --- private pages ---------------------------------------------------------------------------


def _private(pid: str, title: str, body: str = "", type_: str = "project") -> Page:
    return Page(pid, pid + ".md", type_, title, {"private": True}, body)


def test_private_pages_are_marked_in_the_material() -> None:
    block, _ = numbered(_private("wiki/people/sam", "Sam", "- x", "person"))
    assert block.startswith('<page id="wiki/people/sam" type="person" status="stable" lang="en" '
                            'private="true">')  # fmt: skip
    assert "private" not in numbered(_page("wiki/projects/shed", "Shed"))[0].split("\n")[0]


async def test_deduction_keeps_private_lines_off_non_private_pages() -> None:
    material, dates = _material()
    sam = "# Sam\n\n- 2026-05: Sam is moving in. [^shed]\n"
    assert material.add(_private("wiki/people/sam", "Sam", sam, "person"))
    plan = deduction.Plan()
    with pytest.raises(deduction.Invalid, match="not private, wiki/people/sam L3 is"):
        await deduction._supersede(
            {"page": "wiki/projects/shed", "line": "L5", "by_page": "wiki/people/sam",
             "by_line": "L3", "reason": "Sam moves in"}, material, dates, plan, "2026-10-02",
        )  # fmt: skip
    conflict = {"page": "wiki/projects/shed", "followup": "Who builds it?",
                "lines": [{"page": "wiki/projects/shed", "line": "L5"},
                          {"page": "wiki/people/sam", "line": "L3"}]}  # fmt: skip
    with pytest.raises(deduction.Invalid, match="involves a private page"):
        deduction._conflict(conflict, material, dates, plan, "2026-10")
    # onto the private page itself it is fine
    deduction._conflict({**conflict, "page": "wiki/people/sam"}, material, dates, plan, "2026-10")
    assert list(plan.pages) == ["wiki/people/sam"]


def test_private_and_non_private_pages_are_never_merged() -> None:
    shed = _page("wiki/projects/garden-shed", "Garden shed")
    secret = _private("wiki/projects/the-garden-shed", "The garden shed")
    assert merge.candidates([shed, secret]) == []
    assert len(merge.candidates([secret, _private("wiki/projects/garden-shed", "Shed")])) == 1
    other = Page("wiki/projects/shed", "wiki/projects/shed.md", "project", "Shed",
                 {"related": ["wiki/a", "wiki/people/sam"]}, "# Shed\n")  # fmt: skip
    known = {"wiki/a", "wiki/people/sam"}
    ops = merge.merge_ops(shed, other, known, 7, AT, frozenset({"wiki/people/sam"}))
    assert {"op": "add_related", "page": shed.id, "ids": ["wiki/a"]} in ops
    ops = merge.merge_ops(secret, other, known, 7, AT, frozenset({"wiki/people/sam"}))
    assert {"op": "add_related", "page": secret.id, "ids": ["wiki/a", "wiki/people/sam"]} in ops


async def test_hygiene_never_sends_a_link_to_a_private_page() -> None:
    class Renames:
        async def renamed(self, ref: str) -> list[str]:
            return ["wiki/people/sam-doe"]

    body = "# Shed\n\n- Built with [Sam](/wiki/people/sam.md).\n"
    known = {"wiki/people/sam-doe", "wiki/projects/shed"}
    wiki = cast(WikiReader, Renames())
    public = await hygiene.plan(wiki, [_page("wiki/projects/shed", "Shed", body=body)], {},
                                known, {}, frozenset({"wiki/people/sam-doe"}))  # fmt: skip
    assert public.fixes == []
    assert any("renamed to a private page" in f for f in public.flags)
    private = await hygiene.plan(wiki, [_private("wiki/projects/shed", "Shed", body)], {},
                                 known, {}, frozenset({"wiki/people/sam-doe"}))  # fmt: skip
    assert [f.op["new"] for f in private.fixes] == ["- Built with [Sam](/wiki/people/sam-doe.md)."]


# --- what the checks drop, and why -------------------------------------------------------


async def test_deduction_reports_what_it_dropped() -> None:
    material, dates = _material()
    answer = deduction.Answer(
        {"supersede": [{"page": "wiki/projects/other", "line": "L1"}, "x"],
         "conflicts": [], "implications": [{"title": "x"}] * 11}
    )  # fmt: skip
    plan = await deduction.check(answer, material, dates, set(), set(), 7, AT)
    assert plan.proposals(7) == []
    assert plan.dropped == [
        "supersede: wiki/projects/other is not a reviewed page in the material",
        "supersede: not an object",
        "implications: 1 over the limit of 10",
        *["implications: the title is under 3 characters"] * 10,
    ]


def test_induction_reports_what_it_dropped() -> None:
    material, dates = _material()
    state = induction.Induction(
        dates=dates, by_id=dict(material.pages), existing={}, known={"wiki/projects/shed"},
        taken=set(), dream=7, at=AT,
    )  # fmt: skip
    kit = {"page": "wiki/projects/shed", "line": "L5"}
    nested = {"page": "wiki/projects/shed", "line": "L9"}
    create = {"action": "create", "title": "Kits", "statement": "He tends to buy kits.",
              "confidence": "high", "supports": [kit, nested], "about": ["wiki/gone"]}  # fmt: skip
    review = state.review(
        [{"action": "bogus"}, "x", {**create, "supports": [kit]}, create, *[{}] * 20], material
    )
    assert review.over == 4 and len(review.items) == 20
    kept = [c for c in review.items if c.proposal is not None]
    assert [c.proposal.summary for c in kept if c.proposal] == ["Induction: Kits"]
    assert kept[0].flags == [
        "capped: Kits: confidence high lowered to low, what its supports allow",
        "dropped: Kits: unknown pages left out of 'about': wiki/gone",
    ]
    assert review.dropped[:4] == [
        "4 patterns over the limit of 20",
        "unknown action 'bogus'",
        "not an object",
        "Kits: supports: 1 distinct stated line(s), at least 2 needed",
    ]
    assert len(review.dropped) == 1 + 19


async def test_merge_checks_report_why_a_pair_is_dropped() -> None:
    shed = _page("wiki/projects/garden-shed", "Garden shed", body="# Garden shed\n")
    the_shed = _page("wiki/projects/the-garden-shed", "The garden shed", body="# Shed\n")
    docs = {p.id: {"id": p.id, "title": p.title, "frontmatter": {"type": "project"},
                   "body": p.body} for p in (shed, the_shed)}  # fmt: skip

    class Docs:
        async def doc(self, ref: str) -> dict[str, Any]:
            if ref not in docs:
                raise LookupError(ref)
            return docs[ref]

    wiki = cast(WikiReader, Docs())
    state = merge.Duplicates([(shed, the_shed)], set(), frozenset(), 7, AT)
    pair = {"a": shed.id, "b": the_shed.id, "same": True, "keep": "a", "reason": "One shed."}
    bad = {
        "not an object": "x",
        "are not a candidate pair": {**pair, "b": "wiki/projects/boat"},
        "'same' is not true or false": {**pair, "same": "yes"},
        '\'keep\' is not "a" or "b"': {**pair, "keep": "c"},
        "no reason": {**pair, "reason": ""},
    }
    for why, item in bad.items():
        with pytest.raises(deduction.Invalid, match=why):
            await state.check(item, wiki)
    assert await state.check({**pair, "same": False}, wiki) is None and state.not_same == 1
    made = await state.check(pair, wiki)
    assert made is not None and made.summary == "Merge The garden shed into Garden shed"
    assert made.why(7) == (
        "Dream 7 hygiene: wiki/projects/the-garden-shed and wiki/projects/garden-shed are the "
        "same: One shed."
    )
    state.proposed(made)
    with pytest.raises(merge.LeftOut, match="already in another merge proposal"):
        await state.check(pair, wiki)
    del docs[the_shed.id]
    fresh = merge.Duplicates([(shed, the_shed)], set(), frozenset(), 7, AT)
    with pytest.raises(deduction.Invalid, match="the-garden-shed is no longer a reviewed page"):
        await fresh.check(pair, wiki)
