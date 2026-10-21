"""Personal patterns: hypotheses about how the owner tends to act, drawn by the patterns pass
(dream/patterns.py) from observations (wiki/observations.py) only, never from facts or other
inferences. These are the rules the store holds every pattern page to (and lint checks again):

  page        wiki/inferred/patterns/<slug>: type inference, kind pattern, always private, about
              wiki/owner/personal, written whole by the server (write_pattern). Of a pass's
              operation only the title, hypothesis, contexts, valence, the supports and
              counterevidence (observation refs, "wiki/observations/<x>#o3"), the alternative
              explanations, the requested span and confidence and the model's reasoning are
              used; everything else, the body included, is computed here from the observation
              pages. A ref to anything but an existing observation is refused.
  events      observations are clustered into events (clusters): those of one conversation
              (continuations included) with the same event key; and any whose event summaries'
              word sets overlap (Jaccard) by at least JACCARD, when their dates are compatible
              (the same day or month, one within the other, or one unknown). A pattern's
              independent events are the clusters among its supports: repeated accounts of one
              event count once.
  span        tendency needs at least 2 independent events whose dates are TENDENCY_DAYS or more
              apart; anything less is a state
  confidence  capped by pattern_ceiling: high needs 4+ independent events in 2+ contexts over
              90+ days, no counterevidence and no dispute; medium 3+ events from 2+ chats; else
              low. A disputed pattern is low. The page's confidence_rationale says why.
  wording     no diagnoses or personality labels: a title or hypothesis naming one of CLINICAL
              is refused (a small, deliberate list; the prompt forbids them in general)
  feedback    the owner's verdicts (agree, dispute, correct, unsure) with a note, in
              `feedback` and the "Owner feedback" section. Kept verbatim whatever a pass writes;
              a pattern with feedback is never retired; while the latest verdict is a dispute it
              stays `status: disputed`; the latest correction is its displayed hypothesis (the
              model's wording is kept as model_hypothesis). Feedback is never evidence.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from wiki_mcp.wiki.facts import BANNER, CONFIDENCES, above, is_inference_id
from wiki_mcp.wiki.observations import CONTEXTS, VALENCES
from wiki_mcp.wiki.paths import slugify

PATTERN_FOLDER = "wiki/inferred/patterns"
PERSONAL = "wiki/owner/personal"
JACCARD = 0.5
TENDENCY_DAYS = 14
HIGH_DAYS = 90
REASSESS_DAYS = 90
VERDICTS = ("agree", "dispute", "correct", "unsure")
STATUSES = ("stable", "disputed", "deprecated")
MAX_ALTERNATIVES = 5
MAX_REFS = 30
NOTE_CHARS = 1000
# Diagnoses and personality labels: a pattern describes behaviour in a context, never these.
CLINICAL = (
    "disorder",
    "adhd",
    "narcissis",
    "depress",
    "autis",
    "asperger",
    "bipolar",
    "borderline",
    "ocd",
    "ptsd",
    "psychopath",
    "sociopath",
    "schizo",
    "diagnos",
    "neurotic",
    "introvert",
    "extrovert",
    "extravert",
    "personality type",
)
_CLINICAL = re.compile(r"(?<![a-z])(" + "|".join(re.escape(t) for t in CLINICAL) + ")", re.I)
_REF = re.compile(r"^(wiki/observations/[a-z0-9][a-z0-9-]{0,160})#(o\d{1,3})$")
_WORD = re.compile(r"[^\W\d_]{3,}|\d{2,}", re.UNICODE)
_STOP = frozenset(
    [
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "about",
        "into",
        "over",
        "after",
        "before",
        "when",
        "then",
        "than",
        "they",
        "them",
        "his",
        "her",
        "their",
        "owner",
        "was",
        "were",
        "has",
        "have",
        "had",
        "not",
        "but",
        "are",
        "its",
        "what",
        "which",
        "who",
        "how",
        "why",
    ]
)


class PatternError(ValueError):
    """A pattern page may not be written as asked; the message says why."""


def is_pattern_id(pid: str) -> bool:
    return pid.strip("/").removesuffix(".md").startswith(PATTERN_FOLDER + "/")


def clinical(text: str) -> str | None:
    """The first diagnosis or personality label `text` names, if any."""
    m = _CLINICAL.search(text)
    return m.group(1).lower() if m else None


# --- evidence -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Obs:
    """One observation as a pattern may cite it."""

    ref: str  # wiki/observations/<x>#o3
    page: str
    oid: str
    conversation: str  # the chat's key (continuations share it)
    source: str
    kind: str
    basis: str
    context: str
    valence: str
    span: str
    text: str
    quote: str
    msg: int
    date: str  # YYYY-MM-DD of the cited message ("" when the transcript has none)
    summary: str  # of its event
    when: str  # the event's date as reported: YYYY-MM-DD, YYYY-MM or unknown
    key: str  # the event's key within its conversation


def observations_of(page: str, fm: dict[str, Any]) -> list[Obs]:
    """The observations on one observation page (its id and frontmatter)."""
    out = []
    for o in fm.get("observations") or []:
        if not isinstance(o, dict) or not isinstance(o.get("id"), str):
            continue
        msgs = [m for m in o.get("msgs") or [] if isinstance(m, int)]
        raw_event = o.get("event")
        event: dict[str, Any] = raw_event if isinstance(raw_event, dict) else {}
        at = str(o.get("message_at") or fm.get("messages_from") or "")
        out.append(
            Obs(
                ref=f"{page}#{o['id']}",
                page=page,
                oid=o["id"],
                conversation=str(fm.get("conversation") or page),
                source=str(fm.get("source") or ""),
                kind=str(o.get("kind") or ""),
                basis=str(o.get("basis") or ""),
                context=str(o.get("context") or "other"),
                valence=str(o.get("valence") or "neutral"),
                span=str(o.get("span") or "unknown"),
                text=str(o.get("text") or ""),
                quote=str(o.get("quote") or ""),
                msg=msgs[0] if msgs else 0,
                date=at[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", at) else "",
                summary=str(event.get("summary") or o.get("text") or ""),
                when=str(event.get("when") or "unknown"),
                key=str(event.get("key") or ""),
            )
        )
    return out


def _tokens(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(text.lower()) if w not in _STOP)


def _compatible(a: str, b: str) -> bool:
    return a == "unknown" or b == "unknown" or a.startswith(b) or b.startswith(a)


def events(obs: list[Obs]) -> dict[str, str]:
    """Each observation's event cluster id (see the module docstring), by its ref."""
    units: dict[tuple[str, str], list[Obs]] = {}
    for o in obs:
        units.setdefault((o.conversation, o.key or "#" + o.ref), []).append(o)
    keys = sorted(units)
    parent = list(range(len(keys)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    tokens = [frozenset(w for o in units[k] for w in _tokens(o.summary)) for k in keys]
    whens = [next((o.when for o in units[k] if o.when != "unknown"), "unknown") for k in keys]
    by_token: dict[str, list[int]] = {}
    for i, words in enumerate(tokens):
        for w in words:
            by_token.setdefault(w, []).append(i)
    for i, words in enumerate(tokens):
        seen = {j for w in words for j in by_token[w] if j > i}
        for j in sorted(seen):
            if not _compatible(whens[i], whens[j]):
                continue
            overlap = len(words & tokens[j]) / len(words | tokens[j])
            if overlap >= JACCARD:
                parent[root(j)] = root(i)
    members: dict[int, list[int]] = {}
    for i in range(len(keys)):
        members.setdefault(root(i), []).append(i)
    out: dict[str, str] = {}
    for group in members.values():
        first = min("\0".join(keys[i]) for i in group)
        cid = "ev-" + hashlib.sha256(first.encode()).hexdigest()[:8]
        for i in group:
            for o in units[keys[i]]:
                out[o.ref] = cid
    return out


def quarter(date: str) -> str:
    if not re.match(r"^\d{4}-\d{2}", date):
        return "unknown"
    return f"{date[:4]}-Q{(int(date[5:7]) - 1) // 3 + 1}"


def days_between(dates: Iterable[str]) -> int:
    """Days from the earliest to the latest of `dates` (YYYY-MM-DD; others are ignored)."""
    known: list[dt.date] = []
    for d in dates:
        with contextlib.suppress(ValueError):
            known.append(dt.date.fromisoformat(d[:10]))
    known.sort()
    return (known[-1] - known[0]).days if len(known) > 1 else 0


def coverage(obs: list[Obs], clusters: dict[str, str]) -> dict[str, Any]:
    """How much evidence there is, from when and about what: observations and distinct events
    per context, per quarter and per valence, and warnings where it is skewed."""

    def tally(key: str) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, Any]] = {}
        for o in obs:
            group = quarter(o.date) if key == "quarter" else str(getattr(o, key))
            entry = counts.setdefault(group, {"observations": 0, "events": set()})
            entry["observations"] += 1
            entry["events"].add(clusters.get(o.ref, o.ref))
        return {
            g: {"observations": c["observations"], "events": len(c["events"])}
            for g, c in sorted(counts.items())
        }

    dates = [o.date for o in obs if o.date]
    contexts, valence = tally("context"), tally("valence")
    total = len({clusters.get(o.ref, o.ref) for o in obs})
    skew: list[str] = []
    if total:
        top, most = max(contexts.items(), key=lambda kv: kv[1]["events"])
        if len(contexts) > 1 and most["events"] / total > 0.5:
            skew.append(f"{round(100 * most['events'] / total)}% of events are about {top}")
        hard = valence.get("difficulty", {}).get("events", 0)
        if hard / total >= 0.6:
            skew.append(
                f"{round(100 * hard / total)}% of events are difficulties: people bring problems "
                "to assistants, so strengths are under-represented"
            )
        if total < 10:
            skew.append(f"only {total} events so far: every pattern is tentative")
    span = days_between(dates)
    if dates and span < HIGH_DAYS:
        skew.append(f"the evidence spans only {span} days")
    return {
        "observations": len(obs),
        "events": total,
        "chats": len({o.conversation for o in obs}),
        "from": min(dates) if dates else None,
        "to": max(dates) if dates else None,
        "contexts": contexts,
        "quarters": tally("quarter"),
        "valence": valence,
        "skew": skew,
    }


