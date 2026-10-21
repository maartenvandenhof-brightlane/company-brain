"""The Dreams page: what each dream did, and running or reverting one by hand (owner only).

  GET  /dreams               whether a dream is due and why; every dream: trigger, cost,
                             counts, its log, Revert; Dream now; Dream in Claude.ai/ChatGPT
  GET  /dreams/{id}          one dream; for a dry run, what it would have written; for an
                             assistant dream, what to tell the assistant and its session
  GET  /dreams/status        where the current or last dream is (JSON)
  GET  /dreams/{id}/session  an assistant dream's session, the row brought up to date (JSON)
  POST /dreams/run           dream now (dry_run=1: every pass, nothing written)
  POST /dreams/assistant     dream in Claude.ai or ChatGPT (client=claude.ai|chatgpt): open a
                             session (dreams/assistant.py), then its page
  POST /dreams/{id}/end      end an assistant dream's session: its log is written
  POST /dreams/{id}/revert   revert its direct commits, newest first, and reject its proposals

The POSTs are plain forms (CSRF and origin checked by `owner`) that go back to the page.
Listing the dreams brings running assistant dreams up to date (an expired session is closed
then), so none stays running.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from portal.dreams.assistant import CLIENTS, AssistantDreams, Refused, client_name, tell, what
from portal.dreams.job import STEPS, DreamJob
from portal.dreams.store import Dream, DreamStore
from portal.dreams.trigger import DreamTrigger, Due
from portal.ideas.store import now
from portal.sessions import Session
from portal.web import Portal, owner
from portal.wikiclient import WikiUnavailable

PAGE = "/dreams"


class DreamRoutes:
    def __init__(
        self, portal: Portal, store: DreamStore, job: DreamJob, due: DreamTrigger | None = None
    ) -> None:
        self.p = portal
        self.store = store
        self.job = job
        self.due = due
        self.assistant = AssistantDreams(store, job)

    def routes(self) -> list[Route]:
        o = owner(self.p)
        return [
            Route(PAGE, o(self.page)),
            Route(f"{PAGE}/status", o(self.status)),
            Route(f"{PAGE}/run", o(self.run), methods=["POST"]),
            Route(f"{PAGE}/assistant", o(self.start_assistant), methods=["POST"]),
            Route(PAGE + "/{id:int}", o(self.one)),
            Route(PAGE + "/{id:int}/session", o(self.session)),
            Route(PAGE + "/{id:int}/end", o(self.end), methods=["POST"]),
            Route(PAGE + "/{id:int}/revert", o(self.revert), methods=["POST"]),
        ]

    async def page(self, request: Request, session: Session, _: FormData | None) -> Response:
        await self.assistant.sync_running()
        progress = self.job.progress
        due: Due | None = None
        due_error = ""
        if self.due is not None and not self.job.running:  # the page reloads during a run
            try:
                due = await self.due.check()
            except WikiUnavailable as exc:
                due_error = f"the wiki is unreachable ({exc})"
        return self.p.render(
            request,
            "dreams.html",
            session,
            due=due,
            due_error=due_error,
            dreams=self.store.recent(),
            mode=self.p.settings.dream_mode,
            check_minutes=self.p.settings.dream_check_minutes,
            running=self.job.running,
            progress=progress,
            steps=progress.steps if progress else STEPS,
            held=self.job.assistant,
            clients=CLIENTS,
            client_name=client_name,
            what=what,
        )

    async def one(self, request: Request, session: Session, _: FormData | None) -> Response:
        dream = self.store.get(int(request.path_params["id"]))
        if dream is None:
            session.notify("That dream does not exist.", ok=False)
            return RedirectResponse(PAGE, status_code=303)
        would = dream.dry_run_result or {}
        state: dict[str, Any] | None = None
        state_error = ""
        if dream.in_assistant:
            try:
                state = await self.assistant.sync(dream)
            except (ValueError, WikiUnavailable) as exc:
                state_error = str(exc)
        return self.p.render(
            request,
            "dream.html",
            session,
            dream=dream,
            log_text=str(would.get("log") or ""),
            would_commits=would.get("commits") or [],
            would_proposals=would.get("proposals") or [],
            as_json=lambda value: json.dumps(value, ensure_ascii=False, indent=2),
            state=state,
            state_error=state_error,
            client=client_name(dream),
            tell=tell(dream),
            when=_when,
        )

    async def session(self, request: Request, session: Session, _: FormData | None) -> Response:
        dream = self.store.get(int(request.path_params["id"]))
        if dream is None or not dream.in_assistant:
            return JSONResponse({"error": "no such assistant dream"}, status_code=404)
        try:
            state = await self.assistant.sync(dream)
        except (ValueError, WikiUnavailable) as exc:
            return JSONResponse({"dream": dream.id, "running": dream.running, "error": str(exc)})
        return JSONResponse({**(state or {}), "dream": dream.id, "running": dream.running})

    async def start_assistant(
        self, request: Request, session: Session, form: FormData | None
    ) -> Response:
        assert form is not None
        try:
            dream = await self.assistant.start(str(form.get("client") or ""))
        except Refused as exc:
            session.notify(str(exc), ok=False)
            return RedirectResponse(PAGE, status_code=303)
        session.notify(f"Dream {dream.id} is waiting for {client_name(dream)}.")
        return RedirectResponse(f"{PAGE}/{dream.id}", status_code=303)

    async def end(self, request: Request, session: Session, form: FormData | None) -> Response:
        dream = self.store.get(int(request.path_params["id"]))
        if dream is None:
            session.notify("That dream does not exist.", ok=False)
            return RedirectResponse(PAGE, status_code=303)
        try:
            await self.assistant.end(dream)
        except Refused as exc:
            session.notify(str(exc), ok=False)
        else:
            session.notify(f"{what(dream)} ended: its log is written.")
        return RedirectResponse(f"{PAGE}/{dream.id}", status_code=303)

    async def status(self, request: Request, session: Session, _: FormData | None) -> Response:
        progress = self.job.progress
        body = progress.as_dict() if progress else {"finished": True, "step": 0}
        return JSONResponse({**body, "running": self.job.running})

    async def run(self, request: Request, session: Session, form: FormData | None) -> Response:
        assert form is not None
        dry_run = form.get("dry_run") == "1"
        held = self.job.assistant
        if held is not None:
            session.notify(
                f"{what(held)} is waiting in {client_name(held)}: end its session first.",
                ok=False,
            )
        elif not self.job.trigger("manual", dry_run=dry_run):
            session.notify("A dream or an ideas run is going: wait for it to finish.", ok=False)
        else:
            session.notify("Dry run started: nothing is written." if dry_run else "Dream started.")
        return RedirectResponse(PAGE, status_code=303)

    async def revert(self, request: Request, session: Session, form: FormData | None) -> Response:
        dream = self.store.get(int(request.path_params["id"]))
        if dream is not None and dream.in_assistant and dream.running:
            await self._synced(dream)  # finished in the assistant, not read yet
        if dream is None or dream.dry_run or dream.running:
            session.notify("Only a finished dream that wrote something can be reverted.", ok=False)
            return RedirectResponse(PAGE, status_code=303)
        if self.job.running:
            session.notify("Wait for the dream or ideas run that is going.", ok=False)
            return RedirectResponse(PAGE, status_code=303)
        try:
            result = await self.job.wiki.dream_revert(dream.id)
        except (ValueError, WikiUnavailable) as exc:
            session.notify(f"Not reverted: {exc}", ok=False)
            return RedirectResponse(PAGE, status_code=303)
        reverted, rejected = len(result.get("reverted") or []), len(result.get("rejected") or [])
        conflict = result.get("conflict")
        if conflict:
            pages = ", ".join(conflict.get("pages") or [])
            session.notify(
                f"Reverted {reverted} commit(s), then stopped: reverting "
                f"{str(conflict.get('commit'))[:12]} conflicts with a later change to {pages}. "
                "Nothing was forced; fix that page by hand, then revert again.",
                ok=False,
            )
            return RedirectResponse(PAGE, status_code=303)
        dream.reverted = f"{now()}: {reverted} commit(s) reverted, {rejected} proposal(s) rejected"
        self.store.save(dream)
        session.notify(f"Dream {dream.id} reverted: {dream.reverted.split(': ', 1)[1]}.")
        return RedirectResponse(PAGE, status_code=303)

    async def _synced(self, dream: Dream) -> None:
        with contextlib.suppress(ValueError, WikiUnavailable):  # still running: refused
            await self.assistant.sync(dream)


def _when(iso: str | None) -> str:
    """An ISO time as the page shows it: 2026-09-30 10:15 UTC."""
    return f"{iso[:16].replace('T', ' ')} UTC" if iso else ""
