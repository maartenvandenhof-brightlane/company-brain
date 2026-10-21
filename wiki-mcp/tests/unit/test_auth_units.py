from __future__ import annotations

from pathlib import Path

import pyotp
import pytest
from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import OAuthClientInformationFull

from wiki_mcp.__main__ import main
from wiki_mcp.audit import AuditLog
from wiki_mcp.auth.admin import run_clients_command
from wiki_mcp.auth.cimd import CIMDError, fetch_client_metadata
from wiki_mcp.auth.passwords import hash_password, verify_owner, verify_password
from wiki_mcp.auth.provider import OAuthProvider, client_label, host_allowed
from wiki_mcp.auth.ratelimit import Lockout, RateLimiter
from wiki_mcp.config import Settings


def test_rate_limiter() -> None:
    rl = RateLimiter(3, period=60)
    assert [rl.check("a", now=0) for _ in range(3)] == [0, 0, 0]
    assert rl.check("a", now=0) > 0
    assert rl.check("b", now=0) == 0
    assert rl.check("a", now=20) == 0  # refilled one token after 20s


def test_lockout_backoff() -> None:
    lo = Lockout(max_failures=2, base_seconds=10)
    lo.fail("owner", now=0)
    assert lo.blocked_for("owner", now=0) == 0
    lo.fail("owner", now=0)
    assert lo.blocked_for("owner", now=0) == 10
    lo.fail("owner", now=0)
    assert lo.blocked_for("owner", now=0) == 20
    lo.succeed("owner")
    assert lo.blocked_for("owner", now=0) == 0


def test_passwords_and_totp() -> None:
    h = hash_password("a long passphrase")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "a long passphrase")
    assert not verify_password(h, "wrong")
    assert not verify_password("", "anything")
    assert not verify_password("not-a-hash", "x")
    secret = pyotp.random_base32()
    base = {"expected_username": "owner", "password_hash": h, "totp_secret": secret}
    ok = pyotp.TOTP(secret).now()
    assert verify_owner(username="owner", password="a long passphrase", totp_code=ok, **base)
    assert not verify_owner(username="owner", password="a long passphrase", totp_code="123", **base)
    assert not verify_owner(username="admin", password="a long passphrase", totp_code=ok, **base)


REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _info(name: str, redirect: str, cid: str = "abc") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(client_id=cid, client_name=name, redirect_uris=[redirect])  # type: ignore[list-item]


@pytest.mark.parametrize(
    ("name", "redirect", "kind", "cid", "label"),
    [
        ("Claude", "https://claude.ai/api/mcp/auth_callback", "dcr", "x", "claude.ai"),
        ("ChatGPT", "https://chatgpt.com/connector/oauth/abc", "dcr", "x", "chatgpt"),
        ("Le Chat", "https://chat.mistral.ai/cb", "dcr", "x", "lechat"),
        ("Claude", "https://evil.example/cb", "dcr", "x", "claude@evil.example"),
        (
            "MCP Inspector",
            "http://localhost:6274/oauth/callback",
            "dcr",
            "x",
            "mcp-inspector-local",
        ),
        ("x", "https://claude.ai/cb", "cimd", "https://claude.ai/meta.json", "claude.ai"),
        ("x", "https://chatgpt.com/cb", "cimd", "https://openai.com/meta.json", "chatgpt"),
    ],
)
def test_client_label(name: str, redirect: str, kind: str, cid: str, label: str) -> None:
    assert client_label(_info(name, redirect, cid), kind) == label


