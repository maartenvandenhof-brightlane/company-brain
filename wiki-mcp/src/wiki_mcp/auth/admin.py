"""`wiki-mcp clients list|revoke|revoke-all|private`: inspect and revoke OAuth clients, and
switch an app's private access."""

from __future__ import annotations

import datetime as dt
from typing import Any

from wiki_mcp.audit import AuditLog
from wiki_mcp.auth.provider import OAuthProvider
from wiki_mcp.config import Settings


def _fmt(ts: float | None) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d %H:%M") if ts else "-"


def _private(app: dict[str, Any] | None) -> str:
    """private=yes|no and where that comes from: the owner's toggle or the app's consent."""
    if app is None:
        return "private=no (consent)"
    source = "consent" if app["toggle"] == "default" else "toggle"
    return f"private={'yes' if app['private'] else 'no'} ({source})"


def run_clients_command(
    settings: Settings,
    action: str,
    client_id: str | None,
    label: str | None = None,
    access: str | None = None,
) -> int:
    db = settings.state_dir / "oauth.sqlite"
    if not db.exists():
        print("no OAuth state yet (no client has connected)")
        return 0
    audit = AuditLog(settings.state_dir / "audit.sqlite")
    provider = OAuthProvider(
        db,
        issuer=settings.public_base_url or "",
        resource=settings.resource_url or "",
        audit=audit,
        owner_username=settings.owner_username,
    )
    try:
        if action == "list":
            rows = provider.list_clients()
            apps = {a["label"]: a for a in provider.apps()}
            if not rows:
                print("no clients registered")
            for r in rows:
                print(
                    f"{r['label']:<24} {r['kind']:<5} grants={r['active_grants']:<3} "
                    f"{_private(apps.get(r['label'])):<24} "
                    f"created={_fmt(r['created'])} last_used={_fmt(r['last_used'])}\n    {r['client_id']}"
                )
            return 0
        if action == "revoke" and client_id:
            n = provider.revoke_client(client_id)
            print(f"revoked {n} token(s) and removed client {client_id}")
            return 0
        if action == "revoke-all":
            n = provider.revoke_all()
            print(f"revoked {n} token(s); every app must reconnect")
            return 0
        if action == "private" and label and access:
            try:
                app = provider.set_private_access(label, access, by="cli")
            except ValueError as exc:
                print(exc)
                return 2
            print(f"{label}: private access {access}; now {_private(app)}")
            return 0
        print(
            "usage: wiki-mcp clients list | revoke <client_id> | revoke-all"
            " | private <label> on|off|default"
        )
        return 2
    finally:
        provider.close()
        audit.close()
