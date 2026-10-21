"""Public listener: the only surface ever exposed, through the Tailscale Funnel sidecar.
It runs only in the mcp and both modes (McpPublicAdapter, started by the supervisor), once its
public URL is known: PUBLIC_BASE_URL / MCP_EXPOSURE_MODE if set, else the DNS name the sidecar
publishes in data/tailscale/dnsname after its login.

Routes: /mcp (OAuth bearer), OAuth AS endpoints (/authorize, /token, /register,
/revoke), discovery documents (/.well-known/...), /login, /health, and small
informational pages. Nothing else is served, and the internal bearer token is
not accepted here.
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from wiki_mcp.auth.cimd import fetch_client_metadata
from wiki_mcp.auth.login import LoginConfig, LoginHandler
from wiki_mcp.auth.provider import SCOPES, OAuthProvider
from wiki_mcp.auth.ratelimit import Lockout, RateLimiter
from wiki_mcp.brain import TS_DNSNAME, BrainConfig, Mode, owner_hash
from wiki_mcp.config import ExposureMode, ModeError, Settings, resolve_public_base_url
from wiki_mcp.mcp.tools import SERVER_INSTRUCTIONS, register
from wiki_mcp.net import CLIENT_IP, client_ip, parse_networks
from wiki_mcp.runtime import Runtime, uvicorn_server
from wiki_mcp.supervisor import NotReady, Supervisor

log = logging.getLogger(__name__)

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"strict-transport-security", b"max-age=31536000"),
]


class EdgeGuardMiddleware:
    """Per-IP rate limiting (stricter on credential endpoints) + security headers."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        ip_of: Any,
        general: RateLimiter,
        strict: RateLimiter,
        register: RateLimiter,
        audit: Any,
        https: bool,
    ) -> None:
        self.app = app
        self.ip_of = ip_of
        self.general = general
        self.strict = strict
        self.register = register
        self.audit = audit
        self.https = https

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        ip = self.ip_of(scope)
        scope.setdefault("state", {})["client_ip"] = ip
        token = CLIENT_IP.set(ip)
        try:
            await self._guarded(scope, receive, send, ip)
        finally:
            CLIENT_IP.reset(token)

    async def _guarded(self, scope: Scope, receive: Receive, send: Send, ip: str) -> None:
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        wait = self.general.check(f"g:{ip}")
        if not wait and method == "POST" and path in ("/login", "/token"):
            wait = self.strict.check(f"s:{ip}")
        if not wait and method == "POST" and path == "/register":
            wait = self.register.check(f"r:{ip}")
        if wait:
            self.audit.event("rate_limited", ip=ip, outcome="blocked", path=path)
            response = JSONResponse(
                {"error": "rate_limited", "error_description": "Too many requests"},
                status_code=429,
                headers={"Retry-After": str(int(wait) + 1)},
            )
            await response(scope, receive, send)
            return

        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                for key, value in SECURITY_HEADERS:
                    if key == b"strict-transport-security" and not self.https:
                        continue
                    if key not in present:
                        headers.append((key, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def public_url(settings: Settings) -> str | None:
    """The public base URL: the environment's, else https://<the sidecar's DNS name>."""
    if settings.public_base_url:
        return settings.public_base_url
    try:
        name = (settings.tailscale_dir / TS_DNSNAME).read_text(encoding="utf-8").strip()
        return resolve_public_base_url(ExposureMode.TAILSCALE, ts_hostname=name)
    except OSError:
        return None  # the sidecar has not logged in yet
    except ModeError:
        return None  # not a *.ts.net name: never used as the OAuth issuer


def build_public_app(rt: Runtime, base: str) -> Starlette:
    settings = rt.settings
    resource = f"{base}/mcp"
    host = urlsplit(base).netloc

    provider = OAuthProvider(
        settings.state_dir / "oauth.sqlite",
        issuer=base,
        resource=resource,
        audit=rt.audit,
        owner_username=settings.owner_username,
        access_ttl=settings.access_token_ttl_s,
        refresh_ttl=settings.refresh_token_ttl_s,
        code_ttl=settings.auth_code_ttl_s,
        allowed_redirect_hosts=settings.allowed_redirect_hosts,
        cimd_allowed_hosts=settings.cimd_allowed_hosts,
        cimd_fetch=fetch_client_metadata,
    )
    registration = ClientRegistrationOptions(
        enabled=True, valid_scopes=SCOPES, default_scopes=SCOPES
    )
    revocation = RevocationOptions(enabled=True)
    auth = AuthSettings(
        issuer_url=AnyHttpUrl(base),
        resource_server_url=AnyHttpUrl(resource),
        required_scopes=["wiki:read"],
        client_registration_options=registration,
        revocation_options=revocation,
        validate_token_resource=True,
    )
    server: MCPServer[Any] = MCPServer(
        "second-brain-wiki",
        instructions=SERVER_INSTRUCTIONS,
        auth_server_provider=provider,
        auth=auth,
    )
    register(server, rt.deps)
    mcp_app = server.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            # Loopback too, for development (the sidecar keeps the public Host header).
            # Harmless: /mcp always requires OAuth.
            allowed_hosts=[host, "127.0.0.1:*", "localhost:*"],
            allowed_origins=[base],
        ),
    )

    as_metadata = build_metadata(AnyHttpUrl(base), None, registration, revocation).model_dump(
        mode="json", exclude_none=True
    )
    as_metadata.update(
        {
            "issuer": base,  # exact string; RFC 8414/9207 compare issuers byte-for-byte
            "scopes_supported": SCOPES,
            "token_endpoint_auth_methods_supported": [
                "none",
                "client_secret_post",
                "client_secret_basic",
            ],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
        }
    )
    pr_metadata = {
        "resource": resource,
        "authorization_servers": [base],
        "scopes_supported": SCOPES,
        "bearer_methods_supported": ["header"],
        "resource_name": f"{settings.wiki_title} wiki",
    }

    trusted = parse_networks(settings.trusted_proxy_ips)

    def ip_of(scope: Scope) -> str:
        return client_ip(scope, trusted)

    login = LoginHandler(
        provider,
        LoginConfig(
            owner_username=settings.owner_username,
            password_hash=owner_hash(settings),
            totp_secret=settings.owner_totp_secret.get_secret_value(),
            wiki_title=settings.wiki_title,
            secure_cookies=base.startswith("https://"),
        ),
        rt.audit,
        Lockout(settings.login_max_failures, settings.login_lockout_base_s),
        ip_of,
    )

    async def as_meta(_: Request) -> Response:
        return JSONResponse(as_metadata, headers={"Cache-Control": "public, max-age=3600"})

    async def pr_meta(_: Request) -> Response:
        return JSONResponse(pr_metadata, headers={"Cache-Control": "public, max-age=3600"})

    authenticator = ClientAuthenticator(provider)

    async def revoke(request: Request) -> Response:
        """RFC 7009 revocation. Replaces the SDK route, whose request model wrongly
        requires `client_secret` and so rejects public (PKCE) clients."""
        try:
            client = await authenticator.authenticate_request(request)
        except AuthenticationError as exc:
            return JSONResponse({"error": "invalid_client", "error_description": exc.message}, 401)
        form = await request.form()
        token = str(form.get("token", ""))
        if not token or len(token) > 512:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "token required"}, 400
            )
        found = await provider.load_access_token(token) or await provider.load_refresh_token(
            client, token
        )
        if found is not None and found.client_id == client.client_id:
            await provider.revoke_token(found)
        return Response(status_code=200, headers={"Cache-Control": "no-store"})

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def robots(_: Request) -> Response:
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    title = html.escape(settings.wiki_title)

    async def home(_: Request) -> Response:
        return HTMLResponse(
            f"<!doctype html><meta charset=utf-8><meta name=robots content=noindex><title>{title}</title>"
            f"<p>{title} MCP server. Add <code>{html.escape(resource)}</code> as a custom connector "
            "in your AI app.</p>"
        )

    async def wiki_page(request: Request) -> Response:
        did = html.escape(request.path_params["path"][:300])
        return HTMLResponse(
            f"<!doctype html><meta charset=utf-8><meta name=robots content=noindex><title>{title}</title>"
            f"<p>This link points to <code>{did}</code> in a private wiki. "
            "Ask your assistant to <em>fetch</em> it.</p>"
        )

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with server.session_manager.run():
            yield
        provider.close()

    app = Starlette(
        routes=[
            # CORS on discovery/revocation like the SDK's own routes: browser-based
            # clients (e.g. MCP Inspector) run the OAuth flow from JavaScript.
            Route(
                "/.well-known/oauth-authorization-server",
                cors_middleware(as_meta, ["GET", "OPTIONS"]),
                methods=["GET", "OPTIONS"],
            ),
            Route(
                "/.well-known/oauth-protected-resource",
                cors_middleware(pr_meta, ["GET", "OPTIONS"]),
                methods=["GET", "OPTIONS"],
            ),
            Route(
                "/.well-known/oauth-protected-resource/mcp",
                cors_middleware(pr_meta, ["GET", "OPTIONS"]),
                methods=["GET", "OPTIONS"],
            ),
            Route(
                "/revoke", cors_middleware(revoke, ["POST", "OPTIONS"]), methods=["POST", "OPTIONS"]
            ),
            Route("/health", health),
            Route("/robots.txt", robots),
            Route("/login", login.get, methods=["GET"]),
            Route("/login", login.post, methods=["POST"]),
            Route("/page/{path:path}", wiki_page),
            Route("/", home),
            Mount("/", app=mcp_app),
        ],
    )
    guarded = EdgeGuardMiddleware(
        app,
        ip_of=ip_of,
        general=RateLimiter(settings.rate_limit_per_minute),
        strict=RateLimiter(settings.login_rate_limit_per_minute),
        register=RateLimiter(20, period=3600),
        audit=rt.audit,
        https=base.startswith("https://"),
    )
    outer = Starlette(routes=[Mount("/", app=guarded)], lifespan=lifespan)
    outer.state.provider = provider
    return outer


