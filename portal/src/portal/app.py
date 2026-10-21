"""Build the portal ASGI app: routes behind the guard middleware."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Scope
from wiki_mcp.audit import AuditLog
from wiki_mcp.auth.ratelimit import Lockout, RateLimiter
from wiki_mcp.net import client_ip, parse_networks

from portal.apps_routes import AppRoutes
from portal.auth import AuthRoutes
from portal.config import Settings
from portal.docs import DocsLibrary, DocsRoutes
from portal.drive_routes import DriveRoutes
from portal.home import LANDING
from portal.security import GuardMiddleware
from portal.sessions import SessionStore
from portal.web import Portal, build_templates
from portal.wiki import WikiRoutes
from portal.wikiclient import WikiClient

STATIC_DIR = Path(__file__).parent / "static"


def build_app(
    settings: Settings,
    *,
    audit: AuditLog | None = None,
    wiki: WikiClient | None = None,
) -> Starlette:
    """The Company Brain portal: sign-in, the wiki explorer (tree, search, graph, pages),
    proposal review, connected apps, the Google Drive export and the docs. The Second Brain's personal and model-backed
    pages (home, ideas, patterns, dreams, chat import and processing, assistant, dashboard,
    usage, API keys) are not wired: their modules stay for upstream merges."""
    if audit is None:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        audit = AuditLog(settings.state_dir / "audit.sqlite")
    portal = Portal(
        settings=settings,
        sessions=SessionStore(settings.session_idle_s, settings.session_max_s),
        audit=audit,
        lockout=Lockout(settings.login_max_failures, settings.login_lockout_base_s),
        templates=build_templates(),
    )
    auth = AuthRoutes(portal)
    if wiki is None:
        wiki = WikiClient(settings.wiki_api_url, settings.api_token())
    wiki_routes = WikiRoutes(portal, wiki)
    docs_routes = DocsRoutes(portal, DocsLibrary(settings.docs_dir))
    app_routes = AppRoutes(portal, wiki)
    drive_routes = DriveRoutes(portal, wiki)

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def landing(_: Request) -> Response:
        return RedirectResponse(LANDING, status_code=303)

    async def not_found(request: Request, exc: Exception) -> Response:
        status = exc.status_code if isinstance(exc, HTTPException) else 500
        message = "Page not found." if status == 404 else "Something went wrong."
        return portal.render(
            request, "error.html", portal.session_of(request), status=status, message=message
        )

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/login", auth.login_get, methods=["GET"]),
            Route("/login", auth.login_post, methods=["POST"]),
            Route("/reauth", auth.reauth_get, methods=["GET"]),
            Route("/reauth", auth.reauth_post, methods=["POST"]),
            Route("/logout", auth.logout, methods=["POST"]),
            Route("/", landing),
            *wiki_routes.routes(),
            *docs_routes.routes(),
            *app_routes.routes(),
            *drive_routes.routes(),
            Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"),
        ],
        exception_handlers={404: not_found, 405: not_found},
    )
    app.state.portal = portal

    trusted = parse_networks(settings.trusted_proxy_ips)

    def ip_of(scope: Scope) -> str:
        return client_ip(scope, trusted)

    client = wiki

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        yield
        await client.close()

    return Starlette(
        lifespan=lifespan,
        routes=[
            Mount(
                "/",
                app=GuardMiddleware(
                    app,
                    ip_of=ip_of,
                    allowed_hosts={urlsplit(settings.origin).netloc, f"portal:{settings.port}"},
                    general=RateLimiter(settings.rate_limit_per_minute),
                    sensitive=RateLimiter(settings.sensitive_rate_limit_per_minute),
                    audit=audit,
                    https=settings.secure_cookies,
                ),
            )
        ],
    )
