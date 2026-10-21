"""An assistant dream's passes, as the MCP tools drive them (mcp/dream_tools.py): the
assistant's model answers each pass's prompt itself, and wiki-mcp does what the portal's pass
does with a model's answer (portal/dreams/{merge,deduction,induction,hot_memory}.py), with the
same shared code, so the checks, proposals, commits and log lines are the same.

  material  dream_material: one part of a pass (D3's windows, D5's briefings; one for the
            others): its system prompt and material exactly as the portal sends them, the
            limits the checks enforce, and the passes still to do. A pass with nothing to do
            is skipped there, with the portal's skip text in the log.
  submit    dream_submit: the answer to that part, parsed with the pass's parse (an
            unparsable answer is refused, not counted), checked and proposed as the portal
            does; D5 commits the briefing once its last part is in. What was kept, dropped
            (the portal's log strings) and refused by the wiki is the tool's answer.
  finish    dream_finish: D6, the log (dream/assistant.py close); a pass partly done is
            logged, and a briefing made so far is committed first.

The passes go in the order of sessions.PASSES, a part at a time: each part is submitted once;
a pass cannot follow a later one; getting material for or submitting a later pass skips the
earlier ones not done ("skipped by the assistant"). The session keeps, per part, a fingerprint
of the material handed out: a submission rebuilds the material (its answer refers to lines by
page and number) and is refused when it changed in between, e.g. a page was edited or a
proposal accepted; material from dream_material again then.

An app without wiki:private reads the wiki as the MCP tools show it (a LocalReader with
private=False): no private page in any pass's material, so no answer can refer to one, and
no private briefing (D5 has one part; the private briefing page is left as it is). Which
access the material is read with is fixed at the dream's first dream_material; the log says
when private pages were left out. What the tools answer such an app is filtered as well
(Hidden): the session also holds what the hygiene fixes at the opening found with every page
in view, and the log page (private while the wiki has private pages) keeps all of it. Every
write happens under the sessions' lock.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

from wiki_mcp.dream import Unparsable, deduction, hot_memory, induction, merge, prompts
from wiki_mcp.dream import assistant as dream_assistant
from wiki_mcp.dream.limits import MAX_CHARS
from wiki_mcp.dream.local import LocalReader
from wiki_mcp.dream.pages import Page, reviewed_pages
from wiki_mcp.dream.sessions import PASSES, DreamSessions, Session, SessionError, iso
from wiki_mcp.wiki.facts import bullet_lines
from wiki_mcp.wiki.okf import parse_document
from wiki_mcp.wiki.store import WikiStore

# The briefing's size cap; the portal's is HOT_MEMORY_MAX_CHARS (the same default).
HOT_MEMORY_MAX_CHARS = 6000
MAX_ANSWER = 200_000  # characters of a submitted answer
NO_PRIVATE = "private pages left out: the app has no private access"
INSTRUCTIONS = (
    "Answer with only the JSON object the prompt asks for, then call dream_submit with it "
    "(the same dream, pass_name and window)."
)
# The name each pass has in the log, its proposals and commits (merges are D4's)
LOGGED = {
    "duplicates": merge.NAME,
    "deduction": deduction.NAME,
    "induction": induction.NAME,
    "hot-memory": hot_memory.NAME,
}
PART = {"induction": "window", "hot-memory": "part"}  # what a pass's parts are called
PRIVATE = "(a private page)"  # what stands in a message for what an app may not see
QUOTED = 12  # characters a private page's line needs to be looked for in a text


class Refused(SessionError):
    """A tool call refused before anything was written; the message says what to do."""


@dataclass
class Skip:
    """A pass with nothing to do: the portal's skip text (None: the portal logs nothing),
    and what the assistant is told."""

    logged: str | None
    say: str
    flags: list[str] = field(default_factory=list)  # logged with it (the portal's flags)


@dataclass
class Part:
    """One part of a pass's material, as built now."""

    prompt: str
    text: str
    parts: int  # how many parts the pass has
    limits: dict[str, Any]
    data: Any = None  # what the checks need

    @property
    def fingerprint(self) -> str:
        blob = f"{self.parts}\0{self.prompt}\0{self.text}".encode()
        return hashlib.sha256(blob).hexdigest()


