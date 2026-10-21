from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from portal.config import Settings
from portal.security import host_allowed
from portal.web import safe_next


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/wiki/x", "/wiki/x"),
        ("/keys?x=1", "/keys?x=1"),
        (None, "/"),
        ("", "/"),
        ("https://evil.example", "/"),
        ("//evil.example", "/"),
        ("/\\evil.example", "/"),
        ("/ok\r\nSet-Cookie: x", "/"),
        ("javascript:alert(1)", "/"),
    ],
)
def test_safe_next(target: str | None, expected: str) -> None:
    assert safe_next(target) == expected


def test_host_allowed() -> None:
    allowed = {"hub.example.com"}
    assert host_allowed("hub.example.com", allowed)
    assert host_allowed("HUB.example.com", allowed)
    assert host_allowed("127.0.0.1:3090", allowed)
    assert host_allowed("localhost:3090", allowed)
    assert not host_allowed("mcp.example.com", allowed)
    assert not host_allowed("hub.example.com.evil.test", allowed)
    assert not host_allowed("", allowed)


def test_portal_url_per_mode(make_settings: Callable[..., Settings]) -> None:
    s = make_settings(
        PORTAL_PUBLIC_URL="", ui_access_mode="tailscale", ts_hostname="Box.Tail1.ts.net."
    )
    assert s.portal_url == "https://box.tail1.ts.net:9443"
    assert s.ui_url == "https://box.tail1.ts.net:8443"
    assert s.secure_cookies
    s = make_settings(
        PORTAL_PUBLIC_URL="",
        ui_access_mode="cloudflare",
        ui_hostname="chat.example.com",
        portal_hostname="hub.example.com",
        mcp_exposure_mode="cloudflare",
        mcp_hostname="mcp.example.com",
    )
    assert s.portal_url == "https://hub.example.com"
    assert s.mcp_base_url == "https://mcp.example.com"


def test_config_refuses_unsafe_setups(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ValueError, match="PORTAL_HOSTNAME"):  # hostname mode, no portal host
        make_settings(
            PORTAL_PUBLIC_URL="", ui_access_mode="cloudflare", ui_hostname="c.example.com"
        )
    with pytest.raises(ValueError, match="OWNER_TOTP_SECRET"):  # public portal without TOTP
        make_settings(
            PORTAL_PUBLIC_URL="", ui_access_mode="caddy", portal_hostname="hub.example.com"
        )
    with pytest.raises(ValueError, match="Funnel"):
        make_settings(PORTAL_PUBLIC_URL="", ts_hostname="b.t.ts.net", ts_portal_https_port=8443)


def test_credentials_come_from_the_brains_secrets(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    """Without OWNER_PASSWORD_HASH / WIKI_API_TOKEN the portal reads what the brain's setup
    wizard and first start wrote, at each use (the password may be set after the portal starts)."""
    s = make_settings(owner_password_hash="", wiki_api_token="", secrets_dir=tmp_path)
    assert s.password_hash() == "" and s.api_token() == ""
    (tmp_path / "admin_password_hash").write_text("$argon2id$hash\n")
    (tmp_path / "portal_token").write_text("t" * 40 + "\n")
    assert s.password_hash() == "$argon2id$hash" and s.api_token() == "t" * 40
