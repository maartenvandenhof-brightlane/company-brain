"""Endpoint dashboard: URLs per mode and the probe verdicts."""

from __future__ import annotations

from collections.abc import Callable

import httpx2

from portal.config import Settings
from portal.dashboard import Dashboard

CF = {
    "PORTAL_PUBLIC_URL": "",
    "ui_access_mode": "cloudflare",
    "ui_hostname": "chat.example.com",
    "portal_hostname": "hub.example.com",
    "mcp_exposure_mode": "cloudflare",
    "mcp_hostname": "mcp.example.com",
    "librechat_url": "http://librechat:3080",
    "litellm_url": "http://litellm:4000",
    "wiki_mcp_internal_url": "http://wiki-mcp:8081",
    "wiki_mcp_public_url": "http://wiki-mcp:8080",
}


def fake(routes: dict[str, httpx2.Response | Exception]) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        result = routes.get(str(request.url))
        if isinstance(result, Exception):
            raise result
        return result or httpx2.Response(404)

    return httpx2.MockTransport(handler)


def test_urls_follow_the_modes(make_settings: Callable[..., Settings]) -> None:
    board = Dashboard(make_settings(**CF))
    links = {link.label: link for e in board.endpoints() for link in e.links}
    assert links["Open"].url == "https://chat.example.com"
    assert links["Portal URL"].url == "https://hub.example.com"
    assert (
        links["Connector URL"].url == "https://mcp.example.com/mcp" and links["Connector URL"].copy
    )
    assert (
        links["Protected resource metadata"].url
        == "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
    )
    assert links["Health"].url == "https://mcp.example.com/health"


def test_no_mcp_endpoint_in_none_mode(make_settings: Callable[..., Settings]) -> None:
    board = Dashboard(make_settings(**{**CF, "mcp_exposure_mode": "none"}))
    assert board.mcp_url is None
    assert all(not p.url.startswith("https://mcp.") for p in board.probes())
    assert [e.links for e in board.endpoints() if e.title == "MCP connector"] == [[]]


async def test_probe_verdicts(make_settings: Callable[..., Settings]) -> None:
    access = httpx2.Response(302, headers={"location": "https://team.cloudflareaccess.com/x"})
    transport = fake(
        {
            "http://librechat:3080/health": httpx2.Response(200),
            "http://litellm:4000/health/liveliness": httpx2.Response(503),
            "http://wiki-mcp:8081/health": httpx2.ConnectError("refused"),
            "http://wiki-mcp:8080/health": httpx2.Response(200),
            "https://chat.example.com/": access,
            "https://mcp.example.com/.well-known/oauth-protected-resource/mcp": httpx2.Response(
                200, json={"resource": "https://mcp.example.com/mcp"}
            ),
            "https://mcp.example.com/.well-known/oauth-authorization-server": httpx2.Response(
                200, json={"issuer": "https://other.example.com"}
            ),
            "https://mcp.example.com/health": httpx2.ConnectError(
                "[Errno -2] Name or service not known"
            ),
        }
    )
    board = Dashboard(make_settings(**CF), transport=transport)
    st, _ = await board.statuses()
    assert st["librechat"].state == "up"
    assert st["litellm"].state == "down"
    assert st["wiki_internal"].state == "down"
    assert st["ui_public"].state == "up" and "Access" in st["ui_public"].detail
    assert st["mcp_prm"].state == "up"
    assert st["mcp_as"].state == "down" and "issuer" in st["mcp_as"].detail
    assert st["mcp_health"].state == "unknown"


async def test_statuses_are_cached(make_settings: Callable[..., Settings]) -> None:
    calls = 0

    def handler(_: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(200)

    board = Dashboard(make_settings(**CF), transport=httpx2.MockTransport(handler))
    await board.statuses()
    first = calls
    await board.statuses()
    assert calls == first
    await board.statuses(refresh=True)
    assert calls == 2 * first
