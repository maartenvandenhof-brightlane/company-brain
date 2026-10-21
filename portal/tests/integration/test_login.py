"""Login, sessions, CSRF, origin checks, lockout, re-auth, logout and headers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import httpx2
import pyotp
import pytest
from wiki_mcp.audit import AuditLog

from portal.app import build_app
from portal.config import Settings
from tests.conftest import ORIGIN, PASSWORD, TOTP_SECRET, csrf_of, login, serve_app


@pytest.fixture
async def client(make_settings: Callable[..., Settings]) -> AsyncIterator[httpx2.AsyncClient]:
    app = build_app(make_settings(login_max_failures=3))
    async with serve_app(app) as url, httpx2.AsyncClient(base_url=url) as http:
        yield http


async def test_pages_require_login(client: httpx2.AsyncClient) -> None:
    r = await client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/wiki"  # the main screen
    r = await client.get("/wiki")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/wiki"
    r = await client.get("/docs")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/docs"
    for gone in ("/home", "/ideas", "/patterns", "/import", "/dreams", "/keys", "/dashboard"):
        assert (await client.get(gone)).status_code == 404  # not wired in the Company Brain
    r = await client.post("/logout", data={"csrf": "x"}, headers={"Origin": ORIGIN})
    assert r.status_code == 403


async def test_health_is_open(client: httpx2.AsyncClient) -> None:
    assert (await client.get("/health", headers={"Host": "anything.example"})).json() == {
        "status": "ok"
    }


async def test_security_headers(client: httpx2.AsyncClient) -> None:
    r = await client.get("/login")
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY"
    # Browsers send `Origin: null` on POSTs under no-referrer, which would fail every form.
    assert r.headers["referrer-policy"] == "same-origin"
    assert 'content="same-origin"' in r.text
    assert r.headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in r.headers  # http test origin
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


async def test_show_password_eye(client: httpx2.AsyncClient) -> None:
    page = (await client.get("/login")).text
    # hidden until portal.js (same-origin, allowed by the CSP) reveals it; never a dead button
    assert '<button type="button" class="reveal" data-reveal="password"' in page
    assert 'aria-label="Show password" aria-pressed="false" hidden>' in page
    assert "<script>" not in page  # no inline script: the toggle lives in /static/js/
    assert '<script type="module" src="/static/js/portal.js">' in page
    assert "./password.js" in (await client.get("/static/js/portal.js")).text
    js = (await client.get("/static/js/password.js")).text
    assert "data-reveal" in js and 'input.type = "password"' in js


async def test_unknown_host_rejected(client: httpx2.AsyncClient) -> None:
    r = await client.get("/login", headers={"Host": "mcp.example.com"})
    assert r.status_code == 421


async def test_login_and_logout(client: httpx2.AsyncClient) -> None:
    anon_cookie = (await client.get("/login")).cookies.get("sb_portal")
    r = await login(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.cookies.get("sb_portal") != anon_cookie  # session id rotated
    landing = await client.get("/")
    assert landing.status_code == 303 and landing.headers["location"] == "/wiki"
    page = await client.get("/docs")  # signed in (a page that needs no wiki)
    assert page.status_code == 200 and 'href="/apps"' in page.text
    assert 'href="/dashboard"' not in page.text and 'href="/ideas"' not in page.text
    r = await client.post("/logout", data={"csrf": csrf_of(page.text)}, headers={"Origin": ORIGIN})
    assert r.status_code == 303
    assert (await client.get("/docs")).headers["location"].startswith("/login")


async def test_login_rejects_bad_csrf_and_foreign_origin(client: httpx2.AsyncClient) -> None:
    page = await client.get("/login")
    good = {"csrf": csrf_of(page.text), "username": "owner", "password": PASSWORD}
    r = await client.post("/login", data={**good, "csrf": "forged"}, headers={"Origin": ORIGIN})
    assert r.status_code == 403
    page = await client.get("/login")
    good["csrf"] = csrf_of(page.text)
    r = await client.post("/login", data=good, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = await client.post("/login", data=good)  # no Origin, no Referer
    assert r.status_code == 403


async def test_post_requires_session_csrf(client: httpx2.AsyncClient) -> None:
    await login(client)
    r = await client.post("/logout", data={"csrf": "forged"}, headers={"Origin": ORIGIN})
    assert r.status_code == 403
    page = await client.get("/docs")  # any signed-in page carries the token; this one needs no wiki
    r = await client.post(
        "/logout", data={"csrf": csrf_of(page.text)}, headers={"Origin": "https://evil.example"}
    )
    assert r.status_code == 403
    r = await client.post(
        "/logout",
        content=b'{"csrf": "x"}',
        headers={"Origin": ORIGIN, "Content-Type": "application/json"},
    )
    assert r.status_code == 403


async def test_lockout_after_failures(client: httpx2.AsyncClient) -> None:
    for _ in range(3):
        page = await client.get("/login")
        r = await client.post(
            "/login",
            data={"csrf": csrf_of(page.text), "username": "owner", "password": "wrong"},
            headers={"Origin": ORIGIN},
        )
        assert r.status_code == 401
    r = await login(client)
    assert r.status_code == 429  # even the right password is refused while locked


async def test_open_redirect_blocked(client: httpx2.AsyncClient) -> None:
    page = await client.get("/login")
    r = await client.post(
        "/login",
        data={
            "csrf": csrf_of(page.text),
            "username": "owner",
            "password": PASSWORD,
            "next": "//evil.example/x",
        },
        headers={"Origin": ORIGIN},
    )
    assert r.headers["location"] == "/"


async def test_totp_required_when_configured(make_settings: Callable[..., Settings]) -> None:
    app = build_app(make_settings(owner_totp_secret=TOTP_SECRET))
    async with serve_app(app) as url, httpx2.AsyncClient(base_url=url) as http:
        assert (await login(http)).status_code == 401
        r = await login(http, totp=pyotp.TOTP(TOTP_SECRET).now())
        assert r.status_code == 303


async def test_reauth_flow(make_settings: Callable[..., Settings]) -> None:
    app = build_app(make_settings(reauth_window_s=0))
    async with serve_app(app) as url, httpx2.AsyncClient(base_url=url) as http:
        await login(http)
        page = await http.get("/reauth?next=/keys")
        assert page.status_code == 200
        r = await http.post(
            "/reauth",
            data={"csrf": csrf_of(page.text), "password": "wrong", "next": "/keys"},
            headers={"Origin": ORIGIN},
        )
        assert r.status_code == 401
        page = await http.get("/reauth?next=/keys")
        r = await http.post(
            "/reauth",
            data={"csrf": csrf_of(page.text), "password": PASSWORD, "next": "/keys"},
            headers={"Origin": ORIGIN},
        )
        assert r.status_code == 303 and r.headers["location"] == "/keys"


async def test_audit_never_holds_passwords(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings()
    app = build_app(settings)
    async with serve_app(app) as url, httpx2.AsyncClient(base_url=url) as http:
        page = await http.get("/login")
        await http.post(
            "/login",
            data={"csrf": csrf_of(page.text), "username": "owner", "password": "hunter2-wrong"},
            headers={"Origin": ORIGIN},
        )
        await login(http)
    raw = b"".join(p.read_bytes() for p in settings.state_dir.glob("audit.sqlite*"))
    assert b"hunter2-wrong" not in raw and PASSWORD.encode() not in raw
    events = AuditLog(settings.state_dir / "audit.sqlite").recent()
    assert [(e["kind"], e["outcome"]) for e in events][:2] == [("login", "ok"), ("login", "failed")]
