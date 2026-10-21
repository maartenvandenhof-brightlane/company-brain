"""Internal listener: static bearer token for LibreChat on the Docker network only.

This app is served on a separate port that is never published to the host or
exposed by any edge. The public listener does not know this token at all.
The portal's JSON API (wiki_mcp.portal_api: read-only except chat imports) is mounted
here too, behind its own token. It opens the OAuth state (oauth.sqlite) to list the connected
apps and switch their private access.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from wiki_mcp.audit import AuditLog
from wiki_mcp.auth.provider import OAuthProvider
from wiki_mcp.mcp.tools import SERVER_INSTRUCTIONS, ToolDeps, register
from wiki_mcp.net import CLIENT_IP
from wiki_mcp.portal_api import PortalTokenMiddleware, build_portal_api
from wiki_mcp.wiki.actor import PRIVATE_SCOPE


class InternalBearerMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str,
        client_name: str,
        scopes: list[str],
        audit: AuditLog,
        oauth: OAuthProvider | None = None,
    ) -> None:
        self.app = AuthContextMiddleware(app)
        self.token = token.encode()
        self.client_name = client_name
        self.scopes = scopes
        self.audit = audit
        self.oauth = oauth  # the owner's private-access toggle applies per request

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        header = ""
        for key, value in scope.get("headers", []):
            if key == b"authorization":
                header = value.decode("latin-1")
                break
        presented = header[7:].strip().encode() if header.lower().startswith("bearer ") else b""
        if not self.token or not presented or not hmac.compare_digest(presented, self.token):
            ip = (scope.get("client") or ("?", 0))[0]
            self.audit.event("internal_auth", client=self.client_name, ip=ip, outcome="denied")
            response = JSONResponse(
                {"error": "invalid_token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
            await response(scope, receive, send)
            return
        scope["user"] = AuthenticatedUser(
            AccessToken(
                token="internal",  # noqa: S106 - placeholder, not a credential
                client_id=f"internal:{self.client_name}",
                scopes=(
                    self.oauth.scopes_for_label(self.client_name, self.scopes)
                    if self.oauth is not None
                    else self.scopes
                ),
                claims={"client_name": self.client_name, "trust": "agent", "human": None},
            )
        )
        # The Docker network peer (LibreChat's container): nothing forwards to this listener.
        token = CLIENT_IP.set((scope.get("client") or ("?", 0))[0])
        try:
            await self.app(scope, receive, send)
        finally:
            CLIENT_IP.reset(token)


def build_internal_app(
    deps: ToolDeps,
    *,
    token: str,
    client_name: str,
    scopes: list[str],
    allowed_hosts: list[str],
    portal_token: str = "",
    oauth_db: Path | None = None,
    drive_dir: Path | None = None,
) -> tuple[Starlette, MCPServer[Any]]:
    server: MCPServer[Any] = MCPServer("second-brain-wiki", instructions=SERVER_INSTRUCTIONS)
    register(server, deps)
    mcp_app = server.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[],
        ),
    )

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok", "listener": "internal"})

    # The OAuth state the public listener writes; the portal API reads it and sets the toggles.
    oauth = None
    if oauth_db is not None:
        oauth = OAuthProvider(oauth_db, issuer="", resource="", audit=deps.audit, owner_username="")
        oauth.internal_labels.add(client_name)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with server.session_manager.run():
            yield
        if oauth is not None:
            oauth.close()

    inner = Starlette(routes=[Route("/health", health), Mount("/", app=mcp_app)])
    guarded = InternalBearerMiddleware(
        inner, token=token, client_name=client_name, scopes=scopes, audit=deps.audit, oauth=oauth
    )
    routes = [Mount("/", app=guarded)]
    if portal_token:
        # The portal's API has its own token; LibreChat's is not accepted there.
        api = build_portal_api(
            deps.store,
            deps.audit,
            oauth=oauth,
            internal=(client_name, PRIVATE_SCOPE in scopes),
            sessions=deps.sessions,
            drive_dir=drive_dir,
        )
        portal = PortalTokenMiddleware(api, token=portal_token, audit=deps.audit)
        routes.insert(0, Mount("/api/portal/v1", app=portal))
    app = Starlette(routes=routes, lifespan=lifespan)
    return app, server
