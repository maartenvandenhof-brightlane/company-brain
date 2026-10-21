"""Stated lines, superseded lines and inferences: the parts of a page that dreams may touch.

  bullet line     a list item line (`- …`, `* …`, `+ …`) outside code fences
  stated line     a bullet line with a `[^id]` footnote (the id of a raw source), on a page
                  that is not an inference: what the owner said or accepted, with a date
  superseded      a stated line that a newer one replaces keeps its text byte for byte and
                  gets ` *(superseded YYYY-MM-DD: <reason>)*` appended. The word is always
                  English so code can find it; the reason is in the page's language.
  line date       the leading `YYYY-MM[-DD]:` of a line (else the date of its source)

The bullet-line guard holds every dream commit and every accepted dream proposal to one
rule: no bullet line of a page is deleted or reworded. Each line that was there must still
be there, identical, or identical plus a superseded suffix. A dream's direct hygiene commit
may also make three exact rewrites, each re-derived here from the old line:
  checkbox   a doubled checkbox collapsed: `- [ ] - [ ] x` becomes `- [ ] x`
  link       one link target swapped (the store checks it is the old page's only rename)
  pronoun    on the owner profile: their/theirs/them/themselves/themself become
             his/his/him/himself/himself (not "they": that would need the verb changed)

Inference pages (type `inference`, under wiki/inferred/) list their `supports`; the
confidence rubric says how confident one may be with them.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

INFERRED_FOLDER = "wiki/inferred"
BANNER = "**Inferred, not stated.**"
SPECULATIVE_BANNER = "**Speculative, inferred, not stated.**"
CONFIDENCES = ("low", "medium", "high")

_BULLET = re.compile(r"^\s*[-*+]\s")
_FENCE = re.compile(r"^\s*(```|~~~)")
_FOOTNOTE = re.compile(r"\[\^([^\]\s]+)\](?!:)")
_SUFFIX = re.compile(r" \*\(superseded (\d{4}-\d{2}-\d{2}): ([^()*\n]{1,300})\)\*$")
_SUFFIX_LIKE = re.compile(r"\*?\(\s*superseded\b", re.IGNORECASE)
_DATE = re.compile(r"^\s*[-*+]\s+(?:\[[ xX]\]\s+)?(\d{4}-\d{2}(?:-\d{2})?):")


@dataclass(frozen=True)
class Superseded:
    line: str  # the line as it was
    date: str  # YYYY-MM-DD
    reason: str


def bullet_lines(body: str) -> list[str]:
    """Every bullet line of a page body, in order, outside code fences."""
    out: list[str] = []
    fenced = False
    for line in body.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced and _BULLET.match(line):
            out.append(line.rstrip("\r"))
    return out


def footnotes(line: str) -> list[str]:
    """The footnote ids a line cites (`[^id]`, not a footnote definition `[^id]:`)."""
    return _FOOTNOTE.findall(line)


def is_stated(line: str) -> bool:
    """A bullet line citing a source with a footnote (the caller checks it is a raw source
    and that the page is not an inference)."""
    return bool(_BULLET.match(line)) and bool(footnotes(line))


def superseded(line: str) -> Superseded | None:
    """The superseded mark of a line, or None when it has none."""
    m = _SUFFIX.search(line)
    if m is None:
        return None
    return Superseded(line[: m.start()], m.group(1), m.group(2))


def bad_superseded(line: str) -> bool:
    """A line that looks marked superseded but not in the exact format."""
    return bool(_SUFFIX_LIKE.search(line)) and superseded(line) is None


def mark_superseded(line: str, date: str, reason: str) -> str:
    """The line with the superseded suffix appended (its text kept byte for byte)."""
    reason = " ".join(reason.replace("(", "[").replace(")", "]").replace("*", "").split())
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not reason:
        raise ValueError("a superseded mark needs a YYYY-MM-DD date and a reason")
    if superseded(line) is not None:
        raise ValueError("the line is already marked superseded")
    return f"{line} *(superseded {date}: {reason[:300]})*"


def line_date(line: str) -> str | None:
    """The leading YYYY-MM[-DD] of a bullet line, if it has one."""
    m = _DATE.match(line)
    return m.group(1) if m else None


def guard_bullets(
    before: str, after: str, allow: Callable[[str, str], bool] | None = None
) -> list[str]:
    """Why `after` may not replace `before`: every bullet line of `before` must still be in
    `after`, identical or with a superseded suffix appended, or rewritten in a way `allow`
    accepts (hygiene). An empty list: it may."""
    pool = Counter(bullet_lines(after))
    missing = []
    for line in bullet_lines(before):
        if pool[line] > 0:
            pool[line] -= 1
        else:
            missing.append(line)
    left = list(pool.elements())
    problems = []
    for line in missing:
        match = next((n for n in left if (m := superseded(n)) and m.line == line), None)
        if match is None and allow is not None:
            match = next((n for n in left if allow(line, n)), None)
        if match is None:
            problems.append(f"bullet line removed or reworded: {line.strip()[:200]}")
        else:
            left.remove(match)
    return problems


# --- hygiene rewrites -------------------------------------------------------------------

_DOUBLE_BOX = re.compile(r"^(\s*[-*+]\s+\[[ xX]\]\s+)(?:[-*+]\s+\[[ xX]\]\s+)+")
_TARGET = re.compile(r"(\]\()([^)\s]+)(\))")
_WORD = re.compile(r"[A-Za-z]+|[^A-Za-z]+")
PRONOUNS = {
    "their": "his",
    "theirs": "his",
    "them": "him",
    "themselves": "himself",
    "themself": "himself",
}


def collapse_checkboxes(line: str) -> str:
    return _DOUBLE_BOX.sub(r"\1", line)


def he_for_they(line: str) -> str:
    """The owner's pronouns in a line: their/them/... become his/him/..., capitals kept."""

    def swap(word: str) -> str:
        new = PRONOUNS.get(word.lower())
        if new is None:
            return word
        return new.capitalize() if word[0].isupper() else new

    return "".join(swap(t) for t in _WORD.findall(line))


