"""The portal's home: an assistant's desk next to LibreChat, not a second chat app.

  GET /      the wiki workspace is the portal's main screen: redirects to /wiki
  GET /home  the Ask box, the top ideas, follow-ups, what waits for review, recent changes
  GET /ask   opens LibreChat on a new chat with the question (and the default preset)

LibreChat v0.8.7 reads `/c/new?spec=<preset>&prompt=<text>&submit=true`: the prompt is put
in the message box and sent (unless interface.autoSubmitFromUrl is false), and the other
parameters, `spec` among them, are applied as the chat's settings. /ask builds that URL on
the server, so the page needs no script for it.

The home page shows only the HOME_IDEAS best open ideas; the rest, the answers, goals and
runs are on the Ideas tab (ideas/routes.py). Idea text is model output: it is rendered
through portal.render like wiki content (ideas/view.py). Every wiki call can fail on its
own; the page then shows what it has and says what is missing.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from portal.assistant import default_preset
from portal.ideas.store import IdeaStore
from portal.ideas.view import cards, titles
from portal.sessions import Session
from portal.web import Portal, owner
from portal.wiki import page_href
from portal.wikiclient import WikiClient, WikiUnavailable

LANDING = "/wiki"
MAX_ASK_CHARS = 4000
RECENT_SHOWN = 6
HOME_IDEAS = 3


def ask_url(ui_url: str, spec: str | None, question: str) -> str:
    """LibreChat's new-chat URL with the question sent right away."""
    params = {"spec": spec} if spec else {}
    if question:
        params |= {"prompt": question, "submit": "true"}
    return f"{ui_url}/c/new" + (f"?{urlencode(params)}" if params else "")


def when(iso: str) -> str:
    """An ISO timestamp as "28 Sep, 14:02" (UTC; the year only when it is not this one)."""
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(dt.UTC)
    except ValueError:
        return iso[:16]
    shown = f"{t.day} {t:%b}" + ("" if t.year == dt.datetime.now(dt.UTC).year else f" {t.year}")
    return f"{shown}, {t:%H:%M}"


class HomeRoutes:
    def __init__(self, portal: Portal, wiki: WikiClient, store: IdeaStore) -> None:
        self.p = portal
        self.wiki = wiki
        self.store = store

    def routes(self) -> list[Route]:
        o = owner(self.p)
        return [
            Route("/", self.landing),
            Route("/home", o(self.home)),
            Route("/ask", o(self.ask)),
        ]

    async def landing(self, _: Request) -> Response:
        """The portal opens on the wiki workspace (which asks to sign in when needed)."""
        return RedirectResponse(LANDING, status_code=303)

    async def ask(self, request: Request, session: Session, _: Any) -> Response:
        question = request.query_params.get("q", "").strip()[:MAX_ASK_CHARS]
        preset = default_preset(self.p.settings.models_dir)
        spec = str(preset["name"]) if preset else None
        return RedirectResponse(ask_url(self.p.settings.ui_url, spec, question), status_code=303)

    async def home(self, request: Request, session: Session, _: Any) -> Response:
        settings = self.p.settings
        problems: list[str] = []
        known = await titles(self.wiki)
        if known is None:
            problems.append("the wiki's page list")
            known = {}

        open_ideas = self.store.ideas(by="score")
        top = [i for i in open_ideas if i.kind != "follow-up"][:HOME_IDEAS]
        follow_ups = [i for i in open_ideas if i.kind == "follow-up"]

        review: dict[str, int] | None = None
        try:
            processing = await self.wiki.processing()
            drafts = await self.wiki.drafts()
            proposals = await self.wiki.proposals()
            review = {
                "unprocessed": int(processing.get("unprocessed") or 0),
                "refile": int(processing.get("filed_before_playbook") or 0),
                "drafts": len(drafts),
                "proposals": len(proposals),
            }
        except (WikiUnavailable, ValueError):
            problems.append("what waits for review")

        changes: list[dict[str, Any]] = []
        try:
            for c in await self.wiki.recent(RECENT_SHOWN):
                pages = [
                    str(f["path"]).removesuffix(".md")
                    for f in c.get("files") or []
                    if str(f.get("path", "")).endswith(".md") and f.get("status") != "D"
                ]
                first = next((p for p in pages if p in known), None)
                changes.append(
                    {
                        "summary": c.get("summary", ""),
                        "when": when(str(c.get("date", ""))),
                        "href": page_href(first) if first else None,
                        "type": known[first][1] if first else "",
                        "more": max(0, len(pages) - 1),
                    }
                )
        except (WikiUnavailable, ValueError):
            problems.append("recent changes")

        preset = default_preset(settings.models_dir)
        return self.p.render(
            request,
            "home.html",
            session,
            cards=cards(top, known),
            more_ideas=max(0, len(open_ideas) - len(follow_ups) - len(top)),
            follow_ups=cards(follow_ups, known),
            review=review,
            changes=changes,
            problems=problems,
            preset_label=str(preset.get("label") or preset["name"]) if preset else "",
            chat_url=settings.ui_url,
        )
