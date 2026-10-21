"""Request guard for every portal request: client IP, host check, rate limits, headers.

The CSP allows only same-origin scripts, styles and images (no inline code at all),
so even if sanitised wiki HTML somehow carried a script or an event handler, the
browser would refuse to run it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from wiki_mcp.audit import AuditLog
from wiki_mcp.auth.ratelimit import RateLimiter

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'"
)
HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    # Not "no-referrer": under that policy browsers send `Origin: null` on every POST, and
    # the CSRF origin check (portal.web.Portal.origin_ok) would reject all forms.
    # same-origin still sends nothing to other sites.
    (b"referrer-policy", b"same-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    (b"x-robots-tag", b"noindex, nofollow"),
]
HSTS = (b"strict-transport-security", b"max-age=31536000")
# Credential or secret handling, and heavy uploads: stricter per-IP limit.
SENSITIVE_PREFIXES = ("/login", "/reauth", "/keys", "/import")


def host_allowed(host: str, allowed: set[str]) -> bool:
    host = host.lower()
    if host in allowed:
        return True
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    # Tailscale Serve proxies from the host's loopback and may pass the local Host header.
    return name in {"127.0.0.1", "localhost", "[::1]"}


class GuardMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        ip_of: Callable[[Scope], str],
        allowed_hosts: set[str],
        general: RateLimiter,
        sensitive: RateLimiter,
        audit: AuditLog,
        https: bool,
    ) -> None:
        self.app = app
        self.ip_of = ip_of
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.general = general
        self.sensitive = sensitive
        self.audit = audit
        self.https = https

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if path == "/health":  # container healthcheck: no host check, no rate limit
            await self.app(scope, receive, send)
            return
        ip = self.ip_of(scope)
        scope.setdefault("state", {})["client_ip"] = ip
        host = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"host"), "")
        if not host_allowed(host, self.allowed_hosts):
            await PlainTextResponse("Unknown host", status_code=421)(scope, receive, send)
            return
        wait = self.general.check(f"g:{ip}")
        if not wait and scope.get("method") == "POST" and path.startswith(SENSITIVE_PREFIXES):
            wait = self.sensitive.check(f"s:{ip}")
        if wait:
            self.audit.event("rate_limited", client="portal", ip=ip, outcome="blocked", path=path)
            response = PlainTextResponse(
                "Too many requests. Wait a moment and try again.",
                status_code=429,
                headers={"Retry-After": str(int(wait) + 1)},
            )
            await self._with_headers(response, scope, receive, send)
            return
        await self._with_headers(self.app, scope, receive, send)

    async def _with_headers(self, app: Any, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                extra = [*HEADERS, HSTS] if self.https else HEADERS
                headers += [(k, v) for k, v in extra if k not in present]
                if b"cache-control" not in present:
                    # Pages carry session data: never stored. Static files (CSS, JS, fonts)
                    # are public: kept, but revalidated by ETag on every use.
                    static = str(scope.get("path", "")).startswith("/static/")
                    headers.append((b"cache-control", b"no-cache" if static else b"no-store"))
                message["headers"] = headers
            await send(message)

        await app(scope, receive, send_with_headers)
