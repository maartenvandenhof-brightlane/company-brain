"""Connected apps: the OAuth apps connected to the wiki's MCP endpoint, and whether they see
private pages.

  GET  /apps           each app with its private access: the toggle, what it consented to,
                       and the effect
  POST /apps/private   set one app's toggle: on | off | default (as consented)

The toggle is per app label in wiki-mcp (one app may have several client_ids) and applies
from the app's next request, without reconnecting. Changing it needs the password entered
within the last few minutes (re-auth); it is audited here and in wiki-mcp. LibreChat is not an
OAuth app: its access comes from INTERNAL_SCOPES.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from portal.home import when
from portal.sessions import Session
from portal.web import Portal, owner
from portal.wikiclient import WikiClient, WikiUnavailable

ACCESS = ("on", "off", "default")


def _when(ts: Any) -> str:
    if not isinstance(ts, int | float):
        return "never"
    return when(dt.datetime.fromtimestamp(ts, dt.UTC).isoformat())


class AppRoutes:
    def __init__(self, portal: Portal, wiki: WikiClient) -> None:
        self.p = portal
        self.wiki = wiki

    def routes(self) -> list[Route]:
        sensitive = owner(self.p, reauth=True)
        return [
            Route("/apps", owner(self.p)(self.page)),
            Route("/apps/private", sensitive(self.set_private), methods=["POST"]),
        ]

    @staticmethod
    def _back() -> Response:
        return RedirectResponse("/apps", status_code=303)

    async def page(self, request: Request, session: Session, _: FormData | None) -> Response:
        error = None
        try:
            body = await self.wiki.apps()
        except (WikiUnavailable, ValueError) as exc:
            body, error = {}, str(exc)
        apps = [
            {**a, "created": _when(a.get("created")), "last_used": _when(a.get("last_used"))}
            for a in body.get("apps") or []
        ]
        return self.p.render(
            request,
            "apps.html",
            session,
            apps=apps,
            internal=body.get("internal"),
            error=error,
            can_change=session.recently_authenticated(self.p.settings.reauth_window_s),
        )

    async def set_private(
        self, request: Request, session: Session, form: FormData | None
    ) -> Response:
        label = str(form.get("label", "")) if form is not None else ""
        access = str(form.get("access", "")) if form is not None else ""
        if not label or access not in ACCESS:
            return self._back()
        try:
            app = await self.wiki.set_private_access(label, access)
        except (WikiUnavailable, ValueError) as exc:
            session.notify(f"{label}: {exc}", ok=False)
            return self._back()
        self.p.audit.event(
            "private_access",
            client="portal",
            ip=self.p.ip(request),
            outcome="ok",
            app=label,
            access=access,
        )
        if app.get("private") is None and access == "default":
            session.notify(f"{label} follows its configured scopes again.")
            return self._back()
        sees = "sees private pages" if app.get("private") else "does not see private pages"
        how = "as it consented when it connected" if access == "default" else "from now on"
        session.notify(f"{label} {sees} {how}.")
        return self._back()
