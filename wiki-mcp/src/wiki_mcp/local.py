"""The local listener: the setup wizard and settings page, the REST API (rest.py) and /health.

Compose publishes it on the host's 127.0.0.1 only and no tunnel ever reaches it; it also answers
only localhost Host headers, so a web page cannot DNS-rebind onto the wizard.

  GET  /health            {status, setup: true while no mode is saved, mode, adapters}; no auth
  GET  /setup             the admin password (first start), sign-in, then the wizard
                          (?step=mode|settings|check) or, once saved, the settings page
  POST /setup/password    set the admin password (only while none is set)
  POST /setup/login       POST /setup/logout
  POST /setup/mode        step 1: the connectivity mode
  POST /setup/settings    step 2: only the settings the chosen mode needs; for Google also the
                          OAuth client and the Keep key (stored in secrets/ right away) and the
                          Connect Google / Disconnect buttons (action=connect|disconnect); for
                          MCP the Tailscale auth key and hostname (stored in tailscale/ right
                          away: the sidecar logs in, so step 3 can test the public URL)
  GET  /setup/google/callback  Google's redirect back after Connect Google (no cookie needed:
                          the single-use state and the PKCE verifier tie it to the flow)
  POST /setup/start       step 3: save config.yaml; the supervisor starts or restarts only the
                          adapters that changed (no container restart)

Setup state (no config.yaml): every route but /setup and /health redirects to /setup. The admin
password is also the owner login of the MCP connector and the portal (secrets/admin_password_hash,
argon2id). Sessions live in memory (a restart signs out) with a CSRF token on every form; the
cookie is HttpOnly and SameSite=Strict, and a POST must come from a page of this listener.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
import jinja2
from starlette.applications import Starlette
from starlette.datastructures import FormData, UploadFile
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from wiki_mcp.auth.passwords import hash_password, verify_password
from wiki_mcp.auth.ratelimit import Lockout
from wiki_mcp.brain import (
    ADMIN_HASH,
    INBOXES,
    MODE_HELP,
    TS_AUTHKEY,
    TS_HOSTNAME,
    BrainConfig,
    Mode,
    owner_hash,
    prefill,
    read_secret,
    save_config,
    write_secret,
)
from wiki_mcp.config import Trust
from wiki_mcp.folder import FolderInbox
from wiki_mcp.gdrive import find_folder
from wiki_mcp.google import (
    DRIVE_SCOPE,
    TASKS_SCOPE,
    Google,
    GoogleError,
    parse_client,
)
from wiki_mcp.inbox import inbox_for
from wiki_mcp.public import public_url
from wiki_mcp.rest import build_rest_routes
from wiki_mcp.runtime import Runtime
from wiki_mcp.supervisor import Supervisor
from wiki_mcp.wiki.actor import PRIVATE_SCOPE, Actor

COOKIE = "brain_setup"
LOCAL_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
SESSION_S = 8 * 3600
MIN_PASSWORD = 12
MAX_FORM_BYTES = 16 * 1024
STEPS = ("mode", "settings", "check")
AUTHKEY = re.compile(r"tskey-[A-Za-z0-9-]{1,200}")
CSP = (
    # Connect Google posts to /setup/settings, which redirects to Google's consent page.
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://accounts.google.com; "
    "frame-ancestors 'none'; base-uri 'none'"
)
# Whoever signed in to the wizard is the owner: full access, private pages included.
ADMIN = Actor(
    "admin",
    Trust.OWNER,
    frozenset({"wiki:read", "wiki:write", "wiki:admin", PRIVATE_SCOPE}),
    human="owner",
)

Handler = Callable[[Request, "_Session", FormData], Awaitable[Response]]


@dataclass
class _Session:
    csrf: str
    expires: float
    authed: bool = False
    draft: dict[str, Any] = field(default_factory=dict)  # the wizard's answers until Start
    notice: str = ""


def writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(path, os.W_OK)


def _sentence(exc: Exception) -> str:
    text = str(exc)
    return text[:1].upper() + text[1:] + "."


class _SetupGate:
    """Setup state: everything but /setup and /health goes to the wizard."""

    def __init__(self, app: ASGIApp, supervisor: Supervisor) -> None:
        self.app = app
        self.supervisor = supervisor

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] == "http"
            and self.supervisor.config is None
            and path != "/health"
            and path != "/setup"
            and not path.startswith("/setup/")
        ):
            await RedirectResponse("/setup", status_code=303)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_local_app(
    rt: Runtime, supervisor: Supervisor, *, api_token: str, google: Google | None = None
) -> Starlette:
    settings = rt.settings
    google = google or Google(settings.secrets_dir)
    # Shown in the wizard: the company registers exactly this URI on its OAuth client.
    redirect_uri = settings.setup_url.rstrip("/") + "/google/callback"
    sessions: dict[str, _Session] = {}
    lockout = Lockout(settings.login_max_failures, settings.login_lockout_base_s)
    templates = jinja2.Environment(
        loader=jinja2.FileSystemLoader(Path(__file__).parent / "templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )

    # ------------------------------------------------------------ sessions

    def session_of(request: Request) -> _Session | None:
        session = sessions.get(request.cookies.get(COOKIE, ""))
        if session is not None and session.expires < time.monotonic():
            sessions.pop(request.cookies.get(COOKIE, ""), None)
            return None
        return session

    def new_session(*, authed: bool = False) -> tuple[str, _Session]:
        now = time.monotonic()
        for sid in [k for k, s in sessions.items() if s.expires < now]:
            del sessions[sid]
        if len(sessions) >= 200:  # anonymous form sessions go first
            for sid in [k for k, s in sessions.items() if not s.authed]:
                del sessions[sid]
        sid = secrets.token_urlsafe(32)
        sessions[sid] = _Session(secrets.token_urlsafe(32), now + SESSION_S, authed)
        return sid, sessions[sid]

    def signed_in(request: Request) -> Response:
        """Start a signed-in session under a new id and token (no session fixation)."""
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        sid, _ = new_session(authed=True)
        response = RedirectResponse("/setup", status_code=303)
        set_cookie(response, sid)
        return response

    def set_cookie(response: Response, sid: str) -> None:
        response.set_cookie(
            COOKIE, sid, max_age=SESSION_S, path="/", httponly=True, samesite="strict"
        )

    def same_origin(request: Request) -> bool:
        origin = request.headers.get("origin") or request.headers.get("referer", "")
        return urlsplit(origin).netloc == request.headers.get("host", "")

    async def read_form(request: Request, files: int) -> FormData | None:
        length = request.headers.get("content-length", "")
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if not length.isdigit() or int(length) > MAX_FORM_BYTES:
            return None
        if ctype != "application/x-www-form-urlencoded" and not (
            files and ctype == "multipart/form-data"
        ):
            return None
        return await request.form(max_files=files, max_fields=30)

    def render(template: str, **context: Any) -> HTMLResponse:
        html = templates.get_template(template).render(title=settings.wiki_title, **context)
        return HTMLResponse(
            html, headers={"Content-Security-Policy": CSP, "Cache-Control": "no-store"}
        )

    def form_post(
        *, authed: bool, files: int = 0
    ) -> Callable[[Handler], Callable[[Request], Awaitable[Response]]]:
        """A POST from one of our own forms: session, CSRF token, same origin; `authed` also
        requires a signed-in session; `files` allows that many uploads (multipart)."""

        def wrap(handler: Handler) -> Callable[[Request], Awaitable[Response]]:
            async def endpoint(request: Request) -> Response:
                session = session_of(request)
                form = await read_form(request, files)
                token = str(form.get("csrf", "")) if form is not None else ""
                if (
                    session is None
                    or form is None
                    or not same_origin(request)
                    or not hmac.compare_digest(token.encode(), session.csrf.encode())
                    or (authed and not session.authed)
                ):
                    rt.audit.event("setup", client="local", outcome="csrf_failed")
                    return RedirectResponse("/setup", status_code=303)
                return await handler(request, session, form)

            return endpoint

        return wrap

    # ------------------------------------------------------------ REST callers

    def actor_of(request: Request) -> Actor | None:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            presented = header[7:].strip().encode()
            if api_token and hmac.compare_digest(presented, api_token.encode()):
                return Actor(
                    settings.internal_client_name, Trust.AGENT, frozenset(settings.internal_scopes)
                )
            rt.audit.event("rest_auth", client="local", outcome="denied")
            return None
        session = session_of(request)
        return ADMIN if session is not None and session.authed else None

    # ------------------------------------------------------------ pages

    def draft_of(session: _Session) -> dict[str, Any]:
        if not session.draft:
            session.draft = prefill(supervisor.config, settings).as_dict()
        return session.draft

    async def checks(config: BrainConfig) -> list[tuple[bool | None, str, str]]:
        """(ok, what, detail); ok None is a warning. Nothing blocks Start: an adapter that
        cannot run yet waits (NotReady) and says why on the settings page."""
        out: list[tuple[bool | None, str, str]] = [
            (
                os.access(rt.store.root, os.W_OK),
                "Wiki folder",
                "writable" if os.access(rt.store.root, os.W_OK) else "not writable by the brain",
            ),
            (True, "Search index", f"{len(rt.index.all())} documents"),
        ]
        if config.mode in (Mode.MCP, Mode.BOTH):
            out.append(await public_check())
        if config.mode in (Mode.GDRIVE, Mode.BOTH):
            out += await google_checks(config)
        if config.mode is Mode.FOLDER:
            out += folder_checks()
        return out

    def folder_checks() -> list[tuple[bool | None, str, str]]:
        """The two folders, named as the host sees them (data/ is the BRAIN_DATA_DIR mount)."""
        export, inbox = settings.drive_dir / "Brain", FolderInbox(settings.drive_dir / "inbox")
        last = supervisor.details().get("folder", {}).get("last_sync")
        export_detail = f"last export {last} (UTC)" if last else "exported on Start"
        inbox_detail = ""
        if writable(inbox.path):
            c = inbox.counts()
            inbox_detail = f"{c['waiting']} waiting, {c['done']} imported, {c['refused']} refused"
            if c["skipped"]:
                inbox_detail += f"; skipped (not .md or .txt): {', '.join(c['skipped'])}"
        return [
            (ok, what, f"{where}: {detail if ok else 'not writable by the brain'}")
            for ok, what, where, detail in (
                (writable(export), "Export folder", "data/drive/Brain", export_detail),
                (writable(inbox.path), "Inbox folder", "data/drive/inbox", inbox_detail),
            )
        ]

    async def public_check() -> tuple[bool | None, str, str]:
        """The connector's URL, fetched the way a chat app does: out to the internet and back in
        through Funnel."""
        what, url = "Public MCP URL", public_url(settings)
        if url is None:
            return (
                None,
                what,
                "waiting for the Tailscale sidecar to log in with the auth key from step 2 "
                "(about a minute); reload this page",
            )
        try:
            async with httpx2.AsyncClient(timeout=10.0, follow_redirects=False) as http:
                r = await http.get(f"{url}/.well-known/oauth-protected-resource")
            if r.status_code == 200:
                return True, what, f"{url}/mcp answers from the internet"
            problem = f"HTTP {r.status_code}"
        except httpx2.HTTPError as exc:
            problem = type(exc).__name__
        if not supervisor.status().get("mcp"):
            return None, what, f"{url}/mcp starts with Start; then check here again ({problem})"
        return (
            False,
            what,
            f"{url}/mcp does not answer yet ({problem}): is Funnel allowed in the tailnet policy "
            "and HTTPS enabled? A new name can take a few minutes to resolve",
        )

    async def google_checks(config: BrainConfig) -> list[tuple[bool | None, str, str]]:
        s = config.gdrive
        account = google.account()
        out: list[tuple[bool | None, str, str]] = []
        if account is None or not google.has_scope(DRIVE_SCOPE):
            what = "enter the OAuth client and" if google.client() is None else "not connected:"
            out.append((False, "Google account", f"{what} use Connect Google in step 2"))
        else:
            out.append((True, "Google account", account["email"] or "connected"))
            try:
                await find_folder(google, s.folder)
                out.append((True, "Google Drive", f'folder "{s.folder}" found or created'))
            except GoogleError as exc:
                out.append((False, "Google Drive", str(exc)))
        if s.inbox == "keep":
            if google.keep_account() is None or not s.keep_user:
                out.append(
                    (False, "Google Keep", "upload the service account key and enter the user")
                )
                return out
        elif account is None or not google.has_scope(TASKS_SCOPE):
            out.append((False, "Google Tasks", "no Tasks access yet: use Connect Google in step 2"))
            return out
        what = {"keep": "Google Keep", "tasks": "Google Tasks"}[s.inbox]
        try:
            await inbox_for(google, s).check()
            out.append((True, what, "the inbox is readable"))
        except GoogleError as exc:
            out.append((False, what, str(exc)))
        return out

    def wizard(
        session: _Session,
        step: str,
        *,
        notice: str = "",
        error: str = "",
        checks: list[tuple[bool | None, str, str]] | None = None,
    ) -> Response:
        draft = draft_of(session)
        client = google.client()
        keep_key = google.keep_account()
        return render(
            "setup.html",
            view="wizard",
            step=step,
            csrf=session.csrf,
            draft=draft,
            modes=MODE_HELP,
            inboxes=INBOXES,
            saved=supervisor.config is not None,
            notice=notice,
            error=error,
            authkey_set=bool(read_secret(settings.tailscale_dir, TS_AUTHKEY)),
            client_id=client[0] if client else "",
            account=google.account(),
            keep_email=str(keep_key.get("client_email", "")) if keep_key else "",
            redirect_uri=redirect_uri,
            checks=checks or [],
        )

    async def setup_page(request: Request) -> Response:
        session = session_of(request)
        if not owner_hash(settings) or session is None or not session.authed:
            sid = ""
            if session is None:
                sid, session = new_session()
            view = "login" if owner_hash(settings) else "password"
            response = render("setup.html", view=view, csrf=session.csrf, error="")
            if sid:
                set_cookie(response, sid)
            return response
        step = request.query_params.get("step", "")
        if step not in STEPS:
            step = "mode" if supervisor.config is None else ""
        notice, session.notice = session.notice, ""
        if not step:
            return render(
                "setup.html",
                view="settings",
                csrf=session.csrf,
                config=supervisor.config,
                status=supervisor.status(),
                details=supervisor.details(),
                account=google.account(),
                public=public_url(settings),
                notice=notice,
                help=MODE_HELP,
            )
        if step == "check":
            config = BrainConfig.from_dict(draft_of(session))
            return wizard(session, step, notice=notice, checks=await checks(config))
        return wizard(session, step, notice=notice)

    @form_post(authed=False)
    async def set_password(request: Request, session: _Session, form: FormData) -> Response:
        if owner_hash(settings):  # only the first time; never overwrite from this form
            return RedirectResponse("/setup", status_code=303)
        password = str(form.get("password", ""))
        if len(password) < MIN_PASSWORD or password != str(form.get("confirm", "")):
            error = (
                f"Use at least {MIN_PASSWORD} characters."
                if len(password) < MIN_PASSWORD
                else "The two passwords differ."
            )
            return render("setup.html", view="password", csrf=session.csrf, error=error)
        digest = await asyncio.to_thread(hash_password, password[:1000])
        write_secret(settings.secrets_dir, ADMIN_HASH, digest)
        rt.audit.event("setup", client="local", outcome="admin_password_set")
        return signed_in(request)

    @form_post(authed=False)
    async def login(request: Request, session: _Session, form: FormData) -> Response:
        wait = lockout.blocked_for("admin")
        if wait > 0:
            return render(
                "setup.html",
                view="login",
                csrf=session.csrf,
                error=f"Too many failed attempts. Try again in {int(wait) + 1} seconds.",
            )
        ok = await asyncio.to_thread(
            verify_password, owner_hash(settings), str(form.get("password", ""))[:1000]
        )
        if not ok:
            lockout.fail("admin")
            rt.audit.event("setup_login", client="local", outcome="failed")
            return render("setup.html", view="login", csrf=session.csrf, error="Wrong password.")
        lockout.succeed("admin")
        rt.audit.event("setup_login", client="local", outcome="ok")
        return signed_in(request)

    @form_post(authed=True)
    async def logout(request: Request, session: _Session, form: FormData) -> Response:
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        response = RedirectResponse("/setup", status_code=303)
        response.delete_cookie(COOKIE, path="/")
        return response

    @form_post(authed=True)
    async def choose_mode(request: Request, session: _Session, form: FormData) -> Response:
        mode = str(form.get("mode", ""))
        if mode in {m.value for m in Mode}:
            draft_of(session)["mode"] = mode
            return RedirectResponse("/setup?step=settings", status_code=303)
        return RedirectResponse("/setup?step=mode", status_code=303)

    @form_post(authed=True, files=2)
    async def choose_settings(request: Request, session: _Session, form: FormData) -> Response:
        draft = draft_of(session)
        mode = Mode(draft["mode"])
        authkey = str(form.get("authkey", "")).strip()
        if mode in (Mode.MCP, Mode.BOTH):
            draft["mcp"] = {
                "tunnel": str(form.get("tunnel", "tailscale")),
                "hostname": str(form.get("hostname", "company-brain")),
            }
        if mode in (Mode.GDRIVE, Mode.BOTH):
            draft["gdrive"] = {
                **draft["gdrive"],
                **{
                    key: str(form[key])
                    for key in (
                        "folder",
                        "inbox",
                        "prefix",
                        "tasklist",
                        "poll_minutes",
                        "keep_user",
                    )
                    if key in form
                },
                "keep_delete": form.get("keep_delete") == "on",
            }
        try:
            config = BrainConfig.from_dict(draft)
            if authkey and not AUTHKEY.fullmatch(authkey):
                raise ValueError("a Tailscale auth key looks like tskey-auth-…")
            if mode in (Mode.GDRIVE, Mode.BOTH):
                await save_google_secrets(form)
        except ValueError as exc:
            return wizard(session, "settings", error=_sentence(exc))
        if mode in (Mode.MCP, Mode.BOTH):
            # The sidecar watches these files and logs in (again) when they change.
            if authkey:
                write_secret(settings.tailscale_dir, TS_AUTHKEY, authkey)
            write_secret(settings.tailscale_dir, TS_HOSTNAME, config.mcp.hostname)
        action = str(form.get("action", ""))
        if action == "disconnect":
            await google.disconnect()
            rt.audit.event("setup", client="local", outcome="google_disconnected")
            await supervisor.restart("gdrive")
            session.notice = (
                "Google disconnected: nothing is exported or read until you connect again."
            )
            return RedirectResponse("/setup?step=settings", status_code=303)
        if action == "connect" and mode in (Mode.GDRIVE, Mode.BOTH):
            scopes = [DRIVE_SCOPE] + ([TASKS_SCOPE] if draft["gdrive"]["inbox"] == "tasks" else [])
            try:
                url = google.authorize_url(redirect_uri, scopes)
            except GoogleError as exc:
                return wizard(session, "settings", error=_sentence(exc))
            return RedirectResponse(url, status_code=303)
        return RedirectResponse("/setup?step=check", status_code=303)

    async def save_google_secrets(form: FormData) -> None:
        """The OAuth client (typed, or its downloaded JSON) and the Keep service account key,
        straight to secrets/ (needed before Start: Connect Google uses the client)."""
        upload = form.get("client_json")
        if isinstance(upload, UploadFile) and upload.filename:
            google.set_client(*parse_client((await upload.read()).decode("utf-8", "replace")))
        else:
            client_id = str(form.get("client_id", "")).strip()
            secret = str(form.get("client_secret", "")).strip()
            saved = google.client()
            if client_id and not secret and saved and saved[0] == client_id:
                secret = saved[1]  # the secret field is left empty to keep the saved one
            if client_id and (client_id, secret) != saved:
                google.set_client(client_id, secret)
        key = form.get("keep_key")
        if isinstance(key, UploadFile) and key.filename:
            google.set_keep_account((await key.read()).decode("utf-8", "replace"))

    async def google_callback(request: Request) -> Response:
        query = request.query_params
        ok = False
        try:
            if query.get("error"):
                raise GoogleError(f"Google sign-in did not finish ({query['error'][:60]})")
            email = await google.finish(query.get("state", ""), query.get("code", ""), redirect_uri)
            ok, message = True, f"Connected to Google as {email or 'your account'}."
            await supervisor.restart("gdrive")  # adapters that waited for the sign-in start now
        except GoogleError as exc:
            message = _sentence(exc)
        rt.audit.event(
            "setup", client="local", outcome="google_connected" if ok else "google_failed"
        )
        return render("setup.html", view="google", ok=ok, message=message)

    @form_post(authed=True)
    async def start(request: Request, session: _Session, form: FormData) -> Response:
        draft = draft_of(session)
        try:
            config = BrainConfig.from_dict(draft)
        except ValueError:
            return RedirectResponse("/setup?step=mode", status_code=303)
        save_config(settings.config_file, config)
        rt.audit.event("setup", client="local", outcome="saved", mode=config.mode.value)
        await supervisor.apply(config)
        session.draft = {}
        session.notice = f"Saved. The brain now runs in {config.mode.value} mode."
        return RedirectResponse("/setup", status_code=303)

    async def health(_: Request) -> Response:
        config = supervisor.config
        return JSONResponse(
            {
                "status": "ok",
                "setup": config is None,
                "mode": config.mode.value if config else None,
                "adapters": supervisor.status(),
                "details": supervisor.details(),
            }
        )

    routes = [
        Route("/health", health),
        Route("/setup", setup_page, methods=["GET"]),
        Route("/setup/password", set_password, methods=["POST"]),
        Route("/setup/login", login, methods=["POST"]),
        Route("/setup/logout", logout, methods=["POST"]),
        Route("/setup/mode", choose_mode, methods=["POST"]),
        Route("/setup/settings", choose_settings, methods=["POST"]),
        Route("/setup/google/callback", google_callback, methods=["GET"]),
        Route("/setup/start", start, methods=["POST"]),
        *build_rest_routes(rt.store, rt.audit, actor_of),
    ]
    return Starlette(
        routes=routes,
        middleware=[
            Middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS),
            Middleware(_SetupGate, supervisor=supervisor),
        ],
    )
