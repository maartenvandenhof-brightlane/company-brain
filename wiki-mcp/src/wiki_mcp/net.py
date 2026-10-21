"""Client IP extraction that only trusts forwarding headers from configured proxies."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from contextvars import ContextVar

from starlette.types import Scope

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# The client IP of the request being served, set by each listener's middleware. The MCP SDK
# carries ASGI contextvars into tool handlers (as it does the auth context), so tool calls are
# audited with it.
CLIENT_IP: ContextVar[str | None] = ContextVar("client_ip", default=None)


def parse_networks(entries: Iterable[str]) -> list[IPNetwork]:
    return [ipaddress.ip_network(e, strict=False) for e in entries]


def _in(ip: str, networks: list[IPNetwork]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return str(value.decode("latin-1"))
    return None


def client_ip(scope: Scope, trusted: list[IPNetwork]) -> str:
    """The real client IP.

    Forwarding headers are honoured only when the direct peer is a trusted proxy
    (the cloudflared/caddy container, or the Docker gateway that Tailscale's
    host-side proxy arrives from). Otherwise they are ignored, so a public client
    cannot spoof its address to dodge rate limits or pollute the audit log.
    """
    peer = (scope.get("client") or ("unknown", 0))[0]
    if not trusted or not _in(peer, trusted):
        return str(peer)
    cf = _header(scope, b"cf-connecting-ip")
    if cf and _valid(cf):
        return cf.strip()
    xff = _header(scope, b"x-forwarded-for")
    if xff:
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        # Walk from the right, skipping our own proxies; first untrusted hop is the client.
        for hop in reversed(hops):
            if not _valid(hop):
                break
            if not _in(hop, trusted):
                return hop
    return str(peer)


def _valid(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    return True
