"""D3, induction: patterns that recur across stated lines. Proposals only.

  read      every reviewed page, newest first, every body line numbered (pages.numbered), in
            windows of at most 60,000 characters; and every existing inference (type
            inference, status stable), shown compactly with its statement and supports
  ask       the strong model (the cheap one when the budget says so), once per window, in
            order, at most 5 windows per dream; stop when the budget refuses one
  check     supports and counterexamples resolve (page id and L<n>) to stated lines on
            reviewed pages of the window, never on an inference ("rests on an inference")
              create     a title, a statement, at least two supports, a known confidence;
                         at most 5 per dream
              update     an existing inference: new supports (or counterexamples) added
              downgrade  an existing inference: a confidence strictly lower than its own
              retire     an existing inference: it no longer holds (a reason)
            the confidence is capped at the rubric for the supports (and logged when it is);
            a speculative pattern says "Speculative:" and has confidence low, or is dropped
  propose   one proposal per pattern (a dream proposal is refused once its page changed, so
            an inference gets at most one per dream): create writes a new inference page of
            kind induction, update and downgrade write the page again keeping every line
            (inference.update_op), retire deprecates it with a note

Whatever fails a check is dropped and why goes into the log, as do the model's notes. An
inference is never evidence: not for a fact, not for another inference.

The asking and proposing are the caller's (the portal's pass in portal/dreams/induction.py):
this module has the material, the answer's parse and the checks, and makes the proposals.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from wiki_mcp.dream import Unparsable, inference
from wiki_mcp.dream.deduction import (
    REASON_CHARS,
    STATEMENT_CHARS,
    TITLE_CHARS,
    Dates,
    Invalid,
    Material,
    Stated,
    one_line,
    page_ref,
    resolve,
)
from wiki_mcp.dream.hygiene import raw_ids_by_stem
from wiki_mcp.dream.limits import MAX_CHARS
from wiki_mcp.dream.pages import Page, read
from wiki_mcp.dream.reader import WikiReader
from wiki_mcp.wiki.facts import (
    BANNER,
    CONFIDENCES,
    SPECULATIVE_BANNER,
    above,
    bullet_lines,
    is_inference_id,
    superseded,
)
from wiki_mcp.wiki.patterns import is_pattern_id

NAME = "induction"
MAX_WINDOWS = 5
MAX_CREATES = 5
MAX_PATTERNS = 20  # patterns considered per window
ACTIONS = ("create", "update", "downgrade", "retire")
NOTES_CHARS = 300
STATEMENT_SHOWN = 600


@dataclass
class Answer:
    patterns: list[Any]
    notes: str = ""


def parse(answer: str) -> Answer:
    """The answer's patterns; Unparsable unless it is a JSON object with a `patterns` list."""
    try:
        data = json.loads(answer)
    except ValueError as exc:
        raise Unparsable(f"not JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("patterns"), list):
        raise Unparsable("the answer must be a JSON object with a patterns list")
    notes = data.get("notes")
    text = "; ".join(str(n) for n in notes) if isinstance(notes, list) else ""
    return Answer(data["patterns"], " ".join(text.split())[:NOTES_CHARS])


# --- material --------------------------------------------------------------------------


def windows(pages: list[Page], most: int = MAX_CHARS) -> tuple[list[Material], list[Page]]:
    """The pages in order, in windows of at most `most` characters; and the pages too long
    for any window."""
    out: list[Material] = []
    too_long: list[Page] = []
    current = Material(max_pages=10_000, max_chars=most)
    for page in pages:
        if current.add(page):
            continue
        if current.pages:
            out.append(current)
            current = Material(max_pages=10_000, max_chars=most)
        if not current.add(page):
            too_long.append(page)
    if current.pages:
        out.append(current)
    return out, too_long


def statement_of(page: Page) -> str:
    """An inference page's statement: the text between its banner and its first section."""
    lines: list[str] = []
    started = False
    for line in page.body.split("\n"):
        if line.startswith((BANNER, SPECULATIVE_BANNER)):
            started = True
        elif line.startswith("#") and started:
            break
        elif started and line.strip():
            lines.append(line.strip())
    return " ".join(lines) or str(page.frontmatter.get("description") or "")


def entries(page: Page, key: str) -> list[dict[str, str]]:
    """An inference's `supports` or `counterexamples`, as {page, quote, source}."""
    out = []
    for e in page.frontmatter.get(key) or []:
        if isinstance(e, dict):
            out.append({k: str(e.get(k) or "") for k in ("page", "quote", "source")})
    return out


def current(items: list[dict[str, str]], by_id: dict[str, Page]) -> list[dict[str, str]]:
    """Entries whose line has since been marked superseded quote the marked line (the text
    is the same; wiki-mcp checks the quote is a line of the page)."""
    out = []
    for e in items:
        page = by_id.get(e["page"])
        if page is not None:
            lines = [line.strip() for line in bullet_lines(page.body)]
            if e["quote"] not in lines:
                marked = next(
                    (x for x in lines if (m := superseded(x)) and m.line.strip() == e["quote"]),
                    None,
                )
                if marked is not None:
                    e = {**e, "quote": marked}
        out.append(e)
    return out


def inferences_block(existing: dict[str, Page], by_id: dict[str, Page]) -> str:
    """The existing inferences, compactly, for the material."""
    if not existing:
        return "<inferences>\nNone yet.\n</inferences>"
    out = ["<inferences>"]
    for page in existing.values():
        fm = page.frontmatter
        speculative = "true" if fm.get("speculative") is True else "false"
        out.append(
            f'<inference id="{page.id}" kind="{fm.get("kind", "")}" '
            f'confidence="{fm.get("confidence", "")}" speculative="{speculative}">'
        )
        out.append(f"title: {page.title}")
        out.append(f"statement: {statement_of(page)[:STATEMENT_SHOWN]}")
        for key in ("supports", "counterexamples"):
            items = current(entries(page, key), by_id)
            out.append(f"{key}:" + ("" if items else " none"))
            for e in items:
                mark = " (now marked superseded)" if superseded(e["quote"]) else ""
                out.append(f"- {e['page']}: {e['quote']}{mark}")
        out.append("</inference>")
    out.append("</inferences>")
    return "\n".join(out)


def text_of(window: Material, block: str) -> str:
    return "<material>\n" + "\n".join(window.blocks) + "\n" + block + "\n</material>"


# --- checks -----------------------------------------------------------------------------


def evidence(refs: Any, material: Material, dates: Dates, what: str, least: int) -> list[Stated]:
    """The distinct stated lines of a list of {page, line}, at least `least` of them; never
    a line of an inference."""
    if refs is None and least == 0:
        return []
    if not isinstance(refs, list):
        raise Invalid(f"{what} must be a list of line references")
    out: list[Stated] = []
    for ref in refs:
        if not isinstance(ref, dict):
            raise Invalid(f"{what}: each is {{page, line}}")
        pid = ref.get("page")
        if isinstance(pid, str) and is_inference_id(pid.strip()):
            raise Invalid(f"rests on an inference: {page_ref(pid)[:100]}")
        stated = resolve(pid, ref.get("line"), material, dates)
        if all(s.ref != stated.ref for s in out):
            out.append(stated)
    if len(out) < least:
        raise Invalid(f"{what}: {len(out)} distinct stated line(s), at least {least} needed")
    return out


def _evidence(stated: list[Stated]) -> list[inference.Evidence]:
    return [inference.Evidence(s.page.id, s.page.title, s.text, s.source) for s in stated]


def _confidence(item: dict[str, Any]) -> str:
    value = item.get("confidence")
    if value not in CONFIDENCES:
        raise Invalid(f"unknown confidence {str(value)[:20]!r}")
    return str(value)


def _reason(item: dict[str, Any], required: bool = False) -> str:
    value = item.get("reason")
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise Invalid("no reason")
        return ""
    return one_line(value, "reason", REASON_CHARS)


@dataclass
class Proposal:
    summary: str
    reason: str
    ops: list[dict[str, Any]]
    speculative: bool


@dataclass
class Checked:
    """One pattern after the checks: its proposal, or why it was dropped; and what the checks
    flagged on the way (capped confidences, unknown pages left out of `about`)."""

    item: Any
    proposal: Proposal | None = None
    dropped: str = ""
    flags: list[str] = field(default_factory=list)


@dataclass
class Review:
    """A window's patterns after the checks, in the answer's order."""

    items: list[Checked]
    over: int = 0  # patterns over MAX_PATTERNS, left out unchecked

    @property
    def dropped(self) -> list[str]:
        """Why each pattern left out was."""
        out = [f"{self.over} patterns over the limit of {MAX_PATTERNS}"] if self.over else []
        return out + [c.dropped for c in self.items if c.proposal is None]


@dataclass
class Induction:
    """What the checks need across the windows of one dream."""

    dates: Dates
    by_id: dict[str, Page]  # the reviewed pages
    existing: dict[str, Page]  # the inferences (status stable)
    known: set[str]  # page ids that may be in `about`
    taken: set[str]  # page ids in use
    dream: int
    at: dt.datetime
    creates: int = 0
    touched: set[str] = field(default_factory=set)  # inferences with a proposal this dream
    flags: list[str] = field(default_factory=list)

    def review(self, patterns: list[Any], material: Material) -> Review:
        """Every pattern of one window's answer checked, in order (the first MAX_PATTERNS)."""
        checked = [self.checked(item, material) for item in patterns[:MAX_PATTERNS]]
        return Review(checked, max(0, len(patterns) - MAX_PATTERNS))

    def checked(self, item: Any, material: Material) -> Checked:
        """One pattern checked: its proposal, or why it was dropped; with the flags."""
        out = Checked(item)
        try:
            out.proposal = self.check(item, material)
        except Invalid as exc:
            out.dropped = str(exc)
        finally:
            out.flags = list(self.flags)
            self.flags.clear()
        return out

    def check(self, item: Any, material: Material) -> Proposal:
        """The proposal a pattern makes; Invalid saying why not."""
        if not isinstance(item, dict):
            raise Invalid("not an object")
        action = item.get("action")
        if action not in ACTIONS:
            raise Invalid(f"unknown action {str(action)[:20]!r}")
        if action == "create":
            return self._create(item, material)
        pid = page_ref(item.get("id"))
        page = self.existing.get(pid)
        if page is None:
            raise Invalid(f"{action}: {pid[:100]} is not an existing inference")
        if pid in self.touched:
            raise Invalid(f"{action}: {pid} already has a proposal this dream")
        proposal = (
            self._retire(item, page)
            if action == "retire"
            else self._update(item, material, page, action == "downgrade")
        )
        self.touched.add(pid)
        return proposal

    def _create(self, item: dict[str, Any], material: Material) -> Proposal:
        title = one_line(item.get("title"), "title", TITLE_CHARS[1])
        if len(title) < TITLE_CHARS[0]:
            raise Invalid(f"the title is under {TITLE_CHARS[0]} characters")
        statement = item.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            raise Invalid(f"{title}: no statement")
        statement = statement.strip()
        if len(statement) > STATEMENT_CHARS:
            raise Invalid(f"{title}: the statement is over {STATEMENT_CHARS} characters")
        speculative = item.get("speculative", False)
        if not isinstance(speculative, bool):
            raise Invalid(f"{title}: 'speculative' is not true or false")
        try:
            confidence = _confidence(item)
            if speculative and not statement.startswith(inference.SPECULATIVE):
                raise Invalid(
                    f'speculative, but the statement does not start "{inference.SPECULATIVE}"'
                )
            if speculative and confidence != "low":
                raise Invalid(f"speculative, but confidence {confidence}, not low")
            supports = evidence(item.get("supports"), material, self.dates, "supports", 2)
            counter = evidence(
                item.get("counterexamples") or None, material, self.dates, "counterexamples", 0
            )
            reason = _reason(item)
        except Invalid as exc:
            raise Invalid(f"{title}: {exc}") from exc
        if {s.ref for s in supports} & {c.ref for c in counter}:
            raise Invalid(f"{title}: a line is both a support and a counterexample")
        if self.creates >= MAX_CREATES:
            raise Invalid(f"{title}: over the limit of {MAX_CREATES} new inferences per dream")
        ev, cev = _evidence(supports), _evidence(counter)
        ceiling = inference.capped(None, [e.entry() for e in ev], [c.entry() for c in cev])
        if above(confidence, ceiling):
            self.flags.append(
                f"capped: {title}: confidence {confidence} lowered to {ceiling}, "
                "what its supports allow"
            )
        op = inference.write_op(
            title=title,
            statement=statement,
            kind="induction",
            supports=ev,
            counterexamples=cev,
            about=self._about(item, title) or list(dict.fromkeys(s.page.id for s in supports)),
            dream=self.dream,
            taken=self.taken,
            at=self.at,
            speculative=speculative,
            confidence=confidence,
        )
        self.creates += 1
        why = f"rests on {', '.join(s.ref for s in supports)}"
        if counter:
            why += f"; counterexamples {', '.join(c.ref for c in counter)}"
        return Proposal(
            f"Induction: {title}",
            f"Dream {self.dream} induction: {title} {why}" + (f": {reason}" if reason else ""),
            [op],
            speculative,
        )

    def _about(self, item: dict[str, Any], title: str) -> list[str]:
        listed = item.get("about")
        ids = [
            a.strip().strip("/").removesuffix(".md") if isinstance(a, str) else ""
            for a in (listed if isinstance(listed, list) else [])
        ]
        unknown = [i or "?" for i in ids if i not in self.known]
        if unknown:
            self.flags.append(
                f"dropped: {title}: unknown pages left out of 'about': " + ", ".join(unknown)[:200]
            )
        return list(dict.fromkeys(i for i in ids if i in self.known))

    def _update(
        self, item: dict[str, Any], material: Material, page: Page, downgrade: bool
    ) -> Proposal:
        fm = page.frontmatter
        speculative = fm.get("speculative") is True
        was = str(fm.get("confidence") or "low")
        what = "downgrade" if downgrade else "update"
        try:
            confidence = _confidence(item)
            reason = _reason(item, required=downgrade)
            supports = (
                []
                if downgrade
                else evidence(item.get("supports") or None, material, self.dates, "supports", 0)
            )
            counter = evidence(
                item.get("counterexamples") or None, material, self.dates, "counterexamples", 0
            )
        except Invalid as exc:
            raise Invalid(f"{what} {page.id}: {exc}") from exc
        old_s = current(entries(page, "supports"), self.by_id)
        old_c = current(entries(page, "counterexamples"), self.by_id)
        new_s, new_c = _evidence(supports), _evidence(counter)
        all_s, all_c = inference.merged(old_s, new_s), inference.merged(old_c, new_c)
        quotes = {(e["page"], e["quote"]) for e in all_s}
        if any((e["page"], e["quote"]) in quotes for e in all_c):
            raise Invalid(f"{what} {page.id}: a line is both a support and a counterexample")
        added_s = [e for e in new_s if e.entry() not in old_s]
        added_c = [e for e in new_c if e.entry() not in old_c]
        if downgrade:
            if not above(was, confidence) or was not in CONFIDENCES:
                raise Invalid(f"downgrade {page.id}: {confidence} is not lower than {was}")
        elif not added_s and not added_c:
            raise Invalid(f"update {page.id}: no new supporting line or counterexample")
        final = inference.capped(confidence, all_s, all_c, speculative)
        if final != confidence:
            self.flags.append(
                f"capped: {what} {page.id}: confidence {confidence} lowered to {final}, "
                "what its supports allow"
            )
        if downgrade:
            history = f"downgraded by dream {self.dream} to {final}: {reason}"
            summary = f"Induction: downgrade {page.title} to {final}"
        else:
            parts = []
            if added_s:
                parts.append(f"{len(added_s)} new supporting fact(s)")
            if added_c:
                parts.append(f"{len(added_c)} new counterexample(s)")
            history = f"updated by dream {self.dream} ({', '.join(parts)}; confidence {final})"
            summary = f"Induction: update {page.title}"
        try:
            op = inference.update_op(
                page=page.id,
                frontmatter=fm,
                body=page.body,
                supports=all_s,
                counterexamples=all_c,
                added=added_s,
                added_counter=added_c,
                confidence=final,
                history=history + ".",
                at=self.at,
            )
        except ValueError as exc:
            raise Invalid(f"{what} {page.id}: {exc}") from exc
        refs = ", ".join(s.ref for s in [*supports, *counter])
        why = f"Dream {self.dream} induction: {what} {page.id}" + (f" with {refs}" if refs else "")
        return Proposal(summary, why + (f": {reason}" if reason else ""), [op], speculative)

    def _retire(self, item: dict[str, Any], page: Page) -> Proposal:
        try:
            reason = _reason(item, required=True)
        except Invalid as exc:
            raise Invalid(f"retire {page.id}: {exc}") from exc
        note = f"Retired by dream {self.dream} on {self.at:%Y-%m-%d}: {reason}"
        return Proposal(
            f"Induction: retire {page.title}",
            f"Dream {self.dream} induction: retire {page.id}: {reason}",
            [{"op": "deprecate", "page": page.id, "note": note}],
            page.frontmatter.get("speculative") is True,
        )


# --- the wiki ---------------------------------------------------------------------------


async def existing_inferences(wiki: WikiReader) -> dict[str, Page]:
    """The inference pages in force (status stable), by id; not the personal patterns, which
    only the patterns pass writes (dream/patterns.py)."""
    out: dict[str, Page] = {}
    for d in await wiki.docs(type="inference"):
        did = str(d.get("id", ""))
        if not is_inference_id(did) or d.get("status") not in (None, "stable"):
            continue
        if is_pattern_id(did):
            continue
        page = await read(wiki, did)
        if page is not None and page.type == "inference":
            out[did] = page
    return out


async def prepare(wiki: WikiReader, reviewed: list[Page], dream: int) -> Induction:
    """What the checks need for one dream: the existing inferences and the wiki's index."""
    by_id = {p.id: p for p in reviewed}
    existing = await existing_inferences(wiki)
    docs = (await wiki.index()).get("docs", [])
    ids = {str(d.get("id", "")) for d in docs}
    return Induction(
        dates=Dates(wiki, raw_ids_by_stem(docs)),
        by_id=by_id,
        existing=existing,
        known={i for i in ids if i.startswith("wiki/") and not is_inference_id(i)},
        taken=set(ids),
        dream=dream,
        at=dt.datetime.now(dt.UTC),
    )