@dataclass
class Evidence:
    """Every observation in the wiki, clustered into events, and the coverage."""

    by_ref: dict[str, Obs]
    clusters: dict[str, str]
    coverage: dict[str, Any]


def evidence(pages: Iterable[tuple[str, dict[str, Any]]]) -> Evidence:
    """The evidence from observation pages (id, frontmatter)."""
    obs = [o for pid, fm in pages for o in observations_of(pid, fm)]
    clusters = events(obs)
    return Evidence({o.ref: o for o in obs}, clusters, coverage(obs, clusters))


# --- the rules ----------------------------------------------------------------------------


def span_of(requested: Any, dates: list[str]) -> str:
    """tendency only when asked for and at least 2 independent events (their dates) are
    TENDENCY_DAYS or more apart; otherwise state."""
    if requested == "tendency" and len(dates) >= 2 and days_between(dates) >= TENDENCY_DAYS:
        return "tendency"
    return "state"


def pattern_ceiling(
    events: int, chats: int, contexts: int, days: int, counter: int, disputed: bool
) -> tuple[str, str]:
    """The highest confidence the evidence allows, and why."""
    if disputed:
        return "low", "disputed by the owner"
    if events >= 4 and contexts >= 2 and days >= HIGH_DAYS and not counter:
        return "high", "4+ independent events in 2+ contexts over 90+ days, no counterevidence"
    if events >= 3 and chats >= 2:
        why = "high needs 4+ events in 2+ contexts over 90+ days and no counterevidence"
        return "medium", why
    return "low", "medium needs 3+ independent events from 2+ chats"


