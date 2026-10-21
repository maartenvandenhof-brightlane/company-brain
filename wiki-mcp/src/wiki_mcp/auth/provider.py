"""OAuth 2.1 authorization server state, implemented as the MCP SDK provider protocol.

- Tokens are opaque random strings; only their SHA-256 hashes are stored.
- Every grant has a `family` id shared by its code, access and refresh tokens, so
  revoking one revokes all (refresh rotation, code-reuse detection, client revoke).
- Access tokens are bound to the canonical MCP resource URL (RFC 8707 audience).
- Clients come from Dynamic Client Registration (stored) or from Client ID
  Metadata Documents (fetched from an allowlisted HTTPS URL, then cached).
- The owner can toggle private access per app (by label: one app may have several
  client_ids). The toggle overrides what a token was consented with, on every request;
  stored token scopes stay as consented.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from wiki_mcp.audit import AuditLog
from wiki_mcp.wiki.actor import PRIVATE_SCOPE, client_slug

# wiki:private: pages marked `private: true` (personal life). Never granted by default, and
# never pre-checked on the consent page even when a client asks for it.
SCOPES = ["wiki:read", "wiki:write", "wiki:admin", "wiki:private"]
DEFAULT_GRANT = ["wiki:read", "wiki:write"]
MAX_DCR_CLIENTS = 200
PENDING_TTL_S = 600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,            -- dcr | cimd
    info TEXT NOT NULL,
    label TEXT NOT NULL,
    created REAL NOT NULL,
    last_used REAL,
    fetched REAL
);
CREATE TABLE IF NOT EXISTS pending (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    params TEXT NOT NULL,
    csrf TEXT NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS codes (
    hash TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    family TEXT NOT NULL,
    expires REAL NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tokens (
    hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,            -- access | refresh
    client_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT,
    claims TEXT NOT NULL,
    family TEXT NOT NULL,
    expires REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tokens_family ON tokens(family);
CREATE INDEX IF NOT EXISTS tokens_client ON tokens(client_id);
CREATE TABLE IF NOT EXISTS private_access (
    label TEXT PRIMARY KEY,        -- the app's label (client_label), not a client_id
    access TEXT NOT NULL,          -- on | off; no row: as consented
    changed REAL NOT NULL,
    changed_by TEXT NOT NULL
);
"""
# The owner's private-access toggle per app: on, off, or default (as consented).
PRIVATE_ACCESS = ("on", "off", "default")


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _is_loopback_host(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1", "[::1]")


def host_allowed(host: str, allowed: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in (x.lower() for x in allowed))


def client_label(info: OAuthClientInformationFull, kind: str) -> str:
    """Short, stable name used as git author and in audit logs (e.g. 'claude.ai', 'chatgpt')."""
    redirect_hosts = {urlsplit(str(u)).hostname or "" for u in (info.redirect_uris or [])}
    if kind == "cimd":
        host = urlsplit(info.client_id).hostname or ""
        if host_allowed(host, ["claude.ai", "claude.com", "anthropic.com"]):
            return "claude.ai"
        if host_allowed(host, ["chatgpt.com", "openai.com"]):
            return "chatgpt"
        return client_slug(host)
    name = (info.client_name or "").lower()
    known = [
        ("claude", ["claude.ai", "claude.com"], "claude.ai"),
        ("chatgpt", ["chatgpt.com", "openai.com"], "chatgpt"),
        ("openai", ["chatgpt.com", "openai.com"], "chatgpt"),
        ("mistral", ["mistral.ai"], "lechat"),
        ("le chat", ["mistral.ai"], "lechat"),
    ]
    # Every redirect host must fit the label: the owner's private-access toggle is per label,
    # so a registration that adds one of its own hosts must not share a known app's label (it
    # could take the code to its own host and inherit that app's toggle).
    for needle, hosts, label in known:
        if (
            needle in name
            and redirect_hosts
            and all(host_allowed(h, hosts) for h in redirect_hosts)
        ):
            return label
    if redirect_hosts and all(_is_loopback_host(h) for h in redirect_hosts):
        base = client_slug(info.client_name or "local-client")
        return f"{base}-local" if not base.endswith("local") else base
    base = client_slug(info.client_name or "oauth-client")
    host = "+".join(sorted(redirect_hosts)) if redirect_hosts else "unknown"
    return f"{base}@{host}"


@dataclass
class PendingAuthorization:
    id: str
    client_id: str
    params: AuthorizationParams
    csrf: str
    expires: float


class OAuthProvider:
    def __init__(
        self,
        db_path: Path | str,
        *,
        issuer: str,
        resource: str,
        audit: AuditLog,
        owner_username: str,
        access_ttl: int = 3600,
        refresh_ttl: int = 30 * 24 * 3600,
        code_ttl: int = 300,
        allowed_redirect_hosts: list[str] | None = None,
        cimd_allowed_hosts: list[str] | None = None,
        cimd_fetch: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        client_trust: str = "agent",
    ) -> None:
        self.db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        self._lock = threading.Lock()
        self.issuer = issuer
        self.resource = resource
        self.audit = audit
        # Clients that are not OAuth apps but take a toggle too (the internal listener's).
        self.internal_labels: set[str] = set()
        self.owner_username = owner_username
        self.access_ttl = access_ttl
        self.refresh_ttl = refresh_ttl
        self.code_ttl = code_ttl
        self.allowed_redirect_hosts = allowed_redirect_hosts or []
        self.cimd_allowed_hosts = cimd_allowed_hosts or []
        self.cimd_fetch = cimd_fetch
        self.client_trust = client_trust

    def close(self) -> None:
        self.db.close()

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.db.execute(sql, params)

    # ================================================================ clients

    def validate_redirect_uris(self, uris: list[str]) -> None:
        if not uris or len(uris) > 10:
            raise RegistrationError("invalid_redirect_uri", "1-10 redirect_uris are required")
        for uri in uris:
            parts = urlsplit(uri)
            host = (parts.hostname or "").lower()
            if parts.fragment or not host:
                raise RegistrationError("invalid_redirect_uri", f"invalid redirect URI {uri}")
            if parts.scheme == "http" and _is_loopback_host(host):
                continue
            if parts.scheme != "https":
                raise RegistrationError(
                    "invalid_redirect_uri",
                    f"redirect URIs must use https (or http on loopback): {uri}",
                )
            if self.allowed_redirect_hosts and not host_allowed(host, self.allowed_redirect_hosts):
                raise RegistrationError(
                    "invalid_redirect_uri", f"redirect host {host} is not allowed on this server"
                )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if client_id.startswith("https://"):
            return await self._get_cimd_client(client_id)
        row = self._exec(
            "SELECT info FROM clients WHERE client_id = ? AND kind = 'dcr'", (client_id,)
        ).fetchone()
        return _stored_client(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        count = self._exec("SELECT COUNT(*) FROM clients WHERE kind = 'dcr'").fetchone()[0]
        if count >= MAX_DCR_CLIENTS:
            self._prune_unused_clients()
            count = self._exec("SELECT COUNT(*) FROM clients WHERE kind = 'dcr'").fetchone()[0]
            if count >= MAX_DCR_CLIENTS:
                raise RegistrationError("invalid_client_metadata", "too many registered clients")
        self.validate_redirect_uris([str(u) for u in client_info.redirect_uris or []])
        if client_info.client_name and len(client_info.client_name) > 100:
            raise RegistrationError("invalid_client_metadata", "client_name too long")
        client_info.scope = " ".join(SCOPES)
        label = client_label(client_info, "dcr")
        self._exec(
            "INSERT INTO clients (client_id, kind, info, label, created) VALUES (?,?,?,?,?)",
            (client_info.client_id, "dcr", client_info.model_dump_json(), label, time.time()),
        )
        self.audit.event(
            "client_registered", client=label, client_id=client_info.client_id, method="dcr"
        )

    def _prune_unused_clients(self) -> None:
        cutoff = time.time() - 24 * 3600
        self._exec(
            "DELETE FROM clients WHERE kind = 'dcr' AND last_used IS NULL AND created < ?",
            (cutoff,),
        )

    async def _get_cimd_client(self, client_id: str) -> OAuthClientInformationFull | None:
        parts = urlsplit(client_id)
        host = parts.hostname or ""
        if not self.cimd_fetch or not host_allowed(host, self.cimd_allowed_hosts):
            self.audit.event("cimd", client=host, outcome="denied", client_id=client_id)
            return None
        row = self._exec(
            "SELECT info, fetched FROM clients WHERE client_id = ? AND kind = 'cimd'", (client_id,)
        ).fetchone()
        if row and row[1] and time.time() - row[1] < 24 * 3600:
            return _stored_client(row[0])
        try:
            doc = await self.cimd_fetch(client_id)
            info = self._cimd_to_client(client_id, doc)
        except Exception as exc:
            self.audit.event(
                "cimd", client=host, outcome="error", client_id=client_id, error=str(exc)
            )
            if row:  # serve the stale copy rather than locking the user out
                return _stored_client(row[0])
            return None
        label = client_label(info, "cimd")
        self._exec(
            "INSERT INTO clients (client_id, kind, info, label, created, fetched) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(client_id) DO UPDATE SET info = excluded.info, label = excluded.label, "
            "fetched = excluded.fetched",
            (client_id, "cimd", info.model_dump_json(), label, time.time(), time.time()),
        )
        self.audit.event("cimd", client=label, client_id=client_id, outcome="fetched")
        return info

    def _cimd_to_client(self, client_id: str, doc: dict[str, Any]) -> OAuthClientInformationFull:
        if doc.get("client_id") != client_id:
            raise ValueError("metadata document client_id does not match its URL")
        uris = doc.get("redirect_uris")
        if not isinstance(uris, list) or not all(isinstance(u, str) for u in uris):
            raise ValueError("metadata document has no redirect_uris")
        self.validate_redirect_uris(uris)
        grant_types = doc.get("grant_types") or ["authorization_code", "refresh_token"]
        return OAuthClientInformationFull(
            client_id=client_id,
            client_name=str(doc.get("client_name") or urlsplit(client_id).hostname)[:100],
            redirect_uris=uris,
            grant_types=[g for g in grant_types if isinstance(g, str)],
            response_types=["code"],
            # Public client: PKCE protects the code. private_key_jwt assertions, if a client
            # sends them, are not required here.
            token_endpoint_auth_method="none",  # noqa: S106
            scope=" ".join(SCOPES),
        )

    def label_for(self, client_id: str) -> str:
        row = self._exec("SELECT label FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        return str(row[0]) if row else client_slug(client_id)

    def list_clients(self) -> list[dict[str, Any]]:
        rows = self._exec(
            "SELECT c.client_id, c.kind, c.label, c.created, c.last_used, "
            "(SELECT COUNT(*) FROM tokens t WHERE t.client_id = c.client_id AND t.revoked = 0 "
            " AND t.kind = 'refresh' AND t.expires > ?) "
            "FROM clients c ORDER BY c.created",
            (time.time(),),
        ).fetchall()
        return [
            {
                "client_id": r[0],
                "kind": r[1],
                "label": r[2],
                "created": r[3],
                "last_used": r[4],
                "active_grants": r[5],
            }
            for r in rows
        ]

    # ================================================================ private access per app

    def private_toggle(self, label: str) -> str | None:
        """The owner's toggle for this app: on, off, or None (as consented)."""
        row = self._exec("SELECT access FROM private_access WHERE label = ?", (label,)).fetchone()
        return str(row[0]) if row else None

    def effective_scopes(self, client_id: str, scopes: list[str]) -> list[str]:
        """A token's consented scopes with its app's toggle applied: `on` adds wiki:private,
        `off` removes it, no toggle leaves the consent as it is."""
        return self.scopes_for_label(self.label_for(client_id), scopes)

    def scopes_for_label(self, label: str, scopes: list[str]) -> list[str]:
        """`scopes` with this app's toggle applied (also the internal client, LibreChat)."""
        toggle = self.private_toggle(label)
        if toggle is None:
            return scopes
        kept = [s for s in scopes if s != PRIVATE_SCOPE]
        return [*kept, PRIVATE_SCOPE] if toggle == "on" else kept

    def apps(self) -> list[dict[str, Any]]:
        """Connected apps by label: their kinds, grants, whether an active token was consented
        with wiki:private, the toggle (on | off | default) and the effective private access."""
        now = time.time()
        rows = self._exec(
            "SELECT c.label, c.kind, c.created, c.last_used, "
            "(SELECT COUNT(*) FROM tokens t WHERE t.client_id = c.client_id AND t.revoked = 0 "
            " AND t.kind = 'refresh' AND t.expires > ?), "
            "(SELECT COUNT(*) FROM tokens t WHERE t.client_id = c.client_id AND t.revoked = 0 "
            " AND t.expires > ? AND ' ' || t.scopes || ' ' LIKE ?) "
            "FROM clients c ORDER BY c.created",
            (now, now, f"% {PRIVATE_SCOPE} %"),
        ).fetchall()
        toggles = dict(self._exec("SELECT label, access FROM private_access").fetchall())
        apps: dict[str, dict[str, Any]] = {}
        for label, kind, created, last_used, grants, consented in rows:
            app = apps.setdefault(
                label,
                {
                    "label": label,
                    "kinds": set(),
                    "created": created,
                    "last_used": None,
                    "active_grants": 0,
                    "consented": False,
                },
            )
            app["kinds"].add(kind)
            if last_used and (app["last_used"] is None or last_used > app["last_used"]):
                app["last_used"] = last_used
            app["active_grants"] += grants
            app["consented"] = app["consented"] or consented > 0
        for app in apps.values():
            toggle = toggles.get(app["label"])
            app["kind"] = "+".join(sorted(app.pop("kinds")))
            app["toggle"] = toggle or "default"
            app["private"] = toggle == "on" or (toggle is None and app["consented"])
        return list(apps.values())

    def set_private_access(
        self, label: str, access: str, *, by: str, ip: str | None = None
    ) -> dict[str, Any]:
        """Set an app's private access: on, off or default (as consented). Only labels of
        registered clients (or a stale toggle being reset); audited with who changed it."""
        if access not in PRIVATE_ACCESS:
            raise ValueError("access must be on, off or default")
        previous = self.private_toggle(label)
        known = self._exec("SELECT 1 FROM clients WHERE label = ? LIMIT 1", (label,)).fetchone()
        known = known or label in self.internal_labels
        if not known and not (access == "default" and previous is not None):
            raise ValueError(f"no connected app is called {label!r}")
        if access == "default":
            self._exec("DELETE FROM private_access WHERE label = ?", (label,))
        else:
            self._exec(
                "INSERT INTO private_access VALUES (?,?,?,?) ON CONFLICT(label) DO UPDATE SET "
                "access = excluded.access, changed = excluded.changed, "
                "changed_by = excluded.changed_by",
                (label, access, time.time(), by),
            )
        self.audit.event(
            "private_access",
            client=label,
            ip=ip,
            outcome="ok",
            access=access,
            previous=previous or "default",
            by=by,
        )
        app = next((a for a in self.apps() if a["label"] == label), None)
        if app is None and label in self.internal_labels:
            # private: None when it follows the internal listener's configured scopes
            return {
                "label": label,
                "toggle": None if access == "default" else access,
                "private": {"on": True, "off": False}.get(access),
            }
        return app or {"label": label, "toggle": "default"}

    def revoke_client(self, client_id: str) -> int:
        n = self._exec(
            "UPDATE tokens SET revoked = 1 WHERE client_id = ? AND revoked = 0", (client_id,)
        ).rowcount
        self._exec("DELETE FROM clients WHERE client_id = ?", (client_id,))
        self.audit.event("client_revoked", client=client_id, tokens=n)
        return n

    def revoke_all(self) -> int:
        n = self._exec("UPDATE tokens SET revoked = 1 WHERE revoked = 0").rowcount
        self._exec("DELETE FROM pending")
        self.audit.event("all_tokens_revoked", tokens=n)
        return n

    # ================================================================ authorize → login

    def canonical_resource(self, requested: str | None) -> str:
        if requested is None:
            return self.resource
        norm = requested.strip().rstrip("/").lower()
        if norm in (self.resource.lower(), self.issuer.lower()):
            return self.resource
        raise AuthorizeError(
            "invalid_request", f"unknown resource {requested!r}; use {self.resource}"
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        params.resource = self.canonical_resource(params.resource)
        requested = params.scopes or DEFAULT_GRANT
        unknown = [s for s in requested if s not in SCOPES]
        if unknown:
            raise AuthorizeError("invalid_scope", f"unsupported scopes: {' '.join(unknown)}")
        params.scopes = requested
        rid = secrets.token_urlsafe(24)
        csrf = secrets.token_urlsafe(24)
        now = time.time()
        self._exec("DELETE FROM pending WHERE expires < ?", (now,))
        self._exec(
            "INSERT INTO pending VALUES (?,?,?,?,?)",
            (rid, client.client_id, params.model_dump_json(), csrf, now + PENDING_TTL_S),
        )
        return f"{self.issuer}/login?request={rid}"

    def get_pending(self, rid: str) -> PendingAuthorization | None:
        row = self._exec(
            "SELECT id, client_id, params, csrf, expires FROM pending WHERE id = ?", (rid,)
        ).fetchone()
        if not row or row[4] < time.time():
            return None
        return PendingAuthorization(
            row[0], row[1], AuthorizationParams.model_validate_json(row[2]), row[3], row[4]
        )

    def drop_pending(self, rid: str) -> None:
        self._exec("DELETE FROM pending WHERE id = ?", (rid,))

    def issue_code(self, pending: PendingAuthorization, scopes: list[str]) -> str:
        code = secrets.token_urlsafe(32)
        family = secrets.token_hex(16)
        p = pending.params
        auth_code = AuthorizationCode(
            code=code,
            scopes=scopes,
            expires_at=time.time() + self.code_ttl,
            client_id=pending.client_id,
            code_challenge=p.code_challenge,
            redirect_uri=p.redirect_uri,
            redirect_uri_provided_explicitly=p.redirect_uri_provided_explicitly,
            resource=p.resource,
            subject=self.owner_username,
        )
        self._exec(
            "INSERT INTO codes VALUES (?,?,?,?,0)",
            (_h(code), auth_code.model_dump_json(exclude={"code"}), family, auth_code.expires_at),
        )
        self.drop_pending(pending.id)
        return code

    # ================================================================ codes → tokens

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = self._exec(
            "SELECT data, family, used FROM codes WHERE hash = ?", (_h(authorization_code),)
        ).fetchone()
        if not row:
            return None
        if row[2]:
            # OAuth 2.1 §4.1.3: a reused code revokes everything issued from it.
            n = self._revoke_family(row[1])
            self.audit.event(
                "code_reuse", client=self.label_for(client.client_id), outcome="revoked", tokens=n
            )
            return None
        code = AuthorizationCode.model_validate({**json.loads(row[0]), "code": authorization_code})
        if code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        h = _h(authorization_code.code)
        cur = self._exec("UPDATE codes SET used = 1 WHERE hash = ? AND used = 0", (h,))
        if cur.rowcount != 1:
            raise TokenError("invalid_grant", "authorization code already used")
        family = self._exec("SELECT family FROM codes WHERE hash = ?", (h,)).fetchone()[0]
        self._touch(client.client_id)
        token = self._issue(
            client.client_id, authorization_code.scopes, authorization_code.resource, family
        )
        self.audit.event(
            "token_issued",
            client=self.label_for(client.client_id),
            grant="authorization_code",
            scopes=authorization_code.scopes,
        )
        return token

    def _claims(self, client_id: str) -> dict[str, Any]:
        return {
            "client_name": self.label_for(client_id),
            "trust": self.client_trust,
            "human": self.owner_username,
            "iss": self.issuer,
            "sub": self.owner_username,
        }

    def _issue(
        self, client_id: str, scopes: list[str], resource: str | None, family: str
    ) -> OAuthToken:
        now = time.time()
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(40)
        claims = json.dumps(self._claims(client_id))
        scope_s = " ".join(scopes)
        self._exec(
            "INSERT INTO tokens VALUES (?,?,?,?,?,?,?,?,0,?)",
            (
                _h(access),
                "access",
                client_id,
                scope_s,
                resource,
                claims,
                family,
                now + self.access_ttl,
                now,
            ),
        )
        self._exec(
            "INSERT INTO tokens VALUES (?,?,?,?,?,?,?,?,0,?)",
            (
                _h(refresh),
                "refresh",
                client_id,
                scope_s,
                resource,
                claims,
                family,
                now + self.refresh_ttl,
                now,
            ),
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",  # noqa: S106
            expires_in=self.access_ttl,
            refresh_token=refresh,
            scope=scope_s,
        )

    def _touch(self, client_id: str) -> None:
        self._exec("UPDATE clients SET last_used = ? WHERE client_id = ?", (time.time(), client_id))

    def _revoke_family(self, family: str) -> int:
        return self._exec(
            "UPDATE tokens SET revoked = 1 WHERE family = ? AND revoked = 0", (family,)
        ).rowcount

    def _load(self, token: str, kind: str) -> sqlite3.Row | tuple[Any, ...] | None:
        row = self._exec(
            "SELECT client_id, scopes, resource, claims, family, expires, revoked FROM tokens "
            "WHERE hash = ? AND kind = ?",
            (_h(token), kind),
        ).fetchone()
        if not row or row[6] or row[5] < time.time():
            return None
        return row  # type: ignore[no-any-return]

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = self._exec(
            "SELECT client_id, scopes, resource, family, expires, revoked FROM tokens "
            "WHERE hash = ? AND kind = 'refresh'",
            (_h(refresh_token),),
        ).fetchone()
        if not row or row[0] != client.client_id or row[4] < time.time():
            return None
        if row[5]:
            # A rotated-away refresh token was replayed: assume theft, kill the grant.
            n = self._revoke_family(row[3])
            self.audit.event(
                "refresh_reuse",
                client=self.label_for(client.client_id),
                outcome="revoked",
                tokens=n,
            )
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row[0],
            scopes=row[1].split(),
            expires_at=int(row[4]),
            resource=row[2],
            subject=self.owner_username,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        row = self._exec(
            "SELECT family FROM tokens WHERE hash = ? AND kind = 'refresh' AND revoked = 0",
            (_h(refresh_token.token),),
        ).fetchone()
        if not row:
            raise TokenError("invalid_grant", "refresh token is no longer valid")
        family = row[0]
        # Rotate: retire the old refresh token and the family's outstanding access tokens.
        self._exec("UPDATE tokens SET revoked = 1 WHERE family = ? AND revoked = 0", (family,))
        self._touch(client.client_id)
        token = self._issue(client.client_id, scopes, refresh_token.resource, family)
        self.audit.event("token_refreshed", client=self.label_for(client.client_id))
        return token

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self._load(token, "access")
        if row is None:
            return None
        return AccessToken(
            token=token,
            client_id=row[0],
            # the owner's toggle for the app applies from the next request on
            scopes=self.effective_scopes(row[0], row[1].split()),
            expires_at=int(row[5]),
            resource=row[2],
            subject=self.owner_username,
            claims=json.loads(row[3]),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        row = self._exec("SELECT family FROM tokens WHERE hash = ?", (_h(token.token),)).fetchone()
        if row:
            n = self._revoke_family(row[0])
            self.audit.event("token_revoked", client=self.label_for(token.client_id), tokens=n)

    async def exchange_identity_assertion(self, *args: Any, **kwargs: Any) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "identity assertion grant is not supported")


def _stored_client(info: str) -> OAuthClientInformationFull:
    """A stored client with today's scope list: every client may ask for every scope (the owner
    decides on the consent page), also clients registered before a scope was added."""
    client = OAuthClientInformationFull.model_validate_json(info)
    client.scope = " ".join(SCOPES)
    return client


_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{10,64}$")


def valid_request_id(value: str) -> bool:
    return bool(_SAFE_ID.match(value))
