"""Owner login for the portal: the same username, argon2id hash and optional TOTP as
the wiki-mcp connector login (wiki_mcp.auth.passwords.verify_owner).

GET  /login     form (creates an anonymous session that carries the CSRF token)
POST /login     verify, then start a fresh session (new id and CSRF token)
GET  /reauth    confirm the password again before key changes
POST /reauth
POST /logout
"""

from __future__ import annotations

import asyncio
import hmac

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from wiki_mcp.auth.passwords import verify_owner

from portal.sessions import Session
from portal.web import Portal, owner, read_form, safe_next


class AuthRoutes:
    def __init__(self, portal: Portal) -> None:
        self.p = portal
        self.logout = owner(portal)(self._logout)
        self.reauth_get = owner(portal)(self._reauth_get)
        self.reauth_post = owner(portal)(self._reauth_post)

    @property
    def needs_totp(self) -> bool:
        return bool(self.p.settings.owner_totp_secret.get_secret_value())

    async def _verify(self, username: str, password: str, totp: str) -> bool:
        s = self.p.settings
        # argon2 takes tens of milliseconds of CPU: keep the event loop responsive.
        return await asyncio.to_thread(
            verify_owner,
            username=username[:200],
            password=password[:1000],
            totp_code=totp[:20],
            expected_username=s.owner_username,
            password_hash=s.password_hash(),
            totp_secret=s.owner_totp_secret.get_secret_value(),
        )

    async def _check(
        self, request: Request, username: str, form: FormData, event: str
    ) -> str | None:
        """Lockout + credential check. Returns an error message, or None on success."""
        ip = self.p.ip(request)
        keys = ("owner", f"ip:{ip}")
        wait = self.p.lockout.blocked_for(*keys)
        if wait > 0:
            self.p.audit.event(
                event, client="portal", ip=ip, outcome="locked", retry_after=int(wait)
            )
            return f"Too many failed attempts. Try again in {int(wait) + 1} seconds."
        ok = await self._verify(username, str(form.get("password", "")), str(form.get("totp", "")))
        if not ok:
            self.p.lockout.fail(*keys)
            self.p.audit.event(
                event, client="portal", ip=ip, outcome="failed", username=username[:200]
            )
            return "Wrong username, password or code."
        self.p.lockout.succeed(*keys)
        self.p.audit.event(event, client="portal", ip=ip, outcome="ok")
        return None

    # ------------------------------------------------------------------ login

    def _login_form(
        self,
        request: Request,
        session: Session,
        *,
        error: str = "",
        username: str = "",
        status: int = 200,
    ) -> Response:
        response = self.p.render(
            request,
            "login.html",
            None,
            status=status,
            login_csrf=session.csrf,
            error=error,
            username=username,
            needs_totp=self.needs_totp,
            next=safe_next(request.query_params.get("next")),
        )
        self.p.set_cookie(response, session)
        return response

    async def login_get(self, request: Request) -> Response:
        session = self.p.session_of(request)
        if session is not None and session.authenticated:
            return RedirectResponse(safe_next(request.query_params.get("next")), status_code=303)
        return self._login_form(request, session or self.p.sessions.new())

    async def login_post(self, request: Request) -> Response:
        session = self.p.session_of(request)
        form = await read_form(request)
        token = str(form.get("csrf", "")) if form is not None else ""
        if (
            session is None
            or form is None
            or not self.p.origin_ok(request)
            or not hmac.compare_digest(token.encode(), session.csrf.encode())
        ):
            self.p.audit.event(
                "login", client="portal", ip=self.p.ip(request), outcome="csrf_failed"
            )
            return self._login_form(
                request,
                self.p.sessions.new(),
                error="Your sign-in form expired. Try again.",
                status=403,
            )
        username = str(form.get("username", ""))
        error = await self._check(request, username, form, "login")
        if error is not None:
            status = 429 if error.startswith("Too many") else 401
            return self._login_form(
                request, session, error=error, username=username[:200], status=status
            )
        fresh = self.p.sessions.login(session, self.p.settings.owner_username)
        response = RedirectResponse(safe_next(str(form.get("next", ""))), status_code=303)
        self.p.set_cookie(response, fresh)
        return response

    # ------------------------------------------------------------------ re-auth

    async def _reauth_get(self, request: Request, session: Session, _: FormData | None) -> Response:
        return self.p.render(
            request,
            "reauth.html",
            session,
            error="",
            needs_totp=self.needs_totp,
            next=safe_next(request.query_params.get("next")),
        )

    async def _reauth_post(
        self, request: Request, session: Session, form: FormData | None
    ) -> Response:
        assert form is not None
        error = await self._check(request, self.p.settings.owner_username, form, "reauth")
        if error is not None:
            return self.p.render(
                request,
                "reauth.html",
                session,
                status=429 if error.startswith("Too many") else 401,
                error=error,
                needs_totp=self.needs_totp,
                next=safe_next(str(form.get("next", ""))),
            )
        fresh = self.p.sessions.login(session, self.p.settings.owner_username)
        response = RedirectResponse(safe_next(str(form.get("next", ""))), status_code=303)
        self.p.set_cookie(response, fresh)
        return response

    # ------------------------------------------------------------------ logout

    async def _logout(self, request: Request, session: Session, _: FormData | None) -> Response:
        self.p.sessions.drop(session)
        self.p.audit.event("logout", client="portal", ip=self.p.ip(request))
        response = RedirectResponse("/login", status_code=303)
        self.p.clear_cookie(response)
        return response