@pytest.mark.parametrize(
    ("name", "redirects", "label"),
    [
        # a known app's label (and so its private-access toggle) needs all hosts to be its own
        (
            "Claude",
            ["https://claude.ai/cb", "https://evil.example/cb"],
            "claude@claude.ai+evil.example",
        ),
        (
            "ChatGPT",
            ["https://chatgpt.com/cb", "https://evil.example/cb"],
            "chatgpt@chatgpt.com+evil.example",
        ),
        (
            "Claude Code",
            ["http://localhost:1/cb", "https://evil.example/cb"],
            "claude-code@evil.example+localhost",
        ),
        ("Tool", ["https://a.example/cb", "https://z.evil/cb"], "tool@a.example+z.evil"),
        ("Claude", ["https://claude.ai/cb", "https://claude.com/cb"], "claude.ai"),
        ("Claude Code", ["http://localhost:1/cb", "http://127.0.0.1:1/cb"], "claude-code-local"),
    ],
)
def test_client_label_needs_every_redirect_host(
    name: str, redirects: list[str], label: str
) -> None:
    info = OAuthClientInformationFull(client_id="x", client_name=name, redirect_uris=redirects)  # type: ignore[arg-type]
    assert client_label(info, "dcr") == label


def test_host_allowed() -> None:
    assert host_allowed("claude.ai", ["claude.ai"])
    assert host_allowed("api.claude.ai", ["claude.ai"])
    assert not host_allowed("evilclaude.ai", ["claude.ai"])
    assert not host_allowed("claude.ai.evil.com", ["claude.ai"])


def test_redirect_validation(tmp_path: Path) -> None:
    p = OAuthProvider(
        tmp_path / "o.sqlite",
        issuer="https://mcp.x",
        resource="https://mcp.x/mcp",
        audit=AuditLog(None),
        owner_username="owner",
        allowed_redirect_hosts=["claude.ai"],
    )
    p.validate_redirect_uris(["https://claude.ai/cb", "http://127.0.0.1:3000/cb"])
    for bad in (
        ["https://chatgpt.com/cb"],
        ["http://claude.ai/cb"],
        ["https://claude.ai/cb#x"],
        [],
    ):
        with pytest.raises(RegistrationError):
            p.validate_redirect_uris(bad)
    assert p.canonical_resource(None) == "https://mcp.x/mcp"
    assert p.canonical_resource("https://MCP.x/mcp/") == "https://mcp.x/mcp"
    assert p.canonical_resource("https://mcp.x") == "https://mcp.x/mcp"
    p.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://claude.ai/meta.json",
        "https://claude.ai:8443/meta.json",
        "https://user:pw@claude.ai/meta.json",
        "https://localhost/meta.json",
        "https://127.0.0.1/meta.json",
    ],
)
async def test_cimd_guards(url: str) -> None:
    with pytest.raises((CIMDError, OSError)):
        await fetch_client_metadata(url)


# ------------------------------------------------------------------ private access per app

CHATGPT = "https://chatgpt.com/connector/oauth/cb"


def _provider(path: Path, audit: AuditLog | None = None) -> OAuthProvider:
    return OAuthProvider(
        path,
        issuer="https://mcp.x",
        resource="https://mcp.x/mcp",
        audit=audit or AuditLog(None),
        owner_username="owner",
    )


async def _connect(p: OAuthProvider, cid: str, name: str, redirect: str, scopes: str) -> str:
    """A registered client with one grant; answers its access token."""
    await p.register_client(_info(name, redirect, cid))
    return p._issue(cid, scopes.split(), "https://mcp.x/mcp", cid + "-family").access_token


