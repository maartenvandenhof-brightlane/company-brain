"""End-to-end OAuth 2.1 flow against the public listener:
discovery → registration (DCR and CIMD) → authorize → login/consent → token →
MCP tool call → refresh (rotation) → revoke. Also: internal token rejected here."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import pyotp
import pytest

from tests.conftest import SEED_DIR, TYPES_FILE
from tests.integration.helpers import mcp_client, structured
from wiki_mcp.auth.login import REVEAL_HASH
from wiki_mcp.auth.passwords import hash_password
from wiki_mcp.config import Settings
from wiki_mcp.public import build_public_app
from wiki_mcp.runtime import Runtime, create_runtime

PASSWORD = "correct horse battery staple"
TOTP_SECRET = pyotp.random_base32()
INTERNAL = "i" * 40
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
PW_HASH = hash_password(PASSWORD)


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


@pytest.fixture
async def env(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[tuple[str, Runtime]]:
    totp = getattr(request, "param", {}).get("totp", False)
    # The base URL must be known before the port is, so bind a fixed-by-OS port first.
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    base = f"http://127.0.0.1:{port}"
    settings = Settings(
        wiki_dir=tmp_path / "wiki",
        state_dir=tmp_path / "state",
        types_file=TYPES_FILE,
        seed_dir=SEED_DIR,
        PUBLIC_BASE_URL=base,
        owner_username="owner",
        owner_password_hash=PW_HASH,
        owner_totp_secret=TOTP_SECRET if totp else "",
        internal_bearer_token=INTERNAL,
        login_max_failures=3,
        login_lockout_base_s=30,
    )
    rt = await create_runtime(settings, run_background=False)
    app = build_public_app(rt, base)
    rt.provider = app.state.provider  # type: ignore[attr-defined]
    import uvicorn

    from tests.integration.helpers import _null

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    server.capture_signals = lambda: _null()  # type: ignore[method-assign]
    import asyncio

    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    try:
        yield base, rt
    finally:
        server.should_exit = True
        await task
        await rt.close()


async def register(http: httpx2.AsyncClient, base: str, **extra: Any) -> dict[str, Any]:
    r = await http.post(
        f"{base}/register",
        json={
            "client_name": "Claude",
            "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            **extra,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def authorize_and_login(
    http: httpx2.AsyncClient,
    base: str,
    client_id: str,
    *,
    password: str = PASSWORD,
    totp: str = "",
    scopes: tuple[str, ...] = ("wiki:write",),
    resource: str | None = None,
    requested: str = "wiki:read wiki:write",
    pages: list[str] | None = None,
) -> tuple[httpx2.Response, str]:
    verifier, challenge = pkce()
    r = await http.get(
        f"{base}/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st4te",
            "scope": requested,
            "resource": resource or f"{base}/mcp",
        },
    )
    assert r.status_code == 302, r.text
    login_url = r.headers["location"]
    assert login_url.startswith(f"{base}/login?request=")
    page = await http.get(login_url)
    assert page.status_code == 200
    if pages is not None:
        pages.append(page.text)
    assert "Claude" in page.text or "claude" in page.text
    csp = page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    # "show password" eye: one inline script, allowed by its exact hash only
    assert 'data-reveal="password"' in page.text and " hidden>" in page.text
    script_src = next(d.strip() for d in csp.split(";") if d.strip().startswith("script-src"))
    assert script_src == f"script-src '{REVEAL_HASH}'"  # no 'unsafe-inline' for scripts
    script = page.text.split("<script>", 1)[1].split("</script>", 1)[0]
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"sha256-{digest}" == REVEAL_HASH
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
    rid = parse_qs(urlsplit(login_url).query)["request"][0]
    data: list[tuple[str, str]] = [
        ("request", rid),
        ("csrf", csrf),
        ("username", "owner"),
        ("password", password),
        ("totp", totp),
        ("action", "approve"),
    ]
    data += [("scope", s) for s in scopes]
    r = await http.post(
        f"{base}/login",
        content=urlencode(data),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return r, verifier


async def full_login(
    http: httpx2.AsyncClient, base: str, client_id: str, **kw: Any
) -> dict[str, Any]:
    r, verifier = await authorize_and_login(http, base, client_id, **kw)
    assert r.status_code == 302, r.text
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["state"] == ["st4te"] and q["iss"] == [base]
    t = await http.post(
        f"{base}/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": f"{base}/mcp",
        },
    )
    assert t.status_code == 200, t.text
    body = t.json()
    body["_code"] = q["code"][0]
    body["_verifier"] = verifier
    return body  # type: ignore[no-any-return]


async def test_discovery_documents(env: tuple[str, Runtime]) -> None:
    base, _ = env
    async with httpx2.AsyncClient() as http:
        r = await http.post(f"{base}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 401
        challenge = r.headers["www-authenticate"]
        assert f'resource_metadata="{base}/.well-known/oauth-protected-resource/mcp"' in challenge
        prm = (await http.get(f"{base}/.well-known/oauth-protected-resource/mcp")).json()
        assert prm["resource"] == f"{base}/mcp" and prm["authorization_servers"] == [base]
        assert prm["scopes_supported"] == ["wiki:read", "wiki:write", "wiki:admin", "wiki:private"]
        asm = (await http.get(f"{base}/.well-known/oauth-authorization-server")).json()
        assert asm["issuer"] == base
        assert asm["code_challenge_methods_supported"] == ["S256"]
        assert "none" in asm["token_endpoint_auth_methods_supported"]
        assert asm["client_id_metadata_document_supported"] is True
        assert asm["authorization_response_iss_parameter_supported"] is True
        for key in (
            "authorization_endpoint",
            "token_endpoint",
            "registration_endpoint",
            "revocation_endpoint",
        ):
            assert asm[key].startswith(base)
        assert (await http.get(f"{base}/health")).json() == {"status": "ok"}
        # browser-based clients (MCP Inspector) need CORS on discovery
        pre = await http.options(
            f"{base}/.well-known/oauth-authorization-server",
            headers={"Origin": "http://localhost:6274", "Access-Control-Request-Method": "GET"},
        )
        assert pre.headers.get("access-control-allow-origin") in ("*", "http://localhost:6274")
        r = await http.get(
            f"{base}/.well-known/oauth-protected-resource/mcp",
            headers={"Origin": "http://localhost:6274"},
        )
        assert r.headers.get("access-control-allow-origin")


async def test_full_flow_dcr(env: tuple[str, Runtime]) -> None:
    base, rt = env
    async with httpx2.AsyncClient() as http:
        client = await register(http, base)
        tokens = await full_login(http, base, client["client_id"])
        assert tokens["scope"] == "wiki:read wiki:write"

        # MCP tool call with the access token; commits are attributed to claude.ai
        async with mcp_client(f"{base}/mcp", tokens["access_token"]) as mcp:
            res = structured(
                await mcp.call_tool(
                    "write_concept",
                    {
                        "path": "wiki/concepts/oauth",
                        "frontmatter": {
                            "type": "concept",
                            "title": "OAuth",
                            "description": "Auth protocol.",
                        },
                        "body": "Delegated authorization.",
                        "reason": "Testing the OAuth flow",
                    },
                )
            )
            assert res["status"] == "committed"
            denied = await mcp.call_tool(
                "accept_proposal", {"proposal_id": "proposals/x", "reason": "no"}
            )
            assert denied.is_error and "wiki:admin" in denied.content[0].text
        assert rt.store.recent_changes(limit=1)[0].author == "claude.ai"

        # authorization code is single-use; reuse revokes the grant
        replay = await http.post(
            f"{base}/token",
            data={
                "grant_type": "authorization_code",
                "code": tokens["_code"],
                "redirect_uri": REDIRECT,
                "client_id": client["client_id"],
                "code_verifier": tokens["_verifier"],
            },
        )
        assert replay.status_code == 400
        r = await http.post(
            f"{base}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert r.status_code == 401


async def test_refresh_rotation_and_revoke(env: tuple[str, Runtime]) -> None:
    base, _ = env
    async with httpx2.AsyncClient() as http:
        client = await register(http, base)
        tokens = await full_login(http, base, client["client_id"])
        refresh = {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client["client_id"],
        }
        r1 = await http.post(f"{base}/token", data=refresh)
        assert r1.status_code == 200, r1.text
        new = r1.json()
        assert new["refresh_token"] != tokens["refresh_token"]
        # old access token is retired by rotation; new one works
        headers = {
            "Authorization": f"Bearer {new['access_token']}",
            "Accept": "application/json, text/event-stream",
        }
        async with mcp_client(f"{base}/mcp", new["access_token"]) as mcp:
            assert structured(await mcp.call_tool("search", {"query": "brain"}))["results"]
        # replaying the old refresh token kills the whole grant
        assert (await http.post(f"{base}/token", data=refresh)).status_code == 400
        r = await http.post(
            f"{base}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers
        )
        assert r.status_code == 401

        # explicit revocation (RFC 7009)
        tokens = await full_login(http, base, client["client_id"])
        rv = await http.post(
            f"{base}/revoke",
            data={"token": tokens["access_token"], "client_id": client["client_id"]},
        )
        assert rv.status_code == 200, rv.text
        r = await http.post(
            f"{base}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert r.status_code == 401


async def test_internal_token_rejected_on_public_listener(env: tuple[str, Runtime]) -> None:
    base, _ = env
    async with httpx2.AsyncClient() as http:
        r = await http.post(
            f"{base}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "Authorization": f"Bearer {INTERNAL}",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert r.status_code == 401


async def test_wrong_resource_and_bad_redirects(env: tuple[str, Runtime]) -> None:
    base, _ = env
    async with httpx2.AsyncClient() as http:
        client = await register(http, base)
        verifier, challenge = pkce()
        r = await http.get(
            f"{base}/authorize",
            params={
                "response_type": "code",
                "client_id": client["client_id"],
                "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "s",
                "resource": "https://evil.example/mcp",
            },
        )
        assert r.status_code == 302 and "error=invalid_request" in r.headers["location"]
        for bad in (["http://evil.example/cb"], ["javascript:alert(1)"], ["myapp://cb"]):
            reg = await http.post(
                f"{base}/register", json={"client_name": "x", "redirect_uris": bad}
            )
            assert reg.status_code == 400, bad
        # redirect_uri not registered for the client
        r = await http.get(
            f"{base}/authorize",
            params={
                "response_type": "code",
                "client_id": client["client_id"],
                "redirect_uri": "https://attacker.example/cb",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert r.status_code == 400


async def test_login_failures_lockout_and_csrf(env: tuple[str, Runtime]) -> None:
    base, rt = env
    async with httpx2.AsyncClient() as http:
        client = await register(http, base)
        for _ in range(3):
            r, _ = await authorize_and_login(
                http, base, client["client_id"], password="wrong password!"
            )
            assert r.status_code == 401 and "Wrong username" in r.text
        r, _ = await authorize_and_login(http, base, client["client_id"])
        assert r.status_code == 429 and "Too many failed attempts" in r.text
    events = [e for e in rt.audit.recent(200) if e["kind"] == "login"]
    assert {e["outcome"] for e in events} >= {"failed", "locked"}
    assert all("password" not in e or e["password"] == "[redacted]" for e in events)

    async with httpx2.AsyncClient() as http:  # fresh cookie jar: CSRF cookie missing
        client = await register(http, base)
        verifier, challenge = pkce()
        r = await http.get(
            f"{base}/authorize",
            params={
                "response_type": "code",
                "client_id": client["client_id"],
                "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        rid = parse_qs(urlsplit(r.headers["location"]).query)["request"][0]
        r = await http.post(
            f"{base}/login",
            data={
                "request": rid,
                "csrf": "forged",
                "username": "owner",
                "password": PASSWORD,
                "action": "approve",
            },
        )
        assert r.status_code == 403


@pytest.mark.parametrize("env", [{"totp": True}], indirect=True)
async def test_totp_required(env: tuple[str, Runtime]) -> None:
    base, _ = env
    async with httpx2.AsyncClient() as http:
        client = await register(http, base)
        r, _ = await authorize_and_login(http, base, client["client_id"], totp="000000")
        assert r.status_code == 401
        tokens = await full_login(
            http, base, client["client_id"], totp=pyotp.TOTP(TOTP_SECRET).now()
        )
        assert tokens["access_token"]


async def test_admin_scope_and_deny(env: tuple[str, Runtime]) -> None:
    base, _ = env
    async with httpx2.AsyncClient() as http:
        client = await register(http, base)
        tokens = await full_login(
            http, base, client["client_id"], scopes=("wiki:write", "wiki:admin")
        )
        assert set(tokens["scope"].split()) == {"wiki:read", "wiki:write", "wiki:admin"}
        # read-only grant: write tools refuse
        tokens = await full_login(http, base, client["client_id"], scopes=())
        assert tokens["scope"] == "wiki:read"
        async with mcp_client(f"{base}/mcp", tokens["access_token"]) as mcp:
            res = await mcp.call_tool("ingest_source", {"title": "x", "content": "y"})
            assert res.is_error and "wiki:write" in res.content[0].text


async def test_enter_approves_and_deny_ends_the_request(env: tuple[str, Runtime]) -> None:
    base, rt = env
    async with httpx2.AsyncClient() as http:
        client = await register(http, base)
        _, challenge = pkce()
        r = await http.get(
            f"{base}/authorize",
            params={
                "response_type": "code",
                "client_id": client["client_id"],
                "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "st4te",
            },
        )
        page = await http.get(r.headers["location"])
        # Enter in a field (or a password manager) submits with the first submit button.
        first = re.search(r'<button[^>]*type="submit"[^>]*>', page.text).group(0)  # type: ignore[union-attr]
        assert 'value="approve"' in first
        assert re.search(r'<button[^>]*value="deny"[^>]*formnovalidate', page.text)

        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
        rid = parse_qs(urlsplit(r.headers["location"]).query)["request"][0]
        deny = {"request": rid, "csrf": csrf, "action": "deny"}
        r = await http.post(f"{base}/login", data=deny)
        assert r.status_code == 302
        q = parse_qs(urlsplit(r.headers["location"]).query)
        assert q["error"] == ["access_denied"] and q["state"] == ["st4te"]
        # The request is gone: a second submit (or the approve after it) sees an expired link.
        r = await http.post(f"{base}/login", data={**deny, "action": "approve"})
        assert r.status_code == 400 and "This sign-in link has expired" in r.text
    events = [e for e in rt.audit.recent(50) if e["kind"] == "consent"]
    assert [e["outcome"] for e in events] == ["denied"]
    assert any(
        e["kind"] == "login" and e["outcome"] == "expired" and e.get("method") == "POST"
        for e in rt.audit.recent(50)
    )


async def test_double_submit_replays_the_same_redirect(env: tuple[str, Runtime]) -> None:
    base, rt = env
    async with httpx2.AsyncClient() as http:
        client = await register(http, base)
        verifier, challenge = pkce()
        r = await http.get(
            f"{base}/authorize",
            params={
                "response_type": "code",
                "client_id": client["client_id"],
                "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "st4te",
                "resource": f"{base}/mcp",
            },
        )
        login_url = r.headers["location"]
        page = await http.get(login_url)
        # The page's one script guards against a second submit; it is still the hashed one.
        script = page.text.split("<script>", 1)[1].split("</script>", 1)[0]
        assert "preventDefault" in script
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        assert f"sha256-{digest}" == REVEAL_HASH
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
        rid = parse_qs(urlsplit(login_url).query)["request"][0]
        form = {
            "request": rid,
            "csrf": csrf,
            "username": "owner",
            "password": PASSWORD,
            "action": "approve",
        }
        first = await http.post(f"{base}/login", data=form)
        assert first.status_code == 302
        # The first response deleted the CSRF cookie; the resubmit gets the same redirect.
        assert "sb_login_csrf" not in http.cookies
        second = await http.post(f"{base}/login", data=form)
        assert second.status_code == 302
        assert second.headers["location"] == first.headers["location"]
        # A wrong csrf does not replay it.
        forged = await http.post(f"{base}/login", data={**form, "csrf": "forged"})
        assert forged.status_code == 400 and "This sign-in link has expired" in forged.text
        # Reopening the link says the connection is done.
        again = await http.get(login_url)
        assert again.status_code == 400 and "already approved" in again.text

        code = parse_qs(urlsplit(first.headers["location"]).query)["code"][0]
        token = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client["client_id"],
            "code_verifier": verifier,
            "resource": f"{base}/mcp",
        }
        t = await http.post(f"{base}/token", data=token)
        assert t.status_code == 200, t.text
        assert (await http.post(f"{base}/token", data=token)).status_code == 400
    events = [e for e in rt.audit.recent(50) if e["kind"] == "consent"]
    assert sorted(e["outcome"] for e in events) == ["approved", "replayed"]
    assert {e["client"] for e in events} == {"claude.ai"}


async def test_private_scope_is_opt_in(env: tuple[str, Runtime]) -> None:
    base, rt = env
    async with httpx2.AsyncClient() as http:
        client = await register(http, base)
        # Registered before wiki:private existed: it may still ask for it.
        provider = rt.provider  # type: ignore[attr-defined]
        stored = provider._exec(
            "SELECT info FROM clients WHERE client_id = ?", (client["client_id"],)
        ).fetchone()[0]
        old = {**json.loads(stored), "scope": "wiki:read wiki:write wiki:admin"}
        provider._exec(
            "UPDATE clients SET info = ? WHERE client_id = ?",
            (json.dumps(old), client["client_id"]),
        )
        pages: list[str] = []
        plain = await full_login(
            http,
            base,
            client["client_id"],
            requested="wiki:read wiki:write wiki:admin wiki:private",
            scopes=("wiki:write",),
            pages=pages,
        )
        # Offered, but never pre-checked, even when the client asks for it.
        box = re.search(r'<input type="checkbox" name="scope" value="wiki:private"[^>]*>', pages[0])
        assert box and "checked" not in box.group(0) and "disabled" not in box.group(0)
        assert re.search(r'value="wiki:admin" checked', pages[0])
        assert "Private pages: personal life, relationships, feelings." in pages[0]
        assert "wiki:private" not in plain["scope"].split()
        trusted = await full_login(
            http, base, client["client_id"], scopes=("wiki:write", "wiki:private")
        )
        assert set(trusted["scope"].split()) == {"wiki:read", "wiki:write", "wiki:private"}

        args = {
            "path": "wiki/people/partner",
            "frontmatter": {
                "type": "person",
                "title": "Partner",
                "description": "The owner's partner.",
                "private": True,
            },
            "body": "Met in 2020.",
            "reason": "File a personal conversation",
        }
        async with mcp_client(f"{base}/mcp", plain["access_token"]) as mcp:
            assert structured(await mcp.call_tool("write_concept", args))["created"] is True
            res = await mcp.call_tool("read_concept", {"path": "wiki/people/partner"})
            assert res.is_error and "no page at" in res.content[0].text
        async with mcp_client(f"{base}/mcp", trusted["access_token"]) as mcp:
            page = structured(await mcp.call_tool("read_concept", {"path": "wiki/people/partner"}))
            assert page["frontmatter"]["private"] is True


async def test_private_access_toggle_without_reconnecting(env: tuple[str, Runtime]) -> None:
    base, rt = env
    provider = rt.provider  # type: ignore[attr-defined]
    partner = "wiki/people/partner"
    async with httpx2.AsyncClient() as http:
        plain_client = await register(http, base)
        plain = await full_login(http, base, plain_client["client_id"])
        # the same app (label claude.ai) registered again, consented with private pages
        trusted_client = await register(http, base)
        trusted = await full_login(
            http, base, trusted_client["client_id"], scopes=("wiki:write", "wiki:private")
        )
        args = {
            "path": partner,
            "frontmatter": {
                "type": "person",
                "title": "Partner",
                "description": "The owner's partner.",
                "private": True,
            },
            "body": "Met in 2020.",
            "reason": "File a personal conversation",
        }

        async def reads(token: str) -> bool:
            async with mcp_client(f"{base}/mcp", token) as mcp:
                res = await mcp.call_tool("read_concept", {"path": partner})
                return not res.is_error

        async with mcp_client(f"{base}/mcp", plain["access_token"]) as mcp:
            assert structured(await mcp.call_tool("write_concept", args))["created"] is True
        assert not await reads(plain["access_token"])
        assert await reads(trusted["access_token"])

        # on: the existing token reads private pages, no reconnect
        provider.set_private_access("claude.ai", "on", by="test")
        assert await reads(plain["access_token"])
        # the consent page says the toggle overrides the private-pages box
        pages: list[str] = []
        await authorize_and_login(http, base, plain_client["client_id"], pages=pages)
        assert "Private pages are turned on for claude.ai</strong>" in pages[0]
        # off: withdrawn from every token of the app, also the one consented with it
        provider.set_private_access("claude.ai", "off", by="test")
        assert not await reads(plain["access_token"])
        assert not await reads(trusted["access_token"])
        # a refresh keeps the consented scopes; the toggle still applies to the new token
        r = await http.post(
            f"{base}/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": trusted["refresh_token"],
                "client_id": trusted_client["client_id"],
            },
        )
        assert r.status_code == 200, r.text
        refreshed = r.json()
        assert "wiki:private" in refreshed["scope"].split()
        assert not await reads(refreshed["access_token"])
        # default: back to what each token was consented with
        provider.set_private_access("claude.ai", "default", by="test")
        assert await reads(refreshed["access_token"])
        assert not await reads(plain["access_token"])


async def test_cimd_client(env: tuple[str, Runtime], monkeypatch: pytest.MonkeyPatch) -> None:
    base, rt = env
    cimd_id = "https://claude.ai/oauth/mcp-client-metadata"
    fetched: list[str] = []

    async def fake_fetch(url: str) -> dict[str, Any]:
        fetched.append(url)
        return {
            "client_id": cimd_id,
            "client_name": "Claude",
            "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "none",
        }

    provider = rt.provider  # type: ignore[attr-defined]
    monkeypatch.setattr(provider, "cimd_fetch", fake_fetch)
    monkeypatch.setattr(provider, "cimd_allowed_hosts", ["claude.ai"])
    async with httpx2.AsyncClient() as http:
        tokens = await full_login(http, base, cimd_id)
        assert fetched == [cimd_id]
        async with mcp_client(f"{base}/mcp", tokens["access_token"]) as mcp:
            await mcp.call_tool(
                "ingest_source", {"title": "CIMD test", "content": "hello", "trust": "agent"}
            )
    assert rt.store.recent_changes(limit=1)[0].author == "claude.ai"
    # a client_id on a host outside the allowlist is refused
    async with httpx2.AsyncClient() as http:
        verifier, challenge = pkce()
        r = await http.get(
            f"{base}/authorize",
            params={
                "response_type": "code",
                "client_id": "https://evil.example/client.json",
                "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert r.status_code == 400