class McpPublicAdapter:
    """The public MCP listener as a supervised adapter: serves only in the mcp and both modes.
    Each start builds a fresh app (an MCP session manager runs once). `follow` restarts it when
    the sidecar publishes its DNS name (first login, new hostname)."""

    name = "mcp"
    modes = frozenset({Mode.MCP, Mode.BOTH})
    section = "mcp"

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.server: uvicorn.Server | None = None
        self.task: asyncio.Task[None] | None = None
        self.base: str | None = None  # the public URL of the last start

    async def start(self, config: BrainConfig) -> None:
        s = self.rt.settings
        self.base = base = public_url(s)
        if not base:
            raise NotReady(
                "waiting for the Tailscale sidecar to log in: enter the auth key in the settings"
            )
        self.rt.store.public_base_url = base  # page links in tool results
        server = uvicorn_server(
            build_public_app(self.rt, base), s.public_host, s.public_port, s.log_level
        )
        self.server, self.task = server, asyncio.create_task(server.serve())
        while not server.started:  # running means listening (or the start error)
            await asyncio.sleep(0.02)
            if self.task.done():
                self.task.result()
                raise RuntimeError("the public listener did not start")
        log.info("public MCP endpoint: %s/mcp", base)

    async def stop(self) -> None:
        if self.server is not None and self.task is not None:
            self.server.should_exit = True
            await self.task
        self.server = self.task = None

    async def follow(self, supervisor: Supervisor, every: float = 5.0) -> None:
        """Restart on a new public URL; the sidecar writes it about a minute after its login."""
        while True:
            await asyncio.sleep(every)
            config = supervisor.config
            if (
                config is not None
                and config.mode in self.modes
                and public_url(self.rt.settings) != self.base
            ):
                await supervisor.restart(self.section)