async def test_private_toggle_by_label(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.sqlite")
    p = _provider(tmp_path / "o.sqlite", audit)
    # One app, two client_ids (e.g. re-registered): the toggle is per label.
    plain = await _connect(p, "gpt-1", "ChatGPT", CHATGPT, "wiki:read wiki:write")
    consented = await _connect(p, "gpt-2", "ChatGPT", CHATGPT, "wiki:read wiki:private")
    other = await _connect(p, "claude-1", "Claude", REDIRECT, "wiki:read wiki:private")

    async def scopes(token: str) -> set[str]:
        loaded = await p.load_access_token(token)
        assert loaded is not None
        return set(loaded.scopes)

    assert "wiki:private" not in await scopes(plain)
    assert "wiki:private" in await scopes(consented)
    [app] = [a for a in p.apps() if a["label"] == "chatgpt"]
    assert app["kind"] == "dcr" and app["active_grants"] == 2
    assert app["consented"] and app["toggle"] == "default" and app["private"]

    p.set_private_access("chatgpt", "on", by="cli")
    assert await scopes(plain) == {"wiki:read", "wiki:write", "wiki:private"}
    p.set_private_access("chatgpt", "off", by="portal", ip="10.0.0.2")
    assert "wiki:private" not in await scopes(plain) | await scopes(consented)
    assert "wiki:private" in await scopes(other)  # another app keeps its consent
    [app] = [a for a in p.apps() if a["label"] == "chatgpt"]
    assert app["toggle"] == "off" and app["consented"] and not app["private"]
    # Stored scopes stay as consented: `default` restores them.
    stored = p._exec("SELECT scopes FROM tokens WHERE client_id = 'gpt-2'").fetchone()[0]
    assert "wiki:private" in stored
    p.set_private_access("chatgpt", "default", by="cli")
    assert "wiki:private" not in await scopes(plain)
    assert "wiki:private" in await scopes(consented)
    assert p.private_toggle("chatgpt") is None

    changes = [e for e in audit.recent() if e["kind"] == "private_access"]
    assert [(e["access"], e["previous"], e["by"]) for e in reversed(changes)] == [
        ("on", "default", "cli"),
        ("off", "on", "portal"),
        ("default", "off", "cli"),
    ]
    assert changes[1]["client"] == "chatgpt" and changes[1]["ip"] == "10.0.0.2"
    p.close()
    audit.close()


async def test_private_toggle_accepts_known_labels_only(tmp_path: Path) -> None:
    p = _provider(tmp_path / "o.sqlite")
    await _connect(p, "gpt-1", "ChatGPT", CHATGPT, "wiki:read")
    for label, access in (("nosuch", "on"), ("chatgpt", "yes"), ("nosuch", "default")):
        with pytest.raises(ValueError):
            p.set_private_access(label, access, by="cli")
    # A toggle outlives the app's clients (it applies again on reconnect) and can be reset.
    p.set_private_access("chatgpt", "on", by="cli")
    p.revoke_client("gpt-1")
    assert p.private_toggle("chatgpt") == "on"
    with pytest.raises(ValueError):
        p.set_private_access("chatgpt", "off", by="cli")
    p.set_private_access("chatgpt", "default", by="cli")
    assert p.private_toggle("chatgpt") is None
    p.close()


async def test_clients_cli_private(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(state_dir=tmp_path)
    p = _provider(tmp_path / "oauth.sqlite")
    await _connect(p, "gpt-1", "ChatGPT", CHATGPT, "wiki:read wiki:write")
    p.close()

    assert run_clients_command(settings, "list", None) == 0
    assert "private=no (consent)" in capsys.readouterr().out
    assert run_clients_command(settings, "private", None, "chatgpt", "on") == 0
    assert "private=yes (toggle)" in capsys.readouterr().out
    assert run_clients_command(settings, "list", None) == 0
    out = capsys.readouterr().out
    assert "chatgpt" in out and "private=yes (toggle)" in out
    assert run_clients_command(settings, "private", None, "nosuch", "on") == 2
    assert "no connected app is called 'nosuch'" in capsys.readouterr().out
    assert run_clients_command(settings, "private", None, "chatgpt", "default") == 0
    assert "private=no (consent)" in capsys.readouterr().out
    # wired into `wiki-mcp clients private <label> on|off|default`
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    assert main(["clients", "private", "chatgpt", "off"]) == 0
    assert "private=no (toggle)" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["clients", "private", "chatgpt", "maybe"])
    audit = AuditLog(tmp_path / "audit.sqlite")
    changes = [e["access"] for e in audit.recent() if e["kind"] == "private_access"]
    assert changes == ["off", "default", "on"]
    audit.close()
