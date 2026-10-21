"""Runtime configuration (environment variables) and access-mode resolution.

The public URL of the MCP server is never derived from request headers. It is
computed here from MCP_EXPOSURE_MODE and the configured hostnames, so a spoofed
Host / X-Forwarded-* header can never influence OAuth issuer or resource URLs.
"""

from __future__ import annotations

import ipaddress
import re
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class ExposureMode(StrEnum):
    TAILSCALE = "tailscale"
    CLOUDFLARE = "cloudflare"
    CADDY = "caddy"
    NONE = "none"


class UIMode(StrEnum):
    TAILSCALE = "tailscale"
    CLOUDFLARE = "cloudflare"
    CADDY = "caddy"


class Trust(StrEnum):
    """Trust levels, ordered from least to most trusted."""

    EXTERNAL = "external"
    AGENT = "agent"
    OWNER = "owner"

    @property
    def rank(self) -> int:
        return _TRUST_RANK[self]

    @classmethod
    def lowest(cls, *levels: Trust) -> Trust:
        return min(levels, key=lambda t: t.rank)


_TRUST_RANK = {Trust.EXTERNAL: 0, Trust.AGENT: 1, Trust.OWNER: 2}


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


CsvList = Annotated[list[str], NoDecode]


class ModeError(ValueError):
    """Raised when the access-mode configuration is inconsistent."""


def resolve_public_base_url(
    mode: ExposureMode,
    *,
    mcp_hostname: str = "",
    ts_hostname: str = "",
    override: str = "",
) -> str | None:
    """Compute the public base URL of wiki-mcp for an exposure mode.

    Returns None when the MCP server is not publicly exposed. `override` exists
    for local development and tests only.
    """
    if override:
        parts = urlsplit(override)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ModeError(f"PUBLIC_BASE_URL must be an absolute http(s) URL, got {override!r}")
        if parts.scheme == "http" and not _is_loopback(parts.hostname):
            raise ModeError("PUBLIC_BASE_URL may only use http:// for loopback hosts")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ModeError("PUBLIC_BASE_URL must not contain a path, query or fragment")
        return f"{parts.scheme}://{parts.netloc}".lower()
    if mode is ExposureMode.NONE:
        return None
    if mode is ExposureMode.TAILSCALE:
        host = ts_hostname.strip().rstrip(".").lower()
        if not host.endswith(".ts.net"):
            raise ModeError(
                "MCP_EXPOSURE_MODE=tailscale requires TS_HOSTNAME=<machine>.<tailnet>.ts.net"
            )
        return f"https://{host}"
    host = mcp_hostname.strip().rstrip(".").lower()
    if not host or "." not in host or "/" in host or ":" in host:
        raise ModeError(
            f"MCP_EXPOSURE_MODE={mode.value} requires MCP_HOSTNAME (e.g. mcp.example.com)"
        )
    return f"https://{host}"


# Tailscale Funnel only works on these ports, so the portal never uses them: it can
# then never be made public by `tailscale funnel`, even by mistake.
FUNNEL_PORTS = frozenset({443, 8443, 10000})


def _norm_host(host: str) -> str:
    return host.strip().rstrip(".").lower()


def _valid_host(host: str) -> bool:
    label = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"
    return re.fullmatch(rf"{label}(\.{label})+", host) is not None


def resolve_ui_url(
    mode: UIMode, *, ui_hostname: str = "", ts_hostname: str = "", ts_ui_port: int = 8443
) -> str:
    """Public URL of LibreChat (mirrors DERIVED_UI_PUBLIC_URL in scripts/lib/modes.sh)."""
    if mode is UIMode.TAILSCALE:
        host = _norm_host(ts_hostname)
        return f"https://{host}" if ts_ui_port == 443 else f"https://{host}:{ts_ui_port}"
    return f"https://{_norm_host(ui_hostname)}"


