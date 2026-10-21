"""What the setup wizard saves: the mode and its settings in /data/config.yaml, secrets in
/data/secrets/ (one 0600 file each, never in config.yaml and never logged).

No config.yaml means setup state: the local listener sends everything to /setup and no adapter
runs. Environment variables (BRAIN_MODE, ...) only prefill the wizard; the saved file decides.
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from wiki_mcp.config import Settings

ADMIN_HASH = "admin_password_hash"  # the wizard's admin password = the owner/connector login
INTERNAL_TOKEN = "internal_token"  # noqa: S105 - a file name: internal MCP and REST bearer
PORTAL_TOKEN = "portal_token"  # noqa: S105 - a file name: the portal API token
# The Tailscale Funnel sidecar's files in data/tailscale/ (tailscale/entrypoint.sh).
TS_AUTHKEY = "authkey"  # written by the wizard (0600), used for the sidecar's first login
TS_HOSTNAME = "hostname"  # written by the wizard: the machine name to log in with
TS_DNSNAME = "dnsname"  # written by the sidecar once logged in: <hostname>.<tailnet>.ts.net


class Mode(StrEnum):
    MCP = "mcp"
    GDRIVE = "gdrive"
    BOTH = "both"
    FOLDER = "folder"  # standalone: never combined with the others


MODE_HELP = {
    Mode.MCP: "MCP connector: your chat app reads and writes the brain directly. Needs custom "
    "connectors and a public HTTPS tunnel (Tailscale Funnel).",
    Mode.GDRIVE: "Google Drive + inbox: pages are exported as Google Docs your chat app reads, "
    "and notes you save land in the inbox. Works when custom connectors are blocked.",
    Mode.BOTH: "Both: the MCP connector and the Google Drive export with the inbox.",
    Mode.FOLDER: "Google Drive by hand: pages are written as Markdown files to data/drive/Brain "
    "for you to upload to Drive, and the Drive folder downloaded into data/drive/inbox brings "
    "back the pages Gemini added or changed. No Google Cloud project, sign-in or tunnel.",
}
TUNNELS = ("tailscale",)
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]+$")
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
INBOXES = ("tasks", "keep")  # tasks: plain user OAuth; keep: needs a Workspace admin


@dataclass(frozen=True)
class McpSettings:
    tunnel: str = "tailscale"
    hostname: str = (
        "company-brain"  # the Tailscale machine name: https://<hostname>.<tailnet>.ts.net
    )


@dataclass(frozen=True)
class DriveSettings:
    folder: str = "Company Brain"
    inbox: str = "tasks"
    prefix: str = "[brain]"
    tasklist: str = "@default"  # Tasks: a list id or title; Gemini adds tasks to the default list
    poll_minutes: int = 5
    keep_user: str = ""  # Keep: the Workspace user the service account reads notes of
    keep_delete: bool = False  # Keep: delete a note once it is ingested (needs the keep scope)


@dataclass(frozen=True)
class BrainConfig:
    mode: Mode
    mcp: McpSettings = field(default_factory=McpSettings)
    gdrive: DriveSettings = field(default_factory=DriveSettings)

    @classmethod
    def from_dict(cls, data: Any) -> BrainConfig:
        """Validate untrusted input (the wizard's form, a hand-edited config.yaml)."""
        if not isinstance(data, dict):
            raise ValueError("expected a mapping")
        try:
            mode = Mode(str(data.get("mode")))
        except ValueError as exc:
            raise ValueError("mode must be mcp, gdrive, both or folder") from exc
        mcp, drive = data.get("mcp") or {}, data.get("gdrive") or {}
        if not isinstance(mcp, dict) or not isinstance(drive, dict):
            raise ValueError("mcp and gdrive must be mappings")
        tunnel = str(mcp.get("tunnel", "tailscale"))
        if tunnel not in TUNNELS:
            raise ValueError("tunnel must be tailscale")
        hostname = str(mcp.get("hostname", "company-brain")).strip().lower()
        if not _HOSTNAME.match(hostname):
            raise ValueError("the hostname must be letters, digits and hyphens (at most 63)")
        folder = _text(drive.get("folder", "Company Brain"), "Drive folder name", 100)
        inbox = str(drive.get("inbox", "tasks"))
        if inbox not in INBOXES:
            raise ValueError("inbox must be tasks or keep")
        prefix = _text(drive.get("prefix", "[brain]"), "note prefix", 30)
        tasklist = _text(drive.get("tasklist", "@default"), "task list", 100)
        try:
            minutes = int(drive.get("poll_minutes", 5))
        except (TypeError, ValueError):
            minutes = 0
        if not 1 <= minutes <= 1440:
            raise ValueError("poll interval must be 1-1440 minutes")
        keep_user = str(drive.get("keep_user") or "").strip()
        if keep_user and not _EMAIL.match(keep_user):
            raise ValueError("the Keep user must be an email address")
        keep_delete = drive.get("keep_delete", False) in (True, "true", "on")
        return cls(
            mode,
            McpSettings(tunnel, hostname),
            DriveSettings(folder, inbox, prefix, tasklist, minutes, keep_user, keep_delete),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode.value, "mcp": asdict(self.mcp), "gdrive": asdict(self.gdrive)}


def _text(value: Any, what: str, most: int) -> str:
    text = str(value).strip()
    if not text or len(text) > most or any(ord(c) < 32 for c in text):
        raise ValueError(f"{what} must be 1-{most} characters on one line")
    return text


def load_config(path: Path) -> BrainConfig | None:
    """The saved config, or None (setup state). A broken file fails loudly at startup."""
    if not path.is_file():
        return None
    try:
        return BrainConfig.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: {exc}; fix it or delete it to run the setup wizard") from exc


def save_config(path: Path, config: BrainConfig) -> None:
    _write(path, yaml.safe_dump(config.as_dict(), sort_keys=False), 0o644)


def prefill(saved: BrainConfig | None, settings: Settings) -> BrainConfig:
    """What the wizard shows: the saved config, else the BRAIN_* variables (and
    INBOX_ADAPTER, KEEP_DELETE_AFTER_INGEST), else defaults.
    An invalid variable is ignored (the wizard shows the default instead)."""
    if saved is not None:
        return saved
    drive: dict[str, Any] = {
        k: v
        for k, v in (
            ("folder", settings.brain_drive_folder),
            ("inbox", settings.inbox_adapter),
            ("prefix", settings.brain_note_prefix),
            ("keep_delete", settings.keep_delete_after_ingest),
        )
        if v
    }
    data = {"mode": settings.brain_mode or "mcp", "gdrive": drive}
    try:
        return BrainConfig.from_dict(data)
    except ValueError:
        return BrainConfig(Mode.MCP)


# ---------------------------------------------------------------- secrets


def read_secret(secrets_dir: Path, name: str) -> str:
    path = secrets_dir / name
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def write_secret(secrets_dir: Path, name: str, value: str) -> None:
    secrets_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(secrets_dir / name, value + "\n", 0o600)


def ensure_token(secrets_dir: Path, name: str) -> str:
    """A shared token from the secrets volume, generated on first start (never typed)."""
    token = read_secret(secrets_dir, name)
    if len(token) < 32:
        token = secrets.token_urlsafe(32)
        write_secret(secrets_dir, name, token)
    return token


def owner_hash(settings: Settings) -> str:
    """The owner's argon2id hash: the wizard's admin password, else OWNER_PASSWORD_HASH."""
    return read_secret(settings.secrets_dir, ADMIN_HASH) or (
        settings.owner_password_hash.get_secret_value()
    )


def _write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    tmp.replace(path)