def pronouns_only(old: str, new: str) -> bool:
    """`new` is `old` with some of the owner's pronouns swapped, nothing else."""
    a, b = _WORD.findall(old), _WORD.findall(new)
    return (
        len(a) == len(b)
        and a != b
        and all(x == y or he_for_they(x) == y for x, y in zip(a, b, strict=True))
    )


def swapped_link(old: str, new: str) -> tuple[str, str] | None:
    """(old target, new target) when the lines differ in exactly one link target."""
    a, b = _TARGET.split(old), _TARGET.split(new)
    if len(a) != len(b):
        return None
    diffs = [i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]
    if len(diffs) != 1 or diffs[0] % 4 != 2:  # split: text, "](", target, ")", text, ...
        return None
    return a[diffs[0]], b[diffs[0]]


def normalized(line: str) -> str:
    """A line compared across pages: without the bullet, footnotes, spacing or case."""
    text = _BULLET.sub("", line, count=1)
    return " ".join(_FOOTNOTE.sub("", text).split()).lower()


# --- inferences -------------------------------------------------------------------------


def rubric(supports: list[Any], counterexamples: list[Any]) -> str:
    """The highest confidence the supports allow:
    high    at least 4 supports from at least 2 different sources, and no counterexample
    medium  at least 3 supports, or 2 supports from 2 different sources
    low     anything else"""
    sources = {s.get("source") for s in supports if isinstance(s, dict) and s.get("source")}
    if len(supports) >= 4 and len(sources) >= 2 and not counterexamples:
        return "high"
    if len(supports) >= 3 or (len(supports) >= 2 and len(sources) >= 2):
        return "medium"
    return "low"


def above(confidence: str, ceiling: str) -> bool:
    """Whether `confidence` is higher than `ceiling` (an unknown value counts as too high)."""
    if confidence not in CONFIDENCES:
        return True
    return CONFIDENCES.index(confidence) > CONFIDENCES.index(ceiling)


def is_inference_id(page_id: str) -> bool:
    return page_id.strip("/").removesuffix(".md").startswith(INFERRED_FOLDER + "/")


def opening(body: str) -> str:
    """The first line of a body that is neither empty nor a heading."""
    for line in body.split("\n"):
        text = line.strip()
        if text and not text.startswith("#"):
            return text
    return ""