def disputed(feedback: list[dict[str, Any]]) -> bool:
    """Whether the owner's latest verdict is a dispute."""
    return bool(feedback) and feedback[-1].get("verdict") == "dispute"


def correction(feedback: list[dict[str, Any]]) -> str | None:
    """The owner's latest corrected wording, if any."""
    for entry in reversed(feedback):
        if entry.get("verdict") == "correct" and str(entry.get("note") or "").strip():
            return str(entry["note"]).strip()
    return None


def feedback_of(fm: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(e) for e in fm.get("feedback") or [] if isinstance(e, dict)]


def _text(value: Any, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _refs(value: Any, what: str, ev: Evidence) -> list[Obs]:
    if not isinstance(value, list) or len(value) > MAX_REFS:
        raise PatternError(f"'{what}' must be a list of at most {MAX_REFS} observation refs")
    out: list[Obs] = []
    for item in value:
        ref = str(item.get("ref") if isinstance(item, dict) else item or "").strip().strip("/")
        if is_inference_id(ref):
            raise PatternError(f"{what}: rests on an inference ({ref[:100]}), never evidence")
        m = _REF.match(ref.replace(".md#", "#"))
        if m is None or f"{m.group(1)}#{m.group(2)}" not in ev.by_ref:
            raise PatternError(f"{what}: {ref[:100]} is not an existing observation")
        obs = ev.by_ref[f"{m.group(1)}#{m.group(2)}"]
        if obs not in out:
            out.append(obs)
    return out


def _entry(o: Obs, ev: Evidence) -> dict[str, Any]:
    return {
        "page": o.page,
        "obs": o.oid,
        "quote": o.quote,
        "source": o.source,
        "msg": o.msg,
        "date": o.date,
        "event": ev.clusters.get(o.ref, o.ref),
        "basis": o.basis,
        "kind": o.kind,
        "context": o.context,
        "valence": o.valence,
        "text": o.text,
    }


def _event_dates(supports: list[Obs], ev: Evidence) -> list[str]:
    """The earliest date of each independent event among the supports."""
    first: dict[str, str] = {}
    for o in supports:
        cid = ev.clusters.get(o.ref, o.ref)
        if o.date and (cid not in first or o.date < first[cid]):
            first[cid] = o.date
    return sorted(first.values())


def build(
    pid: str,
    fm_in: dict[str, Any],
    old: dict[str, Any] | None,
    history: list[str],
    ev: Evidence,
    dream: int,
    today: dt.date,
) -> tuple[dict[str, Any], str]:
    """A pattern page's frontmatter (without the server's generated, trust, created and
    updated) and body, from a write_pattern operation's frontmatter and the page as it was
    (`old`, None for a new one; `history`: its History lines). PatternError when refused."""
    if not is_pattern_id(pid):
        raise PatternError(f"{pid}: patterns live under {PATTERN_FOLDER}/")
    old = old or {}
    if old and old.get("kind") != "pattern":
        raise PatternError(f"{pid} exists and is not a pattern")
    feedback = feedback_of(old)
    status_in = fm_in.get("status", "stable")
    if status_in not in ("stable", "deprecated"):
        raise PatternError(f"{pid}: status must be stable or deprecated")
    if status_in == "deprecated":
        if not old:
            raise PatternError(f"{pid}: only an existing pattern is retired")
        if feedback:
            raise PatternError(f"{pid}: a pattern with owner feedback is never retired")
        retired = {**old, "status": "deprecated", "dream": dream}
        why = _text(fm_in.get("reasoning"), 300) or "no longer supported"
        return retired, render(retired, [*history, f"- {today}: retired by dream {dream}: {why}"])
    title, hypothesis = _text(fm_in.get("title"), 120), _text(fm_in.get("hypothesis"), 500)
    if not title or not hypothesis:
        raise PatternError(f"{pid}: a pattern needs a title and a hypothesis")
    for text in (title, hypothesis):
        if (term := clinical(text)) is not None:
            raise PatternError(f"{pid}: no diagnoses or personality labels ({term})")
    supports = _refs(fm_in.get("supports"), "supports", ev)
    counter = _refs(fm_in.get("counterevidence") or [], "counterevidence", ev)
    if len(supports) < 2:
        raise PatternError(f"{pid}: a pattern needs at least 2 supporting observations")
    if set(supports) & set(counter):
        raise PatternError(f"{pid}: an observation cannot both support and counter it")
    raw_alt = fm_in.get("alternatives")
    alternatives = [a for a in (_text(x, 300) for x in raw_alt or []) if a][:MAX_ALTERNATIVES]
    if not isinstance(raw_alt, list) or not alternatives:
        raise PatternError(f"{pid}: give at least one alternative explanation")
    contexts = [c for c in fm_in.get("contexts") or [] if c in CONTEXTS]
    contexts = list(dict.fromkeys(contexts or [o.context for o in supports]))
    valence = fm_in.get("valence")
    if valence not in VALENCES:
        valence = Counter(o.valence for o in supports).most_common(1)[0][0]
    clusters = {ev.clusters.get(o.ref, o.ref) for o in supports}
    chats = {o.conversation for o in supports}
    seen = {o.context for o in supports}
    dates = _event_dates(supports, ev)
    days = days_between(dates)
    span = span_of(fm_in.get("span"), dates)
    is_disputed = disputed(feedback)
    ceiling, why = pattern_ceiling(
        len(clusters), len(chats), len(seen), days, len(counter), is_disputed
    )
    asked = fm_in.get("confidence")
    confidence = asked if asked in CONFIDENCES and not above(asked, ceiling) else ceiling
    corrected = correction(feedback)
    shown = corrected or hypothesis
    reasoning = _text(fm_in.get("reasoning"), 600)
    rationale = (
        f"{len(clusters)} independent event(s) from {len(chats)} chat(s) in {len(seen)} "
        f"context(s) over {days} day(s); {len(counter)} counterevidence. At most {ceiling}: "
        f"{why}." + (f" Reasoning: {reasoning}" if reasoning else "")
    )
    fm: dict[str, Any] = {
        "type": "inference",
        "id": pid,
        "title": _short(corrected, 120) if corrected else title,
        "description": _short(shown, 500),
        "tags": ["inferred", "pattern"],
        "status": "disputed" if is_disputed else "stable",
        "private": True,
        "kind": "pattern",
        "confidence": confidence,
        "speculative": False,
        "about": [PERSONAL],
        "hypothesis": shown,
        **({"model_hypothesis": hypothesis} if corrected else {}),
        "contexts": contexts,
        "span": span,
        "valence": valence,
        "supports": [_entry(o, ev) for o in supports],
        "counterevidence": [_entry(o, ev) for o in counter],
        "alternatives": alternatives,
        "independent_events": len(clusters),
        "chats": len(chats),
        "span_days": days,
        "confidence_rationale": rationale,
        "coverage": ev.coverage,
        "feedback": feedback,
        "reassess_after": (today + dt.timedelta(days=REASSESS_DAYS)).isoformat(),
        "dream": dream,
    }
    what = "updated" if old else "created"
    line = f"- {today}: {what} by dream {dream} (confidence {confidence}, {len(clusters)} events)."
    return fm, render(fm, [*history, line])


def with_feedback(
    fm: dict[str, Any], verdict: str, note: str, at: str, history: list[str]
) -> tuple[dict[str, Any], str]:
    """A pattern page with one more verdict of the owner's: disputed while the latest verdict
    is a dispute (confidence low), the corrected wording shown after a correction."""
    note = " ".join(note.split())[:NOTE_CHARS]
    if verdict not in VERDICTS:
        raise PatternError(f"'verdict' must be one of {', '.join(VERDICTS)}")
    if verdict == "correct" and len(note) < 3:
        raise PatternError("a correction needs the corrected wording in the note")
    if fm.get("kind") != "pattern":
        raise PatternError("not a pattern")
    fm = {**fm, "feedback": [*feedback_of(fm), {"at": at, "verdict": verdict, "note": note}]}
    if verdict == "dispute":
        fm["status"], fm["confidence"] = "disputed", "low"
    elif fm.get("status") == "disputed":
        fm["status"] = "stable"  # the owner changed his verdict; the next pass rechecks
    if verdict == "correct":
        fm["model_hypothesis"] = fm.get("model_hypothesis") or fm.get("hypothesis")
        fm["hypothesis"], fm["title"] = note, _short(note, 120)
        fm["description"] = _short(note, 500)
    return fm, render(fm, [*history, f"- {at[:10]}: owner feedback: {verdict}."])


# --- the body -----------------------------------------------------------------------------


def _evidence_line(e: dict[str, Any]) -> str:
    return (
        f"- [{e.get('obs')}](/{e.get('page')}.md) m{e.get('msg')}, {e.get('date') or 'undated'} "
        f"({e.get('basis')} {e.get('kind')}, {e.get('context')}, {e.get('valence')}; event "
        f'{e.get("event")}): {e.get("text")} "{e.get("quote")}" (source `{e.get("source")}`)'
    )


def render(fm: dict[str, Any], history: list[str]) -> str:
    """The body of a pattern page, from its frontmatter and its History lines."""
    cov = fm.get("coverage") or {}
    lines = [
        f"# {fm.get('title')}",
        "",
        f"{BANNER} A hypothesis about a personal pattern, drawn by a dream from observations of "
        "the owner's own messages: not a fact and not a diagnosis. Confidence: "
        f"{fm.get('confidence')} ({fm.get('confidence_rationale')})",
        "",
        str(fm.get("hypothesis") or ""),
        "",
        f"Contexts: {', '.join(fm.get('contexts') or [])}. Span: {fm.get('span')}. "
        f"Valence: {fm.get('valence')}. Status: {fm.get('status')}. "
        f"Reassess after {fm.get('reassess_after')}.",
        "",
        "## Supporting evidence",
        "",
        *(_evidence_line(e) for e in fm.get("supports") or []),
        "",
        "## Counterevidence",
        "",
        *([_evidence_line(e) for e in fm.get("counterevidence") or []] or ["None found."]),
        "",
        "## Alternative explanations",
        "",
        *(f"- {a}" for a in fm.get("alternatives") or []),
        "",
        "## Coverage",
        "",
        f"- {cov.get('observations', 0)} observations, {cov.get('events', 0)} events from "
        f"{cov.get('chats', 0)} chats, {cov.get('from') or '?'} to {cov.get('to') or '?'}.",
        *(f"- {s}" for s in cov.get("skew") or []),
        "",
        "## Owner feedback",
        "",
        *(
            [
                f"- {str(f.get('at'))[:10]}: {f.get('verdict')}"
                + (f": {f['note']}" if f.get("note") else "")
                for f in fm.get("feedback") or []
            ]
            or ["None yet."]
        ),
    ]
    if fm.get("model_hypothesis"):
        lines += [
            "",
            f"The dream's wording, before the owner's correction: {fm['model_hypothesis']}",
        ]
    lines += ["", "## History", "", *history]
    return "\n".join(lines) + "\n"


CAVEATS = (
    "These are inferred hypotheses about the owner, drawn by a dream from observations of his "
    "own messages in chats with assistants: not facts, and never diagnoses. Missing evidence "
    "means unknown, not no. Answer balanced: what the owner states about himself "
    "(wiki/owner/profile) first; then these as hypotheses, each with its confidence, its "
    "counterevidence and the coverage caveats below; strengths as well as difficulties. His "
    "own feedback on a pattern outranks the pattern."
)
SHOWN_EVIDENCE = 3
_GROUPS = (("strength", "Strengths"), ("difficulty", "Difficulties"), ("neutral", "Other patterns"))


def _cited(e: dict[str, Any]) -> str:
    return (
        f'  - "{e.get("quote")}" (`{e.get("source")}`, message {e.get("msg")}, '
        f"{e.get('date') or 'undated'}, {e.get('basis')})"
    )


def answer(pages: list[tuple[str, dict[str, Any]]], context: str | None, observations: int) -> str:
    """get_personal_patterns: the caveats, the coverage, and the patterns in force (strengths,
    difficulties, others), each ordered by independent events and how many contexts they
    span, never by how often something was mentioned."""
    live = [
        (pid, fm)
        for pid, fm in pages
        if fm.get("status") != "deprecated"
        and (context is None or context in (fm.get("contexts") or []))
    ]

    def order(page: tuple[str, dict[str, Any]]) -> tuple[int, int, str]:
        meta = page[1]
        events, contexts = meta.get("independent_events") or 0, meta.get("contexts") or []
        return (-int(events), -len(contexts), page[0])

    live.sort(key=order)
    newest = max((fm for _, fm in pages), key=lambda fm: str(fm.get("updated") or ""), default={})
    cov = newest.get("coverage") or {}
    lines = ["# Personal patterns", "", CAVEATS, "", "## Coverage", ""]
    lines.append(
        f"- {observations} chats observed so far. At the last reassessment: "
        f"{cov.get('observations', 0)} observations, {cov.get('events', 0)} independent events "
        f"from {cov.get('chats', 0)} chats, {cov.get('from') or '?'} to {cov.get('to') or '?'}."
    )
    contexts = cov.get("contexts") or {}
    if contexts:
        lines.append(
            "- Events per context: "
            + ", ".join(f"{c} {n.get('events', 0)}" for c, n in contexts.items())
        )
    lines += [f"- Caveat: {s}" for s in cov.get("skew") or []]
    if not live:
        what = f" in the context {context}" if context else ""
        lines += ["", f"No personal patterns{what} yet: say that this is unknown."]
        return "\n".join(lines) + "\n"
    for valence, heading in _GROUPS:
        group = [(pid, fm) for pid, fm in live if (fm.get("valence") or "neutral") == valence]
        lines += ["", f"## {heading}", ""]
        if not group:
            lines.append("None found yet (unknown, not absent).")
        for pid, fm in group:
            status = " (disputed by the owner)" if fm.get("status") == "disputed" else ""
            lines += [
                f"### {fm.get('hypothesis')}{status}",
                "",
                f"- Page: {pid}. Confidence: {fm.get('confidence')}: "
                f"{fm.get('confidence_rationale')}",
                f"- Independent events: {fm.get('independent_events')}; contexts: "
                f"{', '.join(fm.get('contexts') or [])}; span: {fm.get('span')}; reassess after "
                f"{fm.get('reassess_after')}.",
                "- Evidence:",
                *(_cited(e) for e in (fm.get("supports") or [])[:SHOWN_EVIDENCE]),
                "- Counterevidence:"
                + ("" if fm.get("counterevidence") else " none found (which is not proof)"),
                *(_cited(e) for e in fm.get("counterevidence") or []),
                "- Alternative explanations: " + "; ".join(fm.get("alternatives") or []),
            ]
            for f in fm.get("feedback") or []:
                note = f": {f['note']}" if f.get("note") else ""
                lines.append(f"- Owner feedback {str(f.get('at'))[:10]}: {f.get('verdict')}{note}")
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def slug_id(title: str, taken: set[str]) -> str:
    """A new pattern page id for the title, not in `taken` (which it is added to)."""
    base = f"{PATTERN_FOLDER}/{slugify(title)}"
    pid, n = base, 1
    while pid in taken:
        n += 1
        pid = f"{base}-{n}"
    taken.add(pid)
    return pid
