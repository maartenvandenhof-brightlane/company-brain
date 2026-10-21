"""Dreams in Claude.ai or ChatGPT (assistant dreams): the portal's side.

The owner starts one on the Dreams page. The portal makes its dreams row (trigger
"assistant", the client, the base commit and the day's number as for any dream) and asks
wiki-mcp to open its session (wiki_mcp.dream.assistant), which makes the hygiene fixes at
once. The owner then tells the assistant to run the dream by its id; the assistant does the
passes over MCP and closes the session, which writes the dream log. The owner can end the
session here instead, and one that expired (after two hours) is closed on the next read.

The row is running until the portal reads the session finished: `sync` then copies what it
did (commits, proposals, the log page, when it ended) into the row, so Revert works as for any
dream. The Dreams page syncs running assistant dreams whenever it lists them, and the dream's
own page while it follows the session. While a session may be open, no portal dream starts
(DreamJob.assistant); ideas runs are not affected.

Patterns reassessed in Claude.ai or ChatGPT (the Patterns page) are the same with kind
"patterns": a row with trigger "patterns" and the client, a patterns session in wiki-mcp (no
hygiene; the patterns pass only), and the same end, sync and hold.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from portal.dreams.job import DreamJob
from portal.dreams.store import PATTERNS, Dream, DreamStore
from portal.ideas.store import now
from portal.wikiclient import NotFound, WikiUnavailable

log = logging.getLogger(__name__)

CLIENTS = {"claude.ai": "Claude.ai", "chatgpt": "ChatGPT"}  # the session's client -> its name
KINDS = {"dream": "assistant", "patterns": PATTERNS}  # a session's kind -> its row's trigger


class Refused(Exception):
    """Not started or not ended; the message is for the owner."""


def client_name(dream: Dream) -> str:
    return CLIENTS.get(dream.client or "", dream.client or "the assistant")


def what(dream: Dream) -> str:
    """What the owner calls it: "Dream 3", or "Patterns session 4"."""
    return f"{'Patterns session' if dream.trigger == PATTERNS else 'Dream'} {dream.id}"


def tell(dream: Dream) -> str:
    """What the owner types in the assistant to run it (ChatGPT may not show MCP prompts)."""
    if dream.trigger == PATTERNS:
        return f"Reassess my patterns in session {dream.id}"
    return f"Run the dream {dream.id}"


class AssistantDreams:
    def __init__(self, store: DreamStore, job: DreamJob) -> None:
        self.store = store
        self.job = job
        self.wiki = job.wiki
        self._opening: set[int] = set()  # rows whose session is being opened

    async def start(self, client: str, kind: str = "dream") -> Dream:
        """A new assistant dream (kind "patterns": patterns reassessed in the assistant) with
        its session open; Refused when one cannot start now."""
        if client not in CLIENTS:
            raise Refused("Choose Claude.ai or ChatGPT.")
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        if self.job.running:
            raise Refused("A dream or an ideas run is going: wait for it to finish.")
        held = self.job.assistant
        if held is not None:
            raise Refused(f"{what(held)} is waiting in {client_name(held)}: end its session first.")
        async with self.job.lock:  # no dream or ideas run starts while the session opens
            try:
                dream, since_base = await self.job.begin(KINDS[kind], client=client)
            except WikiUnavailable as exc:
                raise Refused(f"Not started: {exc}.") from exc
            number = self.store.number_on(dream.started[:10], dream.id)
            self._opening.add(dream.id)
            try:
                await self.wiki.dream_session_open(
                    dream.id, client, dream.started, number, since_base, kind
                )
            except (ValueError, WikiUnavailable) as exc:
                dream.error = f"the session did not open: {exc}"[:500]
                if isinstance(exc, WikiUnavailable):  # it may have opened after all: end it
                    with contextlib.suppress(ValueError, WikiUnavailable):
                        self._copy(dream, await self.wiki.dream_session_close(dream.id))
                if dream.running:
                    self.store.finish(dream)
                raise Refused(f"{what(dream)} did not start: {exc}.") from exc
            finally:
                self._opening.discard(dream.id)
        self._audit("dream_assistant_open", dream)
        return dream

    async def sync(self, dream: Dream) -> dict[str, Any] | None:
        """The session's state, with the row brought up to date: an expired session is closed
        first (its log is written), a finished one copied into the row. None when wiki-mcp
        has no session for the dream (the row is then finished with that error)."""
        try:
            state = await self.wiki.dream_session(dream.id)
        except NotFound:
            if dream.running and dream.id not in self._opening:
                dream.error = "wiki-mcp has no session for this dream"
                self.store.finish(dream)
            return None
        if state.get("status") == "expired":
            try:
                state = await self.wiki.dream_session_close(dream.id)
            except ValueError:  # closed in the meantime
                state = await self.wiki.dream_session(dream.id)
        self._copy(dream, state)
        return state

    async def sync_running(self) -> None:
        """Bring every running assistant dream's row up to date (the Dreams page's list)."""
        for dream in self.store.running_assistant():
            try:
                await self.sync(dream)
            except (ValueError, WikiUnavailable) as exc:
                log.info("assistant dream %s not synced: %s", dream.id, exc)

    async def end(self, dream: Dream) -> dict[str, Any]:
        """The owner ends the session: the log is written; Refused when it is over already."""
        if not dream.running or not dream.in_assistant:
            raise Refused(f"{what(dream)} has no open session.")
        try:
            state = await self.wiki.dream_session_close(dream.id)
        except ValueError as exc:  # finished meanwhile: bring the row up to date
            await self.sync(dream)
            raise Refused(f"Not ended: {exc}.") from exc
        except WikiUnavailable as exc:
            raise Refused(f"Not ended: {exc}.") from exc
        self._copy(dream, state)
        self._audit("dream_assistant_end", dream)
        return state

    def _copy(self, dream: Dream, state: dict[str, Any]) -> None:
        """What a finished session did, into its row (once)."""
        if state.get("status") != "finished" or not dream.running:
            return
        dream.commits = list(state.get("commits") or [])
        dream.proposals = list(state.get("proposals") or [])
        dream.log_page = str(state.get("log_page") or "")
        dream.finished = str(state.get("finished") or now())
        self.store.save(dream)

    def _audit(self, event: str, dream: Dream) -> None:
        if self.job.audit is not None:
            self.job.audit.event(
                event, client="portal", outcome="ok", reason=dream.client, dream=dream.id
            )
