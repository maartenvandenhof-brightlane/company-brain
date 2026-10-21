"""Settings > Assistant: the model the portal's assistant uses.

  GET  /settings/assistant   the current model and the choice of models
  POST /settings/assistant   save a choice ("" goes back to LibreChat's default preset)

One model for everything the portal asks of a model on the owner's behalf: generating ideas
and the portal's chat. Batch processing of imported chats keeps its own choice per run (the
Import page). Any model LiteLLM serves can be picked, the same list as LibreChat's menu.
"""

from __future__ import annotations

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from portal.assistant import Prefs, default_model, model_choices
from portal.sessions import Session
from portal.web import Portal, owner

PATH = "/settings/assistant"


class AssistantRoutes:
    def __init__(self, portal: Portal, prefs: Prefs) -> None:
        self.p = portal
        self.prefs = prefs

    def routes(self) -> list[Route]:
        o = owner(self.p)
        return [
            Route(PATH, o(self.page), methods=["GET"]),
            Route(PATH, o(self.save), methods=["POST"]),
        ]

    async def page(self, request: Request, session: Session, _: FormData | None) -> Response:
        models_dir = self.p.settings.models_dir
        presets, others = model_choices(models_dir)
        return self.p.render(
            request,
            "assistant.html",
            session,
            chosen=self.prefs.get("model") or "",
            current=self.prefs.model,
            default=default_model(models_dir),
            presets=presets,
            others=others,
        )

    async def save(self, request: Request, session: Session, form: FormData | None) -> Response:
        assert form is not None
        model = str(form.get("model") or "")
        presets, others = model_choices(self.p.settings.models_dir)
        if model and model not in {m for m, _ in presets} | set(others):
            session.notify("Choose one of the listed models.", ok=False)
            return RedirectResponse(PATH, status_code=303)
        self.prefs.set("model", model or None)
        self.p.audit.event(
            "assistant_model",
            client="portal",
            ip=self.p.ip(request),
            model=model or "default",
        )
        session.notify(f"The assistant now uses {self.prefs.model}.")
        return RedirectResponse(PATH, status_code=303)
