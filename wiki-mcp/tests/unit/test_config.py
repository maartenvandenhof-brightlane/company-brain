import pytest

from wiki_mcp.config import ExposureMode, ModeError, Settings, Trust, resolve_public_base_url

MCP_HOST = "mcp.brightlane.be"
TS_HOST = "box.tail1234.ts.net"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (ExposureMode.CLOUDFLARE, "https://mcp.brightlane.be"),
        (ExposureMode.CADDY, "https://mcp.brightlane.be"),
        (ExposureMode.TAILSCALE, "https://box.tail1234.ts.net"),
        (ExposureMode.NONE, None),
    ],
)
def test_public_base_url_per_mode(mode: ExposureMode, expected: str | None) -> None:
    assert resolve_public_base_url(mode, mcp_hostname=MCP_HOST, ts_hostname=TS_HOST) == expected


@pytest.mark.parametrize("ui_mode", ["tailscale", "cloudflare", "caddy"])
@pytest.mark.parametrize("mcp_mode", ["tailscale", "cloudflare", "caddy", "none"])
def test_ui_mode_never_changes_mcp_url(
    monkeypatch: pytest.MonkeyPatch, ui_mode: str, mcp_mode: str
) -> None:
    monkeypatch.setenv("UI_ACCESS_MODE", ui_mode)
    s = Settings(mcp_exposure_mode=mcp_mode, mcp_hostname=MCP_HOST, ts_hostname=TS_HOST)
    expected = resolve_public_base_url(
        ExposureMode(mcp_mode), mcp_hostname=MCP_HOST, ts_hostname=TS_HOST
    )
    assert s.public_base_url == expected
    assert s.resource_url == (f"{expected}/mcp" if expected else None)


@pytest.mark.parametrize(
    ("mode", "kwargs"),
    [
        (ExposureMode.CLOUDFLARE, {}),
        (ExposureMode.CADDY, {"mcp_hostname": "https://mcp.x.be"}),
        (ExposureMode.CADDY, {"mcp_hostname": "mcp.x.be:8443"}),
        (ExposureMode.TAILSCALE, {"ts_hostname": "box.example.com"}),
    ],
)
def test_missing_or_bad_hostnames(mode: ExposureMode, kwargs: dict[str, str]) -> None:
    with pytest.raises(ModeError):
        resolve_public_base_url(mode, **kwargs)


def test_override_rules() -> None:
    assert (
        resolve_public_base_url(ExposureMode.NONE, override="http://127.0.0.1:8080")
        == "http://127.0.0.1:8080"
    )
    assert (
        resolve_public_base_url(ExposureMode.CADDY, override="https://X.example.com/")
        == "https://x.example.com"
    )
    for bad in ("http://example.com", "https://x.com/path", "ftp://x"):
        with pytest.raises(ModeError):
            resolve_public_base_url(ExposureMode.NONE, override=bad)


def test_hostname_normalization() -> None:
    assert (
        resolve_public_base_url(ExposureMode.TAILSCALE, ts_hostname="Box.Tail1.TS.NET.")
        == "https://box.tail1.ts.net"
    )


def test_settings_csv_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "172.30.0.10, 172.30.0.1/32")
    monkeypatch.setenv("CIMD_ALLOWED_HOSTS", "claude.ai,chatgpt.com")
    s = Settings()
    assert s.trusted_proxy_ips == ["172.30.0.10", "172.30.0.1/32"]
    assert s.cimd_allowed_hosts == ["claude.ai", "chatgpt.com"]
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "not-an-ip")
    with pytest.raises(ValueError):
        Settings()


def test_invalid_mode_config_fails_fast() -> None:
    with pytest.raises(ValueError):
        Settings(mcp_exposure_mode="cloudflare", mcp_hostname="")


def test_trust_order() -> None:
    assert Trust.lowest(Trust.OWNER, Trust.EXTERNAL, Trust.AGENT) is Trust.EXTERNAL
    assert Settings(proposals_for="none").proposal_threshold() is None
    assert Settings().proposal_threshold() is Trust.EXTERNAL
