"""An assistant dream: a dream the owner runs by hand in Claude.ai or ChatGPT, whose model
does the passes that need one. wiki-mcp keeps its session (dream/sessions.py) and does the
rest itself, with the same code as the portal's dreams:

  open    the portal's Dreams page opens the session (POST /dream/session); D4's code fixes
          run at once, as the portal's hygiene pass makes them (dream/hygiene.py), as one
          commit. Duplicate merges are the assistant's first pass.
  passes  the assistant reads each pass's material and submits its answer (the MCP tools,
          mcp/dream_tools.py, over dream/passes.py)
  close   D6: the dream log, as the dream's last commit; by the assistant when it is done
          (finished), by the owner on the Dreams page (closed by the owner), or on the first
          read after the session expired (expired)

Its direct writes are the hygiene fixes, the briefing and the log, one commit per pass with
`Dream: <id>` and `Dream-client: <client>` trailers; everything else is a proposal. There is
no budget guard (the assistant pays for its model); the log names the client instead of a
cost. Reverting the dream (the portal's Revert) works as for any dream.

A patterns session (kind "patterns", the Patterns page's "Reassess in Claude.ai / ChatGPT")
opens and closes the same way, without the hygiene fixes: its one pass is the patterns pass
(dream/reassess.py), a direct commit of pattern pages.
"""

from __future__ import annotations

import logging

from wiki_mcp.dream import hygiene
from wiki_mcp.dream import log as dream_log
from wiki_mcp.dream.local import LocalReader
from wiki_mcp.dream.pages import reviewed_pages
from wiki_mcp.dream.sessions import ENDED, DreamSessions, Session, SessionError, iso
from wiki_mcp.wiki.store import WikiStore

log = logging.getLogger(__name__)

# Why a pass is not in the log's direct changes or proposals, by how the session ended.
NOT_DONE = {
    "finished": "not done: the assistant finished without it",
    "expired": "not done: the session expired",
    "closed by the owner": "not done: the owner ended the session",
}


async def open_session(
    sessions: DreamSessions,
    store: WikiStore,
    *,
    dream: int,
    client: str,
    started: str,
    number: int,
    since_base: str | None,
    kind: str = "dream",
) -> Session:
    """Open a session and, for a dream, make the hygiene fixes; SessionError when it cannot be
    opened."""
    wiki = LocalReader(store)
    async with sessions.lock:
        recent = await wiki.recent(1)
        base = str(recent[0]["commit"]) if recent else ""
        session = sessions.create(dream, client, started, number, since_base, base, kind)
        if kind != "dream":
            return session
        try:
            await _hygiene(wiki, store, session)
        except (ValueError, LookupError) as exc:  # the session stays open: the log says so
            log.warning("dream %s, hygiene: %s", dream, exc)
            session.log.skip("hygiene", f"error: {exc}")
        except Exception as exc:
            log.exception("dream %s, hygiene failed", dream)
            session.log.skip("hygiene", f"error: {type(exc).__name__}")
        sessions.save(session)
    return session


async def _hygiene(wiki: LocalReader, store: WikiStore, session: Session) -> None:
    """D4's code fixes on reviewed pages, as one commit (the portal's hygiene_pass, without
    the merges)."""
    pages = await reviewed_pages(wiki)
    survey = await hygiene.survey(wiki, pages)
    planned = survey.planned
    for flag in planned.flags:
        session.log.flag("hygiene", flag)
    if not planned.fixes:
        session.log.skip("hygiene", f"nothing to fix on {len(pages)} reviewed pages")
        return
    sha, changed = await store.dream_commit(
        session.dream,
        "hygiene",
        survey.reason(session.dream),
        [f.op for f in planned.fixes],
        client=session.client,
    )
    if sha:
        session.record_commit(sha, "hygiene", changed, [f.line for f in planned.fixes])


async def close(sessions: DreamSessions, store: WikiStore, dream: int, ended: str) -> Session:
    """D6: write the dream log as the dream's last commit and finish the session. SessionError
    when there is no session, or it was closed before."""
    if ended not in ENDED:
        raise ValueError(f"unknown end {ended!r}")
    async with sessions.lock:
        session = sessions.get(dream)
        if session is None:
            raise SessionError(f"dream {dream} has no session")
        if session.finished is not None:
            raise SessionError(f"the session of dream {dream} is closed already ({session.ended})")
        for pass_ in session.to_do:
            session.log.skip(pass_, NOT_DONE[ended])
        at = sessions.now()
        body = dream_log.render(
            session,
            session.log,
            session.number,
            iso(at),
            0.0,
            client=session.client,
            ended=ended,
            run_in=", ".join(session.run_in) or None,
        )
        page = dream_log.page_id(session, session.number)
        # private while the wiki has any private document (wiki_mcp.dream.log)
        op = await dream_log.operation(LocalReader(store), session, session.number, body)
        sha, changed = await store.dream_commit(
            dream, "log", f"Dream {dream} log", [op], client=session.client
        )
        if sha:
            session.record_commit(sha, "log", changed, [f"{page}: the dream log"])
        sessions.finish(session, ended, page, at)
    return session