def resolve_portal_url(
    mode: UIMode,
    *,
    portal_hostname: str = "",
    ts_hostname: str = "",
    ts_portal_port: int = 9443,
    ts_ui_port: int = 8443,
    ui_hostname: str = "",
    mcp_hostname: str = "",
) -> str | None:
    """Public URL of the portal. It follows UI_ACCESS_MODE, never the MCP exposure.

    Returns None when a hostname mode has no PORTAL_HOSTNAME (portal not started).
    Mirrors DERIVED_PORTAL_PUBLIC_URL in scripts/lib/modes.sh.
    """
    if mode is UIMode.TAILSCALE:
        host = _norm_host(ts_hostname)
        if not host.endswith(".ts.net"):
            raise ModeError("the portal in tailscale mode requires TS_HOSTNAME")
        if ts_portal_port in FUNNEL_PORTS or ts_portal_port == ts_ui_port:
            raise ModeError(
                "TS_PORTAL_HTTPS_PORT must not be 443, 8443, 10000 (Funnel ports) or the UI port"
            )
        if not 1 <= ts_portal_port <= 65535:
            raise ModeError("TS_PORTAL_HTTPS_PORT must be a port number")
        return f"https://{host}:{ts_portal_port}"
    host = _norm_host(portal_hostname)
    if not host:
        return None
    if not _valid_host(host):
        raise ModeError("PORTAL_HOSTNAME must be a hostname like hub.example.com")
    if host in {_norm_host(ui_hostname), _norm_host(mcp_hostname)}:
        raise ModeError("PORTAL_HOSTNAME must differ from UI_HOSTNAME and MCP_HOSTNAME")
    return f"https://{host}"


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", case_sensitive=False, populate_by_name=True
    )

    # --- access layer --------------------------------------------------------
    mcp_exposure_mode: ExposureMode = ExposureMode.NONE
    mcp_hostname: str = ""
    ts_hostname: str = ""
    public_base_url_override: str = Field(default="", alias="PUBLIC_BASE_URL")
    # Peers allowed to set X-Forwarded-For / CF-Connecting-IP (IPs or CIDRs).
    trusted_proxy_ips: CsvList = Field(default_factory=list)

    # --- listeners -----------------------------------------------------------
    public_host: str = "0.0.0.0"  # noqa: S104 - inside the container; publishing is decided by compose
    public_port: int = 8080
    internal_host: str = "0.0.0.0"  # noqa: S104
    internal_port: int = 8081

    # --- storage -------------------------------------------------------------
    wiki_dir: Path = Path("/data/wiki")
    state_dir: Path = Path("/data/state")
    types_file: Path = Path("/app/config/wiki-types.yaml")
    seed_dir: Path = Path("/app/seed")
    wiki_title: str = "Second Brain"
    max_file_bytes: int = 256 * 1024
    max_source_bytes: int = 1024 * 1024
    stale_after_days: int = 180

    # --- git -----------------------------------------------------------------
    git_remote_url: str = ""
    git_remote_branch: str = "main"
    git_push_debounce_s: float = 60.0
    git_ssh_key_file: Path | None = None

    # --- trust / proposals ---------------------------------------------------
    # Writes whose effective trust is at or below this level become proposals.
    # "none" disables proposals entirely.
    proposals_for: str = "external"

    # --- auth: internal ------------------------------------------------------
    internal_bearer_token: SecretStr = SecretStr("")
    internal_client_name: str = "local"
    # Read-only JSON API for the portal on the internal listener; empty disables it.
    portal_bearer_token: SecretStr = SecretStr("")
    # LibreChat sits behind the owner's own login, so it may accept/reject proposals.
    internal_scopes: CsvList = Field(
        default_factory=lambda: ["wiki:read", "wiki:write", "wiki:admin"]
    )
    internal_allowed_hosts: CsvList = Field(
        default_factory=lambda: [
            "brain",
            "brain:*",
            "wiki-mcp",
            "wiki-mcp:*",
            "localhost:*",
            "127.0.0.1:*",
        ]
    )

    # --- auth: owner / oauth -------------------------------------------------
    owner_username: str = "owner"
    owner_password_hash: SecretStr = SecretStr("")
    owner_totp_secret: SecretStr = SecretStr("")
    access_token_ttl_s: int = 3600
    refresh_token_ttl_s: int = 30 * 24 * 3600
    auth_code_ttl_s: int = 300
    cimd_allowed_hosts: CsvList = Field(
        default_factory=lambda: ["claude.ai", "claude.com", "chatgpt.com", "openai.com"]
    )
    # Empty = any https host is accepted for DCR redirect URIs (loopback http always allowed).
    allowed_redirect_hosts: CsvList = Field(default_factory=list)

    # --- abuse controls ------------------------------------------------------
    rate_limit_per_minute: int = 120
    login_rate_limit_per_minute: int = 10
    login_max_failures: int = 5
    login_lockout_base_s: int = 60

    # --- company brain: setup wizard, local listener, saved config -----------
    # The setup wizard, settings page and REST API. Compose publishes it on the host's
    # 127.0.0.1 only; the tunnel only ever reaches the public listener.
    local_host: str = "0.0.0.0"  # noqa: S104
    local_port: int = 8090
    setup_url: str = "http://localhost:8080/setup"  # logged in setup state (host-side URL)
    # config.yaml (the saved mode) and secrets/ (admin hash, tokens) live here.
    data_dir: Path = Path("/data")
    # Only prefill the wizard; the mode saved in config.yaml decides what runs.
    brain_mode: str = ""
    brain_drive_folder: str = ""
    inbox_adapter: str = ""  # INBOX_ADAPTER: tasks | keep
    brain_note_prefix: str = ""
    keep_delete_after_ingest: bool = False
    # Out-of-band edits (Obsidian, VS Code) reach the search index after this quiet time.
    watch_debounce_ms: int = 1000

    # --- maintenance ---------------------------------------------------------
    lint_schedule: str = "03:30"  # HH:MM (UTC); empty disables
    log_level: str = "INFO"

    @field_validator(
        "trusted_proxy_ips",
        "cimd_allowed_hosts",
        "allowed_redirect_hosts",
        "internal_scopes",
        "internal_allowed_hosts",
        mode="before",
    )
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("proposals_for")
    @classmethod
    def _proposals_for(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"none", *(t.value for t in Trust)}:
            raise ValueError("PROPOSALS_FOR must be none, external, agent or owner")
        return value

    @model_validator(mode="after")
    def _check(self) -> Settings:
        # Resolve eagerly so misconfiguration fails at startup, not at first request.
        _ = self.public_base_url
        for entry in self.trusted_proxy_ips:
            ipaddress.ip_network(entry, strict=False)
        return self

    @cached_property
    def public_base_url(self) -> str | None:
        return resolve_public_base_url(
            self.mcp_exposure_mode,
            mcp_hostname=self.mcp_hostname,
            ts_hostname=self.ts_hostname,
            override=self.public_base_url_override,
        )

    @property
    def resource_url(self) -> str | None:
        """Canonical MCP resource identifier (RFC 8707 / RFC 9728)."""
        base = self.public_base_url
        return f"{base}/mcp" if base else None

    @property
    def public_hostname(self) -> str | None:
        base = self.public_base_url
        return urlsplit(base).netloc if base else None

    @property
    def config_file(self) -> Path:
        return self.data_dir / "config.yaml"

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    @property
    def tailscale_dir(self) -> Path:
        """Shared with the Tailscale Funnel sidecar (and nothing else of the data folder)."""
        return self.data_dir / "tailscale"

    def proposal_threshold(self) -> Trust | None:
        return None if self.proposals_for == "none" else Trust(self.proposals_for)
