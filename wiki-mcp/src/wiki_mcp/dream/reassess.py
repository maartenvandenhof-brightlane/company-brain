"""A patterns session's pass, as the MCP tools drive it (mcp/pattern_tools.py): the owner opens
the session on the portal's Patterns page ("Reassess in Claude.ai / ChatGPT"), the assistant's
model answers the patterns pass's prompt itself, and wiki-mcp does what the portal's patterns
run does with a model's answer (portal/dreams/patterns.py), with the same shared code
(dream/patterns.py), so the material, checks, commit and log lines are the same.

  material  patterns_material: the pass's system prompt and material exactly as the portal's
            run sends them (every observation page, sampled, at most patterns.MAX_CHARS). With
            no observation yet the pass is skipped, with the portal's skip text in the log.
  submit    patterns_submit: the answer, parsed with the pass's parse (an unparsable answer is
            refused, not counted), reviewed, and written as one direct commit of write_pattern
            operations with `Dream` and `Dream-client` trailers (the store keeps the owner's
            feedback on a pattern). One submission; refused when the material changed since it
            was read (an observation page written, feedback given): patterns_material again.
  finish    patterns_finish: the dream log (dream/assistant.py close) and the session's end.

Only apps with wiki:private run it: the tools refuse the others before anything is read (the
material is observations of private chats, and every page it writes is private), so nothing
here is filtered as dream/passes.py filters an assistant dream's answers.
"""

from __future__ import annotations

import hashlib
from typing import Any

from wiki_mcp.dream import Unparsable, patterns
from wiki_mcp.dream import assistant as dream_assistant
from wiki_mcp.dream.passes import Refused, _called_from, _live, _session, _text
from wiki_mcp.dream.sessions import DreamSessions
from wiki_mcp.wiki.store import WikiStore

KIND = "patterns"
NAME = patterns.NAME
PART = f"{NAME}/0"  # the pass's one part, as the session keeps its fingerprint
NO_OBSERVATIONS = "no observations yet: observe chats on the Patterns page first"
INSTRUCTIONS = (
    "Answer with only the JSON object the prompt asks for, then call patterns_submit with it "
    "(the same dream)."
)


def _material(store: WikiStore) -> patterns.Material:
    """The pass's material as GET /patterns/material builds it for the portal."""
    return patterns.material(store.pattern_evidence(), store.pattern_pages())


def _fingerprint(found: patterns.Material) -> str:
    return hashlib.sha256(f"{patterns.system_prompt()}\0{found.text}".encode()).hexdigest()


def _not_again(done: bool) -> None:
    if done:
        raise Refused(
            "the patterns pass is done already (one submission each): call patterns_finish"
        )


async def material(
    sessions: DreamSessions | None, store: WikiStore, dream: Any, *, client: str
) -> dict[str, Any]:
    """patterns_material: the pass's prompt and material, or the pass skipped."""
    live = _live(sessions)
    _session(live, dream, KIND)  # refused before waiting for the lock
    async with live.lock:
        session = _session(live, dream, KIND)
        _not_again(NAME in session.passes)
        session.access = "private"
        _called_from(session, client)
        found = _material(store)
        if not found.refs:
            session.log.skip(NAME, NO_OBSERVATIONS)
            session.passes[NAME] = []
            session.parts[NAME] = 0
            live.save(session)
            return {
                "dream": session.dream,
                "pass": NAME,
                "skipped": NO_OBSERVATIONS,
                "next": "patterns_finish",
                "instructions": "Nothing to answer: call patterns_finish. Chats are observed "
                "first (prompt: run-observe).",
            }
        session.parts[NAME] = 1
        session.seen[PART] = _fingerprint(found)
        live.save(session)
    return {
        "dream": session.dream,
        "pass": NAME,
        "prompt": patterns.system_prompt(),
        "material": found.text,
        "limits": {
            "patterns_considered": patterns.MAX_PATTERNS,
            "creates": patterns.MAX_CREATES,
            "supports_least": 2,
            "sampled": len(found.refs),
            "notes_chars": patterns.NOTES_CHARS,
        },
        "instructions": INSTRUCTIONS,
    }


async def submit(
    sessions: DreamSessions | None, store: WikiStore, dream: Any, answer: Any, *, client: str
) -> dict[str, Any]:
    """patterns_submit: check the answer and write the pattern pages, as the portal's
    patterns_pass does; its log lines go into the session's dream log."""
    text = _text(answer)
    live = _live(sessions)
    _session(live, dream, KIND)
    async with live.lock:
        session = _session(live, dream, KIND)
        _not_again(NAME in session.passes)
        if PART not in session.seen:
            raise Refused(
                f"call patterns_material(dream={session.dream}) first: the answer must be to "
                "that material"
            )
        _called_from(session, client)
        found = _material(store)
        if not found.refs or _fingerprint(found) != session.seen[PART]:
            raise Refused(
                "the material changed since patterns_material (an observation page was written "
                "or feedback given meanwhile); this answer was not counted: call "
                "patterns_material again and answer the new material"
            )
        try:
            parsed = patterns.parse(text)
        except Unparsable as exc:
            raise Refused(
                f"the answer is not what the prompt asks for ({exc}); it was not counted: "
                "call patterns_submit again with only the JSON object"
            ) from exc
        log = session.log
        log.models[NAME] = session.client
        if parsed.notes:
            log.flag(NAME, f"model notes: {parsed.notes}")
        plan = patterns.review(parsed, found)
        for why in plan.dropped:
            log.flag(NAME, f"dropped: {why}")
        for flag in plan.flags:
            log.flag(NAME, flag)
        skew = [f"coverage: {s}" for s in found.coverage.get("skew") or []]
        for flag in skew:
            log.flag(NAME, flag)
        out: dict[str, Any] = {"dream": session.dream, "pass": NAME, "written": []}
        if not plan.ops:
            log.skip(NAME, f"nothing to write from {len(found.refs)} sampled observations")
        else:
            try:
                sha, changed = await store.dream_commit(
                    session.dream,
                    NAME,
                    plan.reason(found.observations),
                    plan.ops,
                    client=session.client,
                )
            except ValueError as exc:  # the portal's pass fails with it
                log.skip(NAME, f"error: {exc}")
                out["refused_by_wiki"] = str(exc)[:200]
            else:
                if sha:
                    session.record_commit(sha, NAME, changed, plan.lines)
                    out["commit"] = {"sha": sha, "pages": changed}
                    out["written"] = plan.lines
        session.passes[NAME] = ["0"]
        live.save(session)
    return {
        **out,
        "dropped": plan.dropped,
        "flags": [*plan.flags, *skew],
        "next": "patterns_finish",
    }


async def finish(
    sessions: DreamSessions | None, store: WikiStore, dream: Any, *, client: str
) -> dict[str, Any]:
    """patterns_finish: D6, the dream log, and the session's end (dream/assistant.py close)."""
    live = _live(sessions)
    _session(live, dream, KIND)
    async with live.lock:
        session = _session(live, dream, KIND)
        _called_from(session, client)
        live.save(session)
    session = await dream_assistant.close(live, store, session.dream, "finished")
    return {
        "dream": session.dream,
        "log_page": session.log_page,
        "commits": session.commits,
        "done": bool(session.passes.get(NAME)),
        "skipped": session.log.skipped,
        "flagged": session.log.flagged,
    }
