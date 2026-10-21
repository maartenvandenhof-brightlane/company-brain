"""The Ideas tab: the ideas and the owner's answers, his goals and statements, the taste
note, and the runs.

  GET  /ideas              the tab: every open idea as a card, the goals editor, the "About
                           me" box, the taste note, the latest runs and the Ideate button
  POST /ideas/{id}/state   the owner's answer: useful, not useful, done, dismissed, already
                           doing, new again (state=...), or snoozed for 7 or 30 days (snooze=)
  POST /ideas/goals        add one goal (add=) or drop one (drop=<the exact line>)
  POST /ideas/about        a statement about himself (text, section)
  POST /ideas/taste        the owner's own taste note
  POST /ideas/run          generate ideas now, in the background: from the whole wiki (the
                           Ideate button, scope=wiki) or from what was filed since the last run
  GET  /ideas/status       where the current or last run is (JSON, for the progress display)

The POSTs are plain forms (CSRF and origin checked by `owner`) that go back to where the
button was (`back`: /ideas or /home). Ideas never go into the wiki: the only writes are the
owner's own goals and statements, through wiki-mcp's POST /owner (trust owner). Every answer
counts as a reaction for the taste note (IdeasJob.reacted).
"""

from __future__ import annotations

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from wiki_mcp.dream.hot_memory import active_goals
from wiki_mcp.dream.pages import OWNER_PAGE, read

from portal.home import when
from portal.ideas.collect import plain
from portal.ideas.job import IdeasJob
from portal.ideas.progress import SCOPES, STEPS
from portal.ideas.store import OPEN_STATES, SNOOZE_DAYS, STATES, IdeaStore
from portal.ideas.taste import NOTE_CHARS
from portal.ideas.view import cards, titles
from portal.sessions import Session
from portal.web import Portal, owner
from portal.wikiclient import WikiClient, WikiUnavailable

HOME = "/home"
PAGE = "/ideas"
BACK = (PAGE, HOME)
SECTIONS = ("Facts", "Background", "Preferences", "Timeline")
SHOW_RESULT_S = 120  # how long the result of a finished run stays on the page
RUNS_SHOWN = 10


def _back(form: FormData, anchor: str = "ideas") -> RedirectResponse:
    back = str(form.get("back") or HOME)
    return RedirectResponse(f"{back if back in BACK else HOME}#{anchor}", status_code=303)


