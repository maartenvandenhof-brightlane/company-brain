"""Shared web plumbing: templates, sessions on requests, and the `owner` decorator.

Every page except /login, /health and /static requires the owner session. Every
POST additionally requires the session's CSRF token in the form and an Origin (or
Referer) header equal to the portal's configured URL.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import jinja2
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from wiki_mcp.audit import AuditLog
from wiki_mcp.auth.ratelimit import Lockout

from portal.config import Settings
from portal.sessions import COOKIE_PLAIN, COOKIE_SECURE, Session, SessionStore

Handler = Callable[[Request, Session, FormData | None], Awaitable[Response]]
MAX_FORM_BYTES = 64 * 1024
MiB = 1024 * 1024


@dataclass
class Portal:
    settings: Settings
    sessions: SessionStore
    audit: AuditLog
    lockout: Lockout
    templates: jinja2.Environment

    @property
    def cookie_name(self) -> str:
        return COOKIE_SECURE if self.settings.secure_cookies else COOKIE_PLAIN

    def session_of(self, request: Request, *, touch: bool = True) -> Session | None:
        return self.sessions.get(request.cookies.get(self.cookie_name), touch=touch)

    def set_cookie(self, response: Response, session: Session) -> None:
        response.set_cookie(
            self.cookie_name,
            session.id,
            max_age=int(self.settings.session_max_s),
            path="/",
            httponly=True,
            secure=self.settings.secure_cookies,
            samesite="strict",
        )

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(
            self.cookie_name, path="/", secure=self.settings.secure_cookies, httponly=True
        )

    def render(
        self,
        request: Request,
        template: str,
        session: Session | None,
        *,
        status: int = 200,
        **context: Any,
    ) -> HTMLResponse:
        html = self.templates.get_template(template).render(
            title=self.settings.wiki_title,
            session=session,
            csrf=session.csrf if session else "",
            notices=session.pop_notices() if session else [],
            path=request.url.path,
            **context,
        )
        return HTMLResponse(html, status_code=status)

    def render_parts(
        self,
        request: Request,
        template: str,
        session: Session,
        blocks: tuple[str, ...],
        **context: Any,
    ) -> dict[str, str]:
        """Some blocks of a page template, rendered exactly as in the full page (the wiki
        workspace loads pages this way). Flash messages are left for the next full page."""
        tmpl = self.templates.get_template(template)
        ctx = tmpl.new_context(
            {
                "title": self.settings.wiki_title,
                "session": session,
                "csrf": session.csrf,
                "notices": [],
                "path": request.url.path,
                **context,
            }
        )
        # Run the template once so its top-level imports and macros exist for the blocks.
        for _ in tmpl.root_render_func(ctx):
            pass
        return {
            name: "".join(tmpl.blocks[name](ctx)) if name in tmpl.blocks else "" for name in blocks
        }

    def ip(self, request: Request) -> str:
        return str(request.scope.get("state", {}).get("client_ip", "?"))

    def origin_ok(self, request: Request) -> bool:
        """The request must come from a page of this portal (defence in depth next to CSRF)."""
        origin = request.headers.get("origin")
        if origin is None or origin == "null":
            referer = request.headers.get("referer", "")
            parts = urlsplit(referer)
            origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""
        return hmac.compare_digest(origin.lower().encode(), self.settings.origin.encode())


def build_templates() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.PackageLoader("portal", "templates"),
        autoescape=True,  # every template, whatever its extension
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["urlq"] = lambda v: quote(str(v), safe="/")
    return env


def safe_next(target: str | None) -> str:
    """Only same-site paths: never //host, scheme URLs or backslash tricks."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    if any(ord(c) < 32 for c in target) or len(target) > 500:
        return "/"
    return target


def login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(safe_next(target))}", status_code=303)


def too_large(request: Request, limit: int) -> bool:
    length = request.headers.get("content-length", "")
    return length.isdigit() and int(length) > limit + MAX_FORM_BYTES


async def read_upload(request: Request, limit: int) -> FormData | None:
    """A multipart form with at most one file; the size must be declared up front
    (uvicorn stops reading at the declared Content-Length)."""
    length = request.headers.get("content-length", "")
    if not length.isdigit() or int(length) > limit + MAX_FORM_BYTES:
        return None
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "multipart/form-data":
        return None
    return await request.form(max_files=1, max_fields=5)


async def read_form(request: Request) -> FormData | None:
    length = request.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > MAX_FORM_BYTES):
        return None
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/x-www-form-urlencoded":
        return None
    return await request.form(max_files=0, max_fields=50)


def owner(
    portal: Portal, *, reauth: bool = False, upload: int | None = None
) -> Callable[[Handler], Callable[[Request], Awaitable[Response]]]:
    """Require the owner session; for POST also the CSRF token and a same-origin request.

    With reauth=True the owner must have entered the password within reauth_window_s
    (key changes); otherwise the browser is sent to /reauth first. With upload=<bytes>
    the POST is a multipart form carrying one file of at most that size.
    """

    def wrap(handler: Handler) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            session = portal.session_of(request)
            if session is None or not session.authenticated:
                if request.method == "GET":
                    return login_redirect(request)
                return portal.render(
                    request,
                    "error.html",
                    None,
                    status=403,
                    message="Your session has expired. Sign in again.",
                )
            form: FormData | None = None
            if request.method == "POST":
                same_origin = portal.origin_ok(request)  # before reading a large body
                if upload and same_origin and too_large(request, upload):
                    return portal.render(
                        request,
                        "error.html",
                        session,
                        status=413,
                        message=f"The file is larger than {upload // MiB} MB. "
                        "Upload conversations.json from the export instead of the whole zip.",
                    )
                if not same_origin:
                    form = None
                elif upload:
                    form = await read_upload(request, upload)
                else:
                    form = await read_form(request)
                token = form.get("csrf", "") if form is not None else ""
                token = token if isinstance(token, str) else ""
                if form is None or not hmac.compare_digest(token.encode(), session.csrf.encode()):
                    portal.audit.event(
                        "csrf",
                        client="portal",
                        ip=portal.ip(request),
                        outcome="blocked",
                        path=request.url.path,
                    )
                    return portal.render(
                        request,
                        "error.html",
                        session,
                        status=403,
                        message="This form has expired. Go back, reload the page and try again.",
                    )
            if reauth and not session.recently_authenticated(portal.settings.reauth_window_s):
                back = request.headers.get("referer", "")
                target = urlsplit(back).path if back else request.url.path
                return RedirectResponse(f"/reauth?next={quote(safe_next(target))}", status_code=303)
            return await handler(request, session, form)

        return endpoint

    return wrap
