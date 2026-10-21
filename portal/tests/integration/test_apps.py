"""Connected apps: each OAuth app's private access, toggled per app label (re-auth, CSRF)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
import pytest
from mcp.shared.auth import OAuthClientInformationFull
from wiki_mcp.auth.provider import OAuthProvider
from wiki_mcp.config import Settings as WikiSettings
from wiki_mcp.internal import build_internal_app
from wiki_mcp.runtime import create_runtime

from portal.app import build_app
from portal.config import Settings
from portal.wikiclient import WikiClient
from tests.conftest import ORIGIN, _find, csrf_of, login, serve_app
from tests.integration.conftest import PORTAL_TOKEN


@pytest.fixture
async def oauth(tmp_path: Path) -> AsyncIterator[tuple[str, OAuthProvider]]:
    """wiki-mcp's portal API over an OAuth state with one app (chatgpt) on two client_ids."""
    settings = WikiSettings(
        wiki_dir=tmp_path / "wiki",
        state_dir=tmp_path / "wiki-state",
        types_file=_find("config/wiki-types.yaml"),
        seed_dir=_find("wiki-mcp/seed"),
    )
    rt = await create_runtime(settings, run_background=False)
    db = settings.state_dir / "oauth.sqlite"
    provider = OAuthProvider(db, issuer="", resource="", audit=rt.audit, owner_username="")
    for cid in ("gpt-1", "gpt-2"):
        await provider.register_client(
            OAuthClientInformationFull(
                client_id=cid,
                client_name="ChatGPT",
                redirect_uris=["https://chatgpt.com/connector/oauth/cb"],  # type: ignore[list-item]
            )
        )
    provider._issue("gpt-1", ["wiki:read", "wiki:write"], None, "f1")
    app, _ = build_internal_app(
        rt.deps,
        token="l" * 40,
        client_name="librechat",
        scopes=["wiki:read"],
        allowed_hosts=["127.0.0.1:*"],
        portal_token=PORTAL_TOKEN,
        oauth_db=db,
    )
    async with serve_app(app) as url:
        yield url + "/api/portal/v1", provider
    provider.close()
    await rt.close()


@asynccontextmanager
async def portal(settings: Settings, wiki_url: str) -> AsyncIterator[httpx2.AsyncClient]:
    app = build_app(settings, wiki=WikiClient(wiki_url, PORTAL_TOKEN))
    async with serve_app(app) as url, httpx2.AsyncClient(base_url=url) as http:
        await login(http)
        yield http


async def toggle(http: httpx2.AsyncClient, access: str, **extra: str) -> httpx2.Response:
    page = await http.get("/apps")
    data = {"csrf": csrf_of(page.text), "label": "chatgpt", "access": access, **extra}
    return await http.post("/apps/private", data=data, headers={"Origin": ORIGIN})


async def test_list_and_toggle(
    make_settings: Callable[..., Settings], oauth: tuple[str, OAuthProvider]
) -> None:
    url, provider = oauth
    async with portal(make_settings(), url) as http:
        page = await http.get("/apps")
        assert page.status_code == 200
        assert "chatgpt" in page.text and "hidden" in page.text and "not ticked" in page.text
        assert "The app's provider sees what it reads." in page.text
        assert "LibreChat" in page.text and "INTERNAL_SCOPES" in page.text
        assert 'action="/apps/private"' in page.text

        r = await toggle(http, "on")
        assert r.status_code == 303 and r.headers["location"] == "/apps"
        assert provider.private_toggle("chatgpt") == "on"
        page = await http.get("/apps")
        assert "chatgpt sees private pages from now on." in page.text
        assert "sees them" in page.text and "turned on here" in page.text
        await toggle(http, "off")
        assert provider.private_toggle("chatgpt") == "off"
        await toggle(http, "default")
        assert provider.private_toggle("chatgpt") is None
        assert "as it consented" in (await http.get("/apps")).text

        # unknown apps and values change nothing
        await toggle(http, "on", label="nosuch")
        assert "no connected app" in (await http.get("/apps")).text
        await toggle(http, "always")
        assert provider.private_toggle("chatgpt") is None
        events = [e for e in provider.audit.recent() if e["kind"] == "private_access"]
        assert [e["by"] for e in events] == ["portal"] * 3


async def test_toggle_needs_reauth_and_csrf(
    make_settings: Callable[..., Settings], oauth: tuple[str, OAuthProvider]
) -> None:
    url, provider = oauth
    async with portal(make_settings(reauth_window_s=0), url) as http:
        page = await http.get("/apps")
        assert "confirm your password" in page.text and 'action="/apps/private"' not in page.text
        r = await http.post(
            "/apps/private",
            data={"csrf": csrf_of(page.text), "label": "chatgpt", "access": "on"},
            headers={"Origin": ORIGIN},
        )
        assert r.status_code == 303 and r.headers["location"].startswith("/reauth")
        assert provider.private_toggle("chatgpt") is None
    async with portal(make_settings(), url) as http:
        data = {"csrf": "forged", "label": "chatgpt", "access": "on"}
        r = await http.post("/apps/private", data=data, headers={"Origin": ORIGIN})
        assert r.status_code == 403
        page = await http.get("/apps")
        data["csrf"] = csrf_of(page.text)
        r = await http.post("/apps/private", data=data, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        assert provider.private_toggle("chatgpt") is None


async def test_apps_needs_the_owner(
    make_settings: Callable[..., Settings], oauth: tuple[str, OAuthProvider]
) -> None:
    url, _ = oauth
    app = build_app(make_settings(), wiki=WikiClient(url, PORTAL_TOKEN))
    async with serve_app(app) as base, httpx2.AsyncClient(base_url=base) as http:
        r = await http.get("/apps")
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
