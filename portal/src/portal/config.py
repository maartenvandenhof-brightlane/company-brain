"""Portal configuration (environment variables).

Like wiki-mcp, the portal never derives its own URL from request headers: it is
computed from UI_ACCESS_MODE and the hostnames with the same rules as
scripts/lib/modes.sh (wiki_mcp.config.resolve_portal_url). The URL is what CSRF
origin checks compare against.
"""

from __future__ import annotations

import ipaddress
from functools import cached_property
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from wiki_mcp.config import (
    CsvList,
    ExposureMode,
    ModeError,
    UIMode,
    resolve_portal_url,
    resolve_public_base_url,
    resolve_ui_url,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", case_sensitive=False, populate_by_name=True
    )

    # --- access layer (same variables as the rest of the stack) ---------------
    ui_access_mode: UIMode = UIMode.TAILSCALE
    mcp_exposure_mode: ExposureMode = ExposureMode.NONE
    ui_hostname: str = ""
    mcp_hostname: str = ""
    ts_hostname: str = ""
    portal_hostname: str = ""
    ts_ui_https_port: int = 8443
    ts_portal_https_port: int = 9443
    # Local development and tests only: an http://127.0.0.1:<port> URL.
    portal_url_override: str = Field(default="", alias="PORTAL_PUBLIC_URL")
    # Peers allowed to set X-Forwarded-For / CF-Connecting-IP (the edge in front of the portal).
    trusted_proxy_ips: CsvList = Field(default_factory=list)

    # --- listener ------------------------------------------------------------
    host: str = "0.0.0.0"  # noqa: S104 - inside the container; publishing is decided by compose
    port: int = 3090

    # --- owner login (the same credentials as the wiki-mcp connector login) ---
    wiki_title: str = "Second Brain"
    owner_username: str = "owner"
    # Empty: the admin password set in the brain's setup wizard (secrets_dir), read at each login.
    owner_password_hash: SecretStr = SecretStr("")
    # The brain's shared secrets volume (read-only here): admin hash and the portal API token.
    secrets_dir: Path = Path("/data/secrets")
    owner_totp_secret: SecretStr = SecretStr("")

    # --- sessions and abuse controls -----------------------------------------
    session_idle_s: int = Field(default=1800, alias="PORTAL_SESSION_IDLE_S")
    session_max_s: int = Field(default=12 * 3600, alias="PORTAL_SESSION_MAX_S")
    # Key changes need a password (+ code) entered within this window.
    reauth_window_s: int = 900
    rate_limit_per_minute: int = 300
    sensitive_rate_limit_per_minute: int = 10
    login_max_failures: int = 5
    login_lockout_base_s: int = 60

    # --- backends --------------------------------------------------------------
    wiki_api_url: str = "http://wiki-mcp:8081/api/portal/v1"
    wiki_api_token: SecretStr = SecretStr("")
    # Largest chat export the import page accepts (a zip or a bare conversations.json).
    import_max_upload_mb: int = Field(default=512, alias="PORTAL_IMPORT_MAX_MB", ge=1, le=4096)
    # Where the dashboard checks each service on the Docker network.
    librechat_url: str = "http://librechat:3080"
    litellm_url: str = "http://litellm:4000"
    # Batch processing of imported chats calls models through LiteLLM with this key.
    litellm_master_key: SecretStr = Field(default=SecretStr(""), alias="LITELLM_MASTER_KEY")
    # Transcripts extracted at the same time (filing stays one at a time).
    process_concurrency: int = Field(default=4, alias="PORTAL_PROCESS_CONCURRENCY", ge=1, le=16)
    # Ideas are generated each night at this time (HH:MM, UTC: the container's time) and
    # after each batch processing run that filed something.
    ideas_at: str = Field(
        default="03:30", alias="PORTAL_IDEAS_AT", pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    # --- dreaming, ideas and their budget (docs/dreaming.md) ----------------------
    # review: dreams run only by hand (the Dreams page says when one is due); auto: scheduled.
    dream_mode: Literal["review", "auto"] = Field(default="review", alias="DREAM_MODE")
    dream_check_minutes: int = Field(default=60, alias="DREAM_CHECK_MINUTES", ge=1)
    dream_cooldown_hours: int = Field(default=24, alias="DREAM_COOLDOWN_HOURS", ge=0)
    dream_min_changes: int = Field(default=5, alias="DREAM_MIN_CHANGES", ge=1)
    dream_max_days: int = Field(default=7, alias="DREAM_MAX_DAYS", ge=1)
    dream_idle_minutes: int = Field(default=60, alias="DREAM_IDLE_MINUTES", ge=0)
    dream_max_chats: int = Field(default=10, alias="DREAM_MAX_CHATS", ge=0, le=200)
    # The cheap tier, and the strong tier as a preference list: the first one LiteLLM serves.
    model_cheap: str = Field(default="mistral-large", alias="MODEL_CHEAP")
    model_strong: str = Field(default="claude-opus-5-5,gpt-sol-5.6,glm-5-3", alias="MODEL_STRONG")
    # Month-to-date caps in EUR: all LiteLLM spend, and what dreams and ideas spend of it.
    budget_total_eur: float = Field(default=5.0, alias="BUDGET_TOTAL_EUR", ge=0)
    budget_automation_eur: float = Field(default=3.0, alias="BUDGET_AUTOMATION_EUR", ge=0)
    ideas_candidates: int = Field(default=12, alias="IDEAS_CANDIDATES", ge=1, le=30)
    ideas_max_shown: int = Field(default=3, alias="IDEAS_MAX_SHOWN", ge=1, le=10)
    hot_memory_max_chars: int = Field(default=6000, alias="HOT_MEMORY_MAX_CHARS", ge=1000, le=20000)
    wiki_mcp_internal_url: str = "http://wiki-mcp:8081"
    wiki_mcp_public_url: str = "http://wiki-mcp:8080"

    # --- storage -------------------------------------------------------------
    state_dir: Path = Path("/data/state")
    docs_dir: Path = Path("/app/docs")
    # Provider keys (keys.env) and the rendered model configs: the portal's only writable
    # host directories. config/ and refresh_models.py are mounted read-only.
    keys_dir: Path = Path("/data/keys")
    models_dir: Path = Path("/data/models")
    config_dir: Path = Path("/config")
    refresh_script: Path = Path("/app/scripts/refresh_models.py")
    cost_currency: str = "USD"
    # LiteLLM's spend log (one JSON line per model call), mounted read-only.
    usage_dir: Path = Path("/data/usage")

    log_level: str = "INFO"

    @field_validator("trusted_proxy_ips", mode="before")
    @classmethod
    def _csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if self.portal_url is None:
            raise ModeError(
                f"UI_ACCESS_MODE={self.ui_access_mode.value} needs PORTAL_HOSTNAME for the portal"
            )
        if (
            self.ui_access_mode is UIMode.CADDY
            and not self.portal_url_override
            and not self.owner_totp_secret.get_secret_value()
        ):
            raise ModeError(
                "the portal in caddy mode is public: OWNER_TOTP_SECRET is required (make setup)"
            )
        for entry in self.trusted_proxy_ips:
            ipaddress.ip_network(entry, strict=False)
        return self

    @cached_property
    def portal_url(self) -> str | None:
        if self.portal_url_override:
            parts = urlsplit(self.portal_url_override)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ModeError("PORTAL_PUBLIC_URL must be an absolute http(s) URL")
            return f"{parts.scheme}://{parts.netloc}".lower()
        return resolve_portal_url(
            self.ui_access_mode,
            portal_hostname=self.portal_hostname,
            ts_hostname=self.ts_hostname,
            ts_portal_port=self.ts_portal_https_port,
            ts_ui_port=self.ts_ui_https_port,
            ui_hostname=self.ui_hostname,
            mcp_hostname=self.mcp_hostname,
        )

    def password_hash(self) -> str:
        """OWNER_PASSWORD_HASH, else the admin password from the brain's setup wizard ("" until
        it is set: every sign-in fails)."""
        return self.owner_password_hash.get_secret_value() or _secret(
            self.secrets_dir / "admin_password_hash"
        )

    def api_token(self) -> str:
        """WIKI_API_TOKEN, else the token the brain generated in the shared secrets volume."""
        return self.wiki_api_token.get_secret_value() or _secret(self.secrets_dir / "portal_token")

    @property
    def origin(self) -> str:
        assert self.portal_url
        return self.portal_url

    @property
    def secure_cookies(self) -> bool:
        return self.origin.startswith("https://")

    @property
    def strong_models(self) -> list[str]:
        """MODEL_STRONG in order of preference."""
        return [m.strip() for m in self.model_strong.split(",") if m.strip()]

    @cached_property
    def ui_url(self) -> str:
        return resolve_ui_url(
            self.ui_access_mode,
            ui_hostname=self.ui_hostname,
            ts_hostname=self.ts_hostname,
            ts_ui_port=self.ts_ui_https_port,
        )

    @cached_property
    def mcp_base_url(self) -> str | None:
        try:
            return resolve_public_base_url(
                self.mcp_exposure_mode,
                mcp_hostname=self.mcp_hostname,
                ts_hostname=self.ts_hostname,
            )
        except ModeError:
            return None


def _secret(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
