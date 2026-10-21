"""Stated lines, the superseded mark, the bullet-line guard and the confidence rubric."""

from __future__ import annotations

import pytest

from wiki_mcp.wiki.facts import (
    above,
    bad_superseded,
    bullet_lines,
    collapse_checkboxes,
    footnotes,
    guard_bullets,
    he_for_they,
    is_inference_id,
    is_stated,
    line_date,
    mark_superseded,
    normalized,
    pronouns_only,
    rubric,
    superseded,
    swapped_link,
)

LINE = "- 2026-03: Jane leads the migration. [^chat-1]"
BODY = f"""# Jane

Intro text, not a bullet.

## Timeline

{LINE}
- 2026-05: Jane moved to the data team. [^chat-2]

```
- not a bullet: inside a code fence
```

## Follow-ups

- [ ] Ask Jane about the budget (as of 2026-05)
"""


def test_bullet_and_stated_lines() -> None:
    lines = bullet_lines(BODY)
    assert lines == [
        LINE,
        "- 2026-05: Jane moved to the data team. [^chat-2]",
        "- [ ] Ask Jane about the budget (as of 2026-05)",
    ]
    assert is_stated(LINE) and not is_stated(lines[2])
    assert footnotes(LINE + " and [^chat-9]") == ["chat-1", "chat-9"]
    assert footnotes("[^chat-1]: the definition") == []
    assert line_date(LINE) == "2026-03" and line_date("- [ ] 2026-05-02: x") == "2026-05-02"
    assert line_date(lines[2]) is None
    assert normalized("* Jane  LEADS it. [^a]") == "jane leads it."


def test_superseded_mark_round_trip() -> None:
    marked = mark_superseded(LINE, "2026-05-02", "moved to the data team")
    assert marked == LINE + " *(superseded 2026-05-02: moved to the data team)*"
    mark = superseded(marked)
    assert mark is not None and mark.line == LINE and mark.date == "2026-05-02"
    assert superseded(LINE) is None and not bad_superseded(marked)
    with pytest.raises(ValueError, match="already"):
        mark_superseded(marked, "2026-06-01", "again")
    with pytest.raises(ValueError):
        mark_superseded(LINE, "May 2026", "reason")
    # a reason cannot break the format
    assert superseded(mark_superseded(LINE, "2026-05-02", "see (new) *line*")) is not None


@pytest.mark.parametrize(
    "line",
    [
        LINE + " (superseded 2026-05-02: moved)",  # no asterisks
        LINE + " *(Superseded: moved)*",  # no date
        LINE + " *(superseded 2026-5-2: moved)*",
        LINE + " *(vervangen 2026-05-02: verhuisd)* *(superseded)*",
    ],
)
def test_malformed_marks_are_recognised(line: str) -> None:
    assert bad_superseded(line)


def test_guard_accepts_additions_and_a_superseded_suffix() -> None:
    marked = BODY.replace(LINE, mark_superseded(LINE, "2026-05-02", "moved"))
    after = marked + "\n- 2026-06: Jane leads the data team. [^chat-3]\n"
    assert guard_bullets(BODY, after) == []
    assert guard_bullets(BODY, BODY.replace("Intro text", "Other intro")) == []  # prose


@pytest.mark.parametrize(
    "change",
    [
        lambda b: b.replace(LINE + "\n", ""),  # removed
        lambda b: b.replace("leads the migration", "led the migration"),  # reworded
        lambda b: b.replace(LINE, LINE + " (updated)"),  # anything but the suffix
        lambda b: b.replace("- [ ] Ask", "- [x] Ask"),  # a checkbox is a reword too
    ],
)
def test_guard_refuses_a_removed_or_reworded_line(change) -> None:  # type: ignore[no-untyped-def]
    problems = guard_bullets(BODY, change(BODY))
    assert len(problems) == 1 and problems[0].startswith("bullet line removed or reworded")


def test_guard_counts_duplicate_lines() -> None:
    twice = BODY + f"\n{LINE}\n"
    assert guard_bullets(twice, BODY) != []  # one of the two copies went


def test_confidence_rubric() -> None:
    def sup(n: int, sources: int) -> list[dict[str, str]]:
        return [{"page": "wiki/p", "quote": "q", "source": f"raw/s{i % sources}"} for i in range(n)]

    assert rubric(sup(4, 2), []) == "high"
    assert rubric(sup(4, 2), [{"page": "wiki/x"}]) == "medium"  # a counterexample
    assert rubric(sup(5, 1), []) == "medium"  # one source only
    assert rubric(sup(3, 1), []) == "medium"
    assert rubric(sup(2, 2), []) == "medium"
    assert rubric(sup(2, 1), []) == "low"
    assert above("high", "medium") and not above("low", "medium") and above("certain", "high")
    assert is_inference_id("/wiki/inferred/x.md") and not is_inference_id("wiki/people/x")


def test_hygiene_rewrites() -> None:
    assert collapse_checkboxes("- [ ] - [ ] Call Jane") == "- [ ] Call Jane"
    assert collapse_checkboxes("  * [x] - [ ] - [ ] done") == "  * [x] done"
    assert collapse_checkboxes("- [ ] Call Jane") == "- [ ] Call Jane"
    line = "- 2025-12: Wants to build an app on their own idea; Their plan suits them. [^a]"
    fixed = he_for_they(line)
    assert fixed == "- 2025-12: Wants to build an app on his own idea; His plan suits him. [^a]"
    assert pronouns_only(line, fixed)
    assert not pronouns_only(line, line)  # nothing changed
    assert not pronouns_only("- they want it", "- he wants it")  # "they" is not swapped
    assert not pronouns_only(line, fixed.replace("app", "tool"))
    link = "- Works with [Jane](/wiki/people/jane.md) at [Acme](/wiki/organizations/acme.md)"
    moved = link.replace("/wiki/people/jane.md", "/wiki/people/jane-doe.md")
    assert swapped_link(link, moved) == ("/wiki/people/jane.md", "/wiki/people/jane-doe.md")
    assert swapped_link(link, moved.replace("Works", "Worked")) is None
    assert swapped_link(link, link) is None


def test_guard_takes_an_allowed_rewrite() -> None:
    before = "- [ ] - [ ] Call Jane\n- kept\n"
    after = "- [ ] Call Jane\n- kept\n"
    assert guard_bullets(before, after) != []
    assert guard_bullets(before, after, lambda o, n: collapse_checkboxes(o) == n) == []
    assert guard_bullets(before, "- kept\n", lambda o, n: True) != []  # removal stays refused