@dataclass
class Hidden:
    """What an app without wiki:private may not be shown in a tool's answer: the ids and
    titles of private documents, and the lines of private wiki pages. A list loses the
    entries naming any of them (and says how many); any other text has them replaced."""

    ids: set[str]
    titles: list[re.Pattern[str]]
    lines: set[str]

    @classmethod
    def of(cls, store: WikiStore, *, but: str = "") -> Hidden:
        """For the wiki as it is now; `but`: text the app sent itself (the answer), whose
        mentions tell it nothing new, so a check that only echoes it is kept as it is."""
        ids: set[str] = set()
        titles: list[re.Pattern[str]] = []
        lines: set[str] = set()
        for row in store.index.all():
            if not row.private:
                continue
            if row.id not in but:
                ids.add(row.id)
            title = " ".join((row.title or "").split())
            if len(title) >= 3 and title not in but:
                titles.append(re.compile(rf"(?<!\w){re.escape(title)}(?!\w)"))
            if row.path.startswith("wiki/"):
                try:
                    body = parse_document((store.root / row.path).read_text("utf-8")).body
                except (OSError, ValueError):
                    continue
                lines |= {t for line in bullet_lines(body) if len(t := line.strip()) >= QUOTED}
        return cls(ids, titles, lines)

    def names(self, text: str) -> bool:
        return (
            any(i in text for i in self.ids)
            or any(t.search(text) for t in self.titles)
            or any(line in text for line in self.lines)
        )

    def text(self, text: str) -> str:
        for line in self.lines:
            text = text.replace(line, PRIVATE)
        for i in sorted(self.ids, key=len, reverse=True):
            text = text.replace(i, PRIVATE)
        for title in self.titles:
            text = title.sub(PRIVATE, text)
        return text

    def value(self, value: Any, keep: tuple[str, ...] = ()) -> Any:
        """A tool's answer as the app may see it; the keys in `keep` stay as they are (the
        material, read without private pages already)."""
        if isinstance(value, str):
            return self.text(value) if self.names(value) else value
        if isinstance(value, dict):
            out = {k: v if k in keep else self.value(v) for k, v in value.items() if k != "pages"}
            if isinstance(value.get("pages"), list):  # a commit's pages: counted, not listed
                shown = [p for p in value["pages"] if not self.names(str(p))]
                if len(shown) < len(value["pages"]):
                    out["private_pages"] = len(value["pages"]) - len(shown)
                out["pages"] = shown
            elif "pages" in value:
                out["pages"] = self.value(value["pages"])
            return out
        if isinstance(value, list):
            shown = [self.value(v) for v in value if not (isinstance(v, str) and self.names(v))]
            left = len(value) - len(shown)
            if left:
                shown.append(f"{left} line(s) about private pages left out")
            return shown
        return value


async def _shown(
    store: WikiStore, private: bool, work: Awaitable[dict[str, Any]], *, but: str = ""
) -> dict[str, Any]:
    """A tool's answer, or its refusal, as the calling app may see it."""
    if private:
        return await work
    try:
        answer = await work
    except ValueError as exc:  # Refused, and any WikiError on the way
        raise Refused(Hidden.of(store, but=but).text(str(exc))) from exc
    # the dream's own log page is private while the wiki has private pages: its id is not
    but += f" {answer.get('log_page') or ''}"
    return dict(Hidden.of(store, but=but).value(answer, keep=("prompt", "material")))


# --- the session ------------------------------------------------------------------------


def _live(sessions: DreamSessions | None) -> DreamSessions:
    if sessions is None:
        raise Refused("assistant dreams are not available on this server")
    return sessions


# Per kind of session: how the owner opens one, and what runs it.
OPENS = {
    "dream": (
        "starts a dream on the portal's Dreams page first (Dream in Claude.ai or ChatGPT)",
        "start a new dream on the portal's Dreams page",
    ),
    "patterns": (
        "opens one on the portal's Patterns page first (Reassess in Claude.ai / ChatGPT)",
        "open a new one on the portal's Patterns page",
    ),
}
TOOLS = {
    "dream": "the dream tools (prompt: run-dream)",
    "patterns": "the patterns tools (prompt: run-patterns)",
}