class IdeaRoutes:
    def __init__(self, portal: Portal, store: IdeaStore, job: IdeasJob, wiki: WikiClient) -> None:
        self.p = portal
        self.store = store
        self.job = job
        self.wiki = wiki

    def routes(self) -> list[Route]:
        o = owner(self.p)
        return [
            Route(PAGE, o(self.page)),
            Route("/ideas/{id:int}/state", o(self.set_state), methods=["POST"]),
            Route("/ideas/goals", o(self.goals), methods=["POST"]),
            Route("/ideas/about", o(self.about), methods=["POST"]),
            Route("/ideas/taste", o(self.taste), methods=["POST"]),
            Route("/ideas/run", o(self.run), methods=["POST"]),
            Route("/ideas/status", o(self.status)),
        ]

    async def page(self, request: Request, session: Session, _: FormData | None) -> Response:
        problems: list[str] = []
        known = await titles(self.wiki)
        if known is None:
            problems.append("the wiki's page list")
            known = {}
        goals: list[dict[str, str]] | None = None
        try:
            goals = [
                {"line": g, "text": plain(g)}
                for g in active_goals(await read(self.wiki, OWNER_PAGE))
            ]
        except (WikiUnavailable, ValueError):
            problems.append("your goals")
        progress = self.job.progress
        # A finished run stays on the page for a moment, so its result is seen.
        recent = progress is not None and (
            not progress.finished or progress.seconds_since_end < SHOW_RESULT_S
        )
        runs = self.store.runs(RUNS_SHOWN)
        return self.p.render(
            request,
            "ideas.html",
            session,
            cards=cards(self.store.ideas(OPEN_STATES, by="score"), known),
            counts=self.store.counts(),
            goals=goals,
            sections=SECTIONS,
            taste=self.store.taste(),
            note_chars=NOTE_CHARS,
            runs=[(r, when(r.started)) for r in runs],
            problems=problems,
            progress=progress if recent else None,
            steps=STEPS,
            can_look=bool(self.p.settings.litellm_master_key.get_secret_value()),
            ideas_at=self.p.settings.ideas_at,
        )

    async def set_state(
        self, request: Request, session: Session, form: FormData | None
    ) -> Response:
        assert form is not None
        idea_id = int(request.path_params["id"])
        state, days = str(form.get("state") or ""), None
        if form.get("snooze") is not None:
            state = "snoozed"
            days = int(s) if (s := str(form.get("snooze"))).isdigit() else None
        if state not in STATES or (state == "snoozed" and days not in SNOOZE_DAYS):
            session.notify("Unknown choice for this idea.", ok=False)
        elif not self.store.set_state(idea_id, state, days=days):
            session.notify("That idea no longer exists.", ok=False)
        else:
            self.job.reacted()
        return _back(form)

    async def goals(self, request: Request, session: Session, form: FormData | None) -> Response:
        assert form is not None
        add = " ".join(str(form.get("add") or "").split())
        drop = str(form.get("drop") or "").strip()
        if not add and not drop:
            session.notify("Type a goal to add.", ok=False)
            return _back(form, "goals")
        try:
            await self.wiki.goals([add] if add else [], [drop] if drop else [])
        except ValueError as exc:
            session.notify(f"The wiki refused this: {exc}", ok=False)
        except WikiUnavailable:
            session.notify("The wiki is not answering: nothing was changed.", ok=False)
        else:
            session.notify("Goal added." if add else "Goal dropped (marked superseded).")
        return _back(form, "goals")

    async def about(self, request: Request, session: Session, form: FormData | None) -> Response:
        assert form is not None
        text = " ".join(str(form.get("text") or "").split())
        section = str(form.get("section") or "")
        if section not in SECTIONS:
            session.notify("Choose one of the sections.", ok=False)
        elif not text:
            session.notify("Type what you want to state.", ok=False)
        else:
            try:
                await self.wiki.statement(text, section)
            except ValueError as exc:
                session.notify(f"The wiki refused this: {exc}", ok=False)
            except WikiUnavailable:
                session.notify("The wiki is not answering: nothing was changed.", ok=False)
            else:
                session.notify(f"Added to your profile under {section}.")
        return _back(form, "about")

    async def taste(self, request: Request, session: Session, form: FormData | None) -> Response:
        assert form is not None
        note = str(form.get("note") or "").strip()
        if len(note) > NOTE_CHARS:
            session.notify(f"The note can be at most {NOTE_CHARS} characters.", ok=False)
        else:
            self.store.set_taste(note)
            session.notify("Taste note saved.")
        return _back(form, "taste")

    async def run(self, request: Request, session: Session, form: FormData | None) -> Response:
        assert form is not None
        scope = str(form.get("scope") or "wiki")
        if scope not in SCOPES:
            session.notify("Unknown choice of what to read.", ok=False)
        elif not self.p.settings.litellm_master_key.get_secret_value():
            session.notify("Ideas need LITELLM_MASTER_KEY in the portal. Run make up.", ok=False)
        elif not self.job.trigger("manual", scope):
            session.notify("Already ideating: wait for this run to finish.", ok=False)
        back = str(form.get("back") or PAGE)
        return RedirectResponse(f"{back if back in BACK else PAGE}#ideas", status_code=303)

    async def status(self, request: Request, session: Session, _: FormData | None) -> Response:
        progress = self.job.progress
        body = progress.as_dict() if progress else {"finished": True, "step": 0}
        return JSONResponse({**body, "running": self.job.running})