def _session(sessions: DreamSessions, dream: Any, kind: str = "dream") -> Session:
    """The dream's open session of this kind; Refused when there is none, it is of another
    kind, or it expired or finished."""
    if not isinstance(dream, int) or isinstance(dream, bool) or dream < 1:
        raise Refused("'dream' must be the dream's id, a positive integer")
    session = sessions.get(dream)
    first, again = OPENS[kind]
    if session is None:
        raise Refused(f"dream {dream} has no session: the owner {first} and gives you its id")
    if session.kind != kind:
        raise Refused(
            f"dream {dream} is a {session.kind} session: run it with {TOOLS[session.kind]}"
        )
    status = session.status(sessions.now())
    if status == "expired":
        raise Refused(
            f"the session of dream {dream} expired at {iso(session.expires)}: nothing more "
            f"is written to it; the owner can {again}"
        )
    if status == "finished":
        raise Refused(
            f"the session of dream {dream} is closed already ({session.ended}); the owner can "
            f"{again}"
        )
    return session


def _pass(value: Any) -> str:
    if value not in PASSES:
        raise Refused(f"'pass_name' must be one of {', '.join(PASSES)}")
    return str(value)


def _window(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise Refused("'window' must be the part's number: 0, 1, ...")
    return value


def _complete(session: Session, step: str) -> bool:
    """Every part of the pass was submitted, or it was skipped."""
    return step in session.passes and len(session.passes[step]) >= session.parts.get(step, 0)


def to_do(session: Session) -> list[str]:
    """The passes not done yet, in order (a pass partly done included)."""
    return [p for p in PASSES if not _complete(session, p)]


def _next(session: Session) -> dict[str, Any] | str:
    """What to call next."""
    left = to_do(session)
    if not left:
        return "dream_finish"
    return {"pass_name": left[0], "window": len(session.passes.get(left[0], []))}


def _order(session: Session, step: str, window: int) -> None:
    """Refused unless this part is the pass's next one and no later pass was begun."""
    later = [p for p in PASSES[PASSES.index(step) + 1 :] if p in session.passes]
    if later:
        raise Refused(
            f"out of order: {later[0]} is done or skipped already, and the passes go "
            f"{', '.join(PASSES)}; passes to do: {', '.join(to_do(session)) or 'none'}"
        )
    done = session.passes.get(step, [])
    if _complete(session, step):
        raise Refused(
            f"{step} is done or skipped already (one submission each); passes to do: "
            f"{', '.join(to_do(session)) or 'none, call dream_finish'}"
        )
    if window < len(done):
        raise Refused(f"{step} {PART.get(step, 'part')} {window} was submitted already")
    if window > len(done):
        raise Refused(f"{step}: {PART.get(step, 'part')} {len(done)} comes first")


def _access(session: Session, private: bool) -> None:
    """Fix how the material is read at the dream's first read; Refused when it changed."""
    want = "private" if private else "public"
    if not session.access:
        session.access = want
        if not private:
            session.log.flagged.append(NO_PRIVATE)
    elif session.access != want:
        had = "with" if session.access == "private" else "without"
        now = "has" if private else "lacks"
        raise Refused(
            f"this dream's material was read {had} private pages and this connection {now} "
            "private access: finish the dream from the app that began it"
        )


def _called_from(session: Session, client: str) -> None:
    name = " ".join(str(client).split())[:80] or "unknown"
    if name not in session.run_in:
        session.run_in.append(name)


def _reader(store: WikiStore, session: Session) -> LocalReader:
    return LocalReader(store, private=session.access == "private")


# --- material ---------------------------------------------------------------------------


def _answer_limits(**limits: Any) -> dict[str, Any]:
    return {"notes_chars": deduction.NOTES_CHARS, **limits}


async def _build(wiki: LocalReader, session: Session, step: str, window: int) -> Part | Skip:
    """The part's material as the portal's pass builds it (Refused for a window it lacks)."""
    if step == "duplicates":
        part: Part | Skip = await _duplicates(wiki)
    elif step == "deduction":
        part = await _deduction(wiki, session)
    elif step == "induction":
        part = await _induction(wiki, session, window)
    else:
        part = await _hot_memory(wiki, session)
    if isinstance(part, Part) and window >= part.parts:
        raise Refused(f"{step} has {part.parts} {PART.get(step, 'part')}(s): 0 to {part.parts - 1}")
    return part


async def _duplicates(wiki: LocalReader) -> Part | Skip:
    pages = await reviewed_pages(wiki)
    pairs = merge.candidates(pages)
    if not pairs:  # the portal logs nothing then
        return Skip(None, "no candidate duplicate pairs among the reviewed pages")
    docs = (await wiki.index()).get("docs", [])
    known = {str(d["id"]) for d in docs}
    private = frozenset(str(d["id"]) for d in docs if d.get("private"))
    limits = _answer_limits(
        pairs=len(pairs), pairs_considered=merge.MOST, reason_chars=deduction.REASON_CHARS
    )
    text = merge.material(pairs)
    return Part(prompts.system(prompts.DUPLICATES), text, 1, limits, (pairs, known, private))


async def _deduction(wiki: LocalReader, session: Session) -> Part | Skip:
    found = await deduction.material(wiki, session.since_base)
    if not found.pages:
        why = "nothing changed on reviewed pages since the last dream"
        return Skip(why, why)
    limits = _answer_limits(
        items_per_kind=deduction.MOST,
        reason_chars=deduction.REASON_CHARS,
        followup_chars=deduction.FOLLOWUP_CHARS,
        statement_chars=deduction.STATEMENT_CHARS,
        title_chars=list(deduction.TITLE_CHARS),
        premises_least=2,
    )
    return Part(prompts.system(prompts.DEDUCTION), found.text, 1, limits, found)


@dataclass
class Window:
    """What D3's checks need for one window."""

    material: deduction.Material
    state: induction.Induction
    too_long: list[Page]
    windows: int  # windows found (at most MAX_WINDOWS read)


async def _induction(wiki: LocalReader, session: Session, window: int) -> Part | Skip:
    reviewed = await reviewed_pages(wiki)  # newest first
    if not reviewed:
        return Skip("no reviewed pages", "no reviewed pages")
    found, too_long = induction.windows(reviewed)
    flags = [f"{p.id}: too long to read (over {MAX_CHARS} characters)" for p in too_long]
    if not found:  # the portal logs its flags, and no skip
        return Skip(None, "every reviewed page is too long to read", flags)
    state = await induction.prepare(wiki, reviewed, session.dream)
    block = induction.inferences_block(state.existing, state.by_id)
    parts = min(len(found), induction.MAX_WINDOWS)
    creates = int(session.carry.get("induction", {}).get("creates", 0))
    limits = _answer_limits(
        patterns_per_window=induction.MAX_PATTERNS,
        creates_per_dream=induction.MAX_CREATES,
        creates_left=max(0, induction.MAX_CREATES - creates),
        supports_least=2,
        statement_chars=deduction.STATEMENT_CHARS,
        title_chars=list(deduction.TITLE_CHARS),
        reason_chars=deduction.REASON_CHARS,
    )
    chosen = found[min(window, parts - 1)]
    text = induction.text_of(chosen, block)
    data = Window(chosen, state, too_long, len(found))
    return Part(prompts.system(prompts.INDUCTION), text, parts, limits, data)


@dataclass
class Briefings:
    """D5's parts: the public briefing (when there is material for it) and, with private
    access and when its material differs, the private one."""

    variants: list[tuple[str, str, dict[str, Page]]]  # (public|private, material, known)
    same: bool  # the private variant's material is the public one's


async def _hot_memory(wiki: LocalReader, session: Session) -> Part | Skip:
    reviewed = await reviewed_pages(wiki)  # read once for both variants
    user, known = await hot_memory.material(wiki, reviewed=reviewed)
    if session.access == "private":
        full_user, full_known = await hot_memory.material(wiki, private=True, reviewed=reviewed)
        same = full_user == user
    else:  # no private variant: the private briefing page is left as it is
        full_user, full_known, same = user, known, False
    if not full_known:
        why = "nothing to summarise: no owner profile and no reviewed pages"
        return Skip(why, why)
    variants = [("public", user, known)] if known else []
    if session.access == "private" and not same:
        variants.append(("private", full_user, full_known))
    if not variants:
        return Skip(None, "nothing to summarise")
    limits = _answer_limits(
        lines={key: most for key, _, most in hot_memory.SECTIONS},
        text_chars=hot_memory.TEXT_CHARS,
        page_chars=HOT_MEMORY_MAX_CHARS,
    )
    return Part(
        prompts.system(prompts.HOT_MEMORY), "", len(variants), limits, Briefings(variants, same)
    )


def _variant(part: Part, window: int) -> Part:
    """D5's part `window`: its material is that variant's."""
    if isinstance(part.data, Briefings):
        variant = part.data.variants[window]
        part.text = variant[1]
        part.limits = {**part.limits, "briefing": variant[0]}
    return part


async def material(
    sessions: DreamSessions | None,
    store: WikiStore,
    dream: Any,
    step: Any,
    window: Any,
    *,
    private: bool,
    client: str,
) -> dict[str, Any]:
    """dream_material: one part's prompt and material, or the pass skipped."""
    work = _material(sessions, store, dream, step, window, private=private, client=client)
    return await _shown(store, private, work)


async def _material(
    sessions: DreamSessions | None,
    store: WikiStore,
    dream: Any,
    step: Any,
    window: Any,
    *,
    private: bool,
    client: str,
) -> dict[str, Any]:
    step, window = _pass(step), _window(window)
    live = _live(sessions)
    _session(live, dream)  # refused before waiting for the lock
    async with live.lock:
        session = _session(live, dream)  # again, under the lock
        _order(session, step, window)
        _access(session, private)
        _called_from(session, client)
        wiki = _reader(store, session)
        await _skip_before(session, store, step)
        built = await _build(wiki, session, step, window)
        if isinstance(built, Skip):
            _skipped(session, step, built)
            live.save(session)
            return {
                "dream": session.dream,
                "pass": step,
                "skipped": built.say,
                "passes_to_do": to_do(session),
                "next": _next(session),
                "instructions": "Nothing to answer for this pass: go on with `next`.",
            }
        built = _variant(built, window)
        session.parts[step] = built.parts
        session.seen[f"{step}/{window}"] = built.fingerprint
        live.save(session)
    return {
        "dream": session.dream,
        "pass": step,
        "window": window,
        "windows": built.parts,
        "prompt": built.prompt,
        "material": built.text,
        "limits": built.limits,
        "private_pages": "included" if session.access == "private" else NO_PRIVATE,
        "passes_to_do": to_do(session),
        "instructions": INSTRUCTIONS,
    }


def _skipped(session: Session, step: str, skip: Skip) -> None:
    name = LOGGED[step]
    for flag in skip.flags:
        session.log.flag(name, flag)
    if skip.logged is not None:
        session.log.skip(name, skip.logged)
    session.passes[step] = []
    session.parts[step] = 0


async def _skip_before(session: Session, store: WikiStore, step: str) -> None:
    """The passes before `step` not done yet are skipped by the assistant (a pass partly
    done ends with the parts it has)."""
    for earlier in PASSES[: PASSES.index(step)]:
        if not _complete(session, earlier):
            await _abandon(session, store, earlier, "skipped by the assistant")


async def _abandon(session: Session, store: WikiStore, step: str, why: str) -> None:
    """A pass left before all its parts were submitted: logged, and ended as it is."""
    done = session.passes.get(step, [])
    parts = session.parts.get(step, 0)
    if not done:
        session.log.skip(step, why)
        session.passes[step] = []
        session.parts[step] = 0
        return
    what = PART.get(step, "part")
    session.log.skip(step, f"{parts - len(done)} of {parts} {what}s {why}")
    session.parts[step] = len(done)
    await _end(session, store, step, None)


# --- submissions ------------------------------------------------------------------------


@dataclass
class Outcome:
    """What one submission did, as the tool answers it; it all goes into the log too, as
    the portal's pass logs it."""

    session: Session
    step: str
    kept: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    commit: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return LOGGED[self.step]

    def flag(self, text: str) -> None:
        self.session.log.flag(self.name, text)
        if text.startswith("dropped: "):
            self.dropped.append(text.removeprefix("dropped: "))
        else:
            self.flags.append(text)

    def skip(self, why: str) -> None:
        self.session.log.skip(self.name, why)
        self.skipped.append(why)

    async def propose(
        self,
        store: WikiStore,
        summary: str,
        reason: str,
        ops: list[dict[str, Any]],
        *,
        speculative: bool = False,
    ) -> bool:
        """One proposal; wiki-mcp checks it again, and a refusal is logged, not raised."""
        session = self.session
        try:
            result = await store.dream_proposal(
                session.dream,
                self.name,
                summary,
                reason,
                ops,
                speculative=speculative,
                client=session.client,
            )
        except ValueError as exc:
            message = str(exc)[:200]
            session.log.flag(self.name, f"dropped: refused by the wiki: {summary}: {message}")
            self.refused.append({"summary": summary, "message": message})
            return False
        pid = str(result.proposal_id or "")
        session.record_proposal(pid, self.name, summary, speculative=speculative)
        self.kept.append({"id": pid, "summary": summary, "speculative": speculative})
        return True

    def answer(self, window: int) -> dict[str, Any]:
        out: dict[str, Any] = {
            "dream": self.session.dream,
            "pass": self.step,
            "window": window,
            "kept": self.kept,
            "dropped": self.dropped,
            "refused_by_wiki": self.refused,
            "flags": self.flags,
        }
        if self.skipped:
            out["skipped"] = self.skipped
        if self.commit is not None:
            out["commit"] = self.commit
        return {**out, "passes_to_do": to_do(self.session), "next": _next(self.session)}


def _text(answer: Any) -> str:
    """The answer as the JSON text the shared parse reads."""
    if isinstance(answer, dict):
        text = json.dumps(answer, ensure_ascii=False)
    elif isinstance(answer, str):
        text = answer.strip()
    else:
        raise Refused("'answer' must be the JSON object the prompt asks for")
    if len(text) > MAX_ANSWER:
        raise Refused(f"the answer is over {MAX_ANSWER} characters")
    return text


def _parse(step: str, text: str, part: Part) -> Any:
    """The answer parsed by the pass's own parse; Refused (not counted) when unparsable."""
    try:
        if step == "duplicates":
            return merge.parse(text)
        if step == "deduction":
            return deduction.parse(text)
        if step == "induction":
            return induction.parse(text)
        return hot_memory.parse(text, part.data.variants[0][2])  # the variant's known pages
    except Unparsable as exc:
        raise Refused(
            f"the answer is not what the prompt asks for ({exc}); it was not counted: "
            "call dream_submit again with only the JSON object"
        ) from exc


async def submit(
    sessions: DreamSessions | None,
    store: WikiStore,
    dream: Any,
    step: Any,
    window: Any,
    answer: Any,
    *,
    private: bool,
    client: str,
) -> dict[str, Any]:
    """dream_submit: check the answer, propose (D5: commit after its last part), log."""
    but = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
    work = _submit(sessions, store, dream, step, window, answer, private=private, client=client)
    return await _shown(store, private, work, but=but)


async def _submit(
    sessions: DreamSessions | None,
    store: WikiStore,
    dream: Any,
    step: Any,
    window: Any,
    answer: Any,
    *,
    private: bool,
    client: str,
) -> dict[str, Any]:
    step, window = _pass(step), _window(window)
    text = _text(answer)
    live = _live(sessions)
    _session(live, dream)  # refused before waiting for the lock
    async with live.lock:
        session = _session(live, dream)
        _order(session, step, window)
        _access(session, private)
        key = f"{step}/{window}"
        if key not in session.seen:
            raise Refused(
                f"call dream_material(dream={session.dream}, pass_name={step!r}, "
                f"window={window}) first: the answer must be to that material"
            )
        _called_from(session, client)
        wiki = _reader(store, session)
        await _skip_before(session, store, step)
        built = await _build(wiki, session, step, window)
        if isinstance(built, Part):
            built = _variant(built, window)
            if isinstance(built.data, Briefings):
                built.data.variants = [built.data.variants[window]]  # for the parse
        if isinstance(built, Skip) or built.fingerprint != session.seen[key]:
            raise Refused(
                "the material changed since dream_material (a page was edited or a proposal "
                "accepted meanwhile); this answer was not counted: call dream_material again "
                "for this part and answer the new material"
            )
        parsed = _parse(step, text, built)
        out = Outcome(session, step)
        session.log.models[out.name] = session.client
        now = dt.datetime.now(dt.UTC)
        if step == "duplicates":
            await _merges(out, wiki, store, built, parsed, now)
        elif step == "deduction":
            await _deductions(out, wiki, store, built, parsed, now)
        elif step == "induction":
            await _patterns(out, store, built, parsed, window, now)
        else:
            _briefing(out, built, parsed, window, now)
        session.passes.setdefault(step, []).append(str(window))
        if _complete(session, step):
            await _end(session, store, step, out)
        live.save(session)
    return out.answer(window)


async def _merges(
    out: Outcome, wiki: LocalReader, store: WikiStore, part: Part, answer: Any, now: dt.datetime
) -> None:
    """D4's duplicates, as portal/dreams/merge.py merges()."""
    pairs, known, private = part.data
    if answer.notes:
        out.flag(f"duplicates: model notes: {answer.notes}")
    state = merge.Duplicates(pairs, known, private, out.session.dream, now)
    for item in answer.pairs[: merge.MOST]:
        try:
            found = await state.check(item, wiki)
        except merge.LeftOut as exc:
            out.flag(f"merge left out: {exc}")
            continue
        except deduction.Invalid as exc:
            out.flag(f"dropped: duplicates: {exc}")
            continue
        if found is None:  # not the same: counted
            continue
        if not await out.propose(store, found.summary, found.why(out.session.dream), found.ops):
            continue
        state.proposed(found)
        out.flag(f"merge proposed: {found.other.id} into {found.kept.id}: {found.reason}")
    if state.not_same:
        out.flag(f"{state.not_same} candidate duplicate pair(s) judged not the same")


async def _deductions(
    out: Outcome, wiki: LocalReader, store: WikiStore, part: Part, answer: Any, now: dt.datetime
) -> None:
    """D2, as portal/dreams/deduction.py deduction_pass()."""
    found: deduction.Material = part.data
    if answer.notes:
        out.flag(f"model notes: {answer.notes}")
    plan = await deduction.review(wiki, answer, found, out.session.dream, now)
    for why in plan.dropped:
        out.flag(f"dropped: {why}")
    for proposal in plan.proposals(out.session.dream):
        await out.propose(store, proposal.summary, proposal.reason, proposal.ops)
    if not plan.pages and not plan.inferences:
        n = len(found.pages)
        out.skip(f"nothing to propose from {n} page{'' if n == 1 else 's'}")


async def _patterns(
    out: Outcome, store: WikiStore, part: Part, answer: Any, window: int, now: dt.datetime
) -> None:
    """One D3 window, as portal/dreams/induction.py induction_pass() does it; what its
    Induction carries across windows (new inferences counted, inferences with a proposal,
    page ids taken, the time) is kept in the session between windows."""
    data: Window = part.data
    state = data.state
    carry = out.session.carry.setdefault("induction", {})
    if window == 0:  # the portal's flags at the start of the pass
        for page in data.too_long:
            out.flag(f"{page.id}: too long to read (over {MAX_CHARS} characters)")
        if data.windows > induction.MAX_WINDOWS:
            out.flag(f"read the first {induction.MAX_WINDOWS} of {data.windows} windows of pages")
        carry.update(at=now.isoformat(), creates=0, touched=[], taken=[], pages=0, proposed=0)
    ids = set(state.taken)
    state.at = dt.datetime.fromisoformat(str(carry["at"]))
    state.creates = int(carry["creates"])
    state.touched = set(carry["touched"])
    state.taken |= set(carry["taken"])
    carry["pages"] = int(carry["pages"]) + len(data.material.pages)
    if answer.notes:
        out.flag(f"model notes: {answer.notes}")
    if len(answer.patterns) > induction.MAX_PATTERNS:
        out.flag(
            f"dropped: {len(answer.patterns) - induction.MAX_PATTERNS} patterns over the limit "
            f"of {induction.MAX_PATTERNS}"
        )
    for item in answer.patterns[: induction.MAX_PATTERNS]:
        checked = state.checked(item, data.material)
        if checked.proposal is None:
            out.flag(f"dropped: {checked.dropped}")
        for flag in checked.flags:
            out.flag(flag)
        p = checked.proposal
        if p is not None and await out.propose(
            store, p.summary, p.reason, p.ops, speculative=p.speculative
        ):
            carry["proposed"] = int(carry["proposed"]) + 1
    carry["creates"] = state.creates
    carry["touched"] = sorted(state.touched)
    carry["taken"] = sorted(set(carry["taken"]) | (state.taken - ids))


def _briefing(out: Outcome, part: Part, briefing: Any, window: int, now: dt.datetime) -> None:
    """One D5 briefing, as portal/dreams/hot_memory.py _briefing(); committed with the
    other at the pass's end."""
    data: Briefings = part.data
    variant, _, known = data.variants[0]
    made = hot_memory.made(briefing, known, out.session.dream, now, HOT_MEMORY_MAX_CHARS)
    for why in briefing.dropped:
        out.flag(f"dropped: {why}")
    carry = out.session.carry.setdefault("hot-memory", {})
    carry.setdefault("made", {})[variant] = {"body": made.body, "lines": made.lines}
    carry["same"] = data.same
    out.kept.append({"briefing": variant, "lines": made.lines, "characters": len(made.body)})


async def _end(session: Session, store: WikiStore, step: str, out: Outcome | None) -> None:
    """A pass's parts are all in (or it was left): what the portal does after its last part."""
    if step == "induction":
        carry = session.carry.get("induction", {})
        pages = int(carry.get("pages", 0))
        if not carry.get("proposed") and pages:
            why = f"nothing to propose from {pages} page(s)"
            if out is not None:
                out.skip(why)
            else:
                session.log.skip(LOGGED[step], why)
    elif step == "hot-memory":
        await _commit_briefing(session, store, out)


async def _commit_briefing(session: Session, store: WikiStore, out: Outcome | None) -> None:
    """D5's one commit: the briefings made, as hot_memory.write makes it."""
    carry = session.carry.get("hot-memory", {})
    made = carry.get("made") or {}
    if not made:
        return

    def of(variant: str) -> hot_memory.Made | None:
        got = made.get(variant)
        return None if got is None else hot_memory.Made(str(got["body"]), int(got["lines"]))

    wiki = _reader(store, session)
    commit = await hot_memory.write(
        wiki, session.dream, of("public"), of("private"), bool(carry.get("same"))
    )
    if not commit.ops:
        return
    name = LOGGED["hot-memory"]
    try:
        sha, changed = await store.dream_commit(
            session.dream, name, commit.reason, commit.ops, client=session.client
        )
    except ValueError as exc:  # the portal's pass fails with it
        session.log.skip(name, f"error: {exc}")
        if out is not None:
            out.refused.append({"summary": commit.reason, "message": str(exc)[:200]})
        return
    if sha:
        session.record_commit(sha, name, changed, commit.lines)
        if out is not None:
            out.commit = {"sha": sha, "pages": changed}


# --- the end ----------------------------------------------------------------------------


async def finish(
    sessions: DreamSessions | None, store: WikiStore, dream: Any, *, private: bool, client: str
) -> dict[str, Any]:
    """dream_finish: end the passes left partly done, then D6 (dream/assistant.py close).
    `private`: whether the calling app has wiki:private (the session holds what the hygiene
    fixes at its opening found on private pages)."""
    return await _shown(store, private, _finish(sessions, store, dream, client=client))


async def _finish(
    sessions: DreamSessions | None, store: WikiStore, dream: Any, *, client: str
) -> dict[str, Any]:
    live = _live(sessions)
    _session(live, dream)  # refused before waiting for the lock
    async with live.lock:
        session = _session(live, dream)
        _called_from(session, client)
        for step in PASSES:
            if session.passes.get(step) and not _complete(session, step):
                await _abandon(
                    session, store, step, "not done: the assistant finished without them"
                )
        live.save(session)
    session = await dream_assistant.close(live, store, session.dream, "finished")
    done = [p for p in PASSES if session.passes.get(p)]
    return {
        "dream": session.dream,
        "log_page": session.log_page,
        "commits": session.commits,
        "proposals": session.proposals,
        "passes_done": done,
        "passes_skipped": [p for p in PASSES if p not in done],
        "skipped": session.log.skipped,
        "flagged": session.log.flagged,
    }
