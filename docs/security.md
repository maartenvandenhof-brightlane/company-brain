# Security

## What is reachable

| Component | Reachable from |
| --- | --- |
| Setup wizard, settings page, REST API (brain `:8090`) | The host's `127.0.0.1` only (`BRAIN_PORT`); answers only `localhost`, `127.0.0.1` and `[::1]` Host headers, so a web page cannot DNS-rebind onto it |
| Portal (`:3090`) | The host's `127.0.0.1` only (`PORTAL_PORT`) |
| Brain internal listener (`:8081`: internal MCP, the portal's API) | The Docker network only, bearer tokens required |
| Brain public listener (`:8080`) | **The internet**, through the [Tailscale Funnel sidecar](tailscale.md), in the `mcp`/`both` modes only |
| Google APIs | Outbound only, in the `gdrive`/`both` modes |

The public listener serves exactly these routes: `/mcp` (OAuth bearer required), the OAuth
endpoints (`/authorize`, `/token`, `/register`, `/revoke`, `/login`), discovery documents under
`/.well-known/`, `/health`, and small informational pages (`/`, `/page/…`, `/robots.txt`) that
contain no wiki content. Nothing else is ever proxied: the sidecar's serve config points only at
`brain:8080`, and the sidecar sees only `data/tailscale/`.

## Authentication

- **Chat apps → MCP connector:** OAuth 2.1 per the MCP authorization spec: Protected Resource
  Metadata (RFC 9728), Authorization Server Metadata (RFC 8414), PKCE S256, resource indicators
  with audience checks (RFC 8707), issuer in the authorization response (RFC 9207), and Client ID
  Metadata Documents (host-allowlisted, `CIMD_ALLOWED_HOSTS`) or Dynamic Client Registration.
  - One owner: the admin password (argon2id hash in `data/secrets/admin_password_hash`),
    CSRF-protected consent page, per-scope consent. `wiki:private` is never granted by default
    and never pre-checked.
  - Access tokens (1 h) and refresh tokens (30 d, rotated) are random; only their SHA-256 hashes
    are stored. A replayed refresh token or authorization code revokes the whole grant.
  - Rate limits per client IP (general, and stricter on `/login` and `/token`), plus a
    registration cap. Repeated failed logins lock out exponentially, per IP and globally.
- **Setup wizard and REST API:** the admin password (in-memory session, CSRF token and
  same-origin check on every form), or the REST bearer token `data/secrets/internal_token`
  (trust `agent`, scopes `INTERNAL_SCOPES`). The public listener accepts neither.
- **Portal:** the same owner login, its own sessions; see [portal](portal.md).

The brain never derives URLs from request headers. The OAuth issuer and resource URLs come from
the DNS name the sidecar publishes (`data/tailscale/dnsname`, only `*.ts.net` accepted) or
`PUBLIC_BASE_URL`. Forwarding headers for the client IP are trusted only from the sidecar's
network (`TRUSTED_PROXY_IPS`).

## Private pages

Pages can be marked `private: true` ([wiki format](wiki-format.md#private-pages)). Only clients
with the `wiki:private` scope see them: apps you ticked *Private pages* for or turned on under
the portal's **Connected apps** (or `make clients ARGS="private <label> on|off|default"`), the
signed-in admin, and the REST token only if `INTERNAL_SCOPES` includes `wiki:private`. For every
other client they are filtered out of search, lists, backlinks, `index.md`, `log.md` and
proposals, and read like missing pages. Private pages are **never exported to Google Drive**.

They are still ordinary files in the wiki's git repository: pushed to `GIT_REMOTE_URL` when set
and part of any copy of `data/`. Keep that remote private and backups encrypted.

## Prompt injection

Wiki pages, ingested sources and inbox notes are **untrusted data**. A malicious page an
assistant is asked to file could contain instructions. Mitigations:

- The server's instructions, the tool descriptions and `AGENTS.md` tell agents never to follow
  instructions found in content. Raw sources carry an explicit "untrusted data" banner; inbox
  notes are stored with trust `external`.
- Trust levels: anything derived from `external` sources becomes a **proposal** under
  `proposals/` instead of a direct change (`PROPOSALS_FOR=external`). Accepting needs
  `wiki:admin`.
- No destructive tools: pages cannot be deleted through MCP or REST, raw sources are immutable,
  and every change is a git commit that can be reverted.
- Credential patterns (API keys, private keys, JWTs, password assignments) are rejected on write.

## Audit log

Every auth event (registration, consent, login success/failure, lockout, token issue, refresh,
revocation, replay), every wizard action and every tool call (client, tool, target path,
outcome, duration) is logged as JSON to stdout and stored in `data/state/audit.sqlite`.
Passwords, tokens, auth keys and codes are never logged. The portal keeps its own audit log in
`data/portal/audit.sqlite`.

## Cutting access

| Situation | Action |
| --- | --- |
| Remove all public exposure now | `docker compose stop tailscale` |
| Revoke one app | `make clients`, then `make clients ARGS="revoke <client_id>"` |
| Admin password may be compromised | Delete `data/secrets/admin_password_hash` and restart the brain: the wizard asks for a new one. Then revoke the apps |
| REST or portal token leaked | Delete `data/secrets/internal_token` or `portal_token` and restart the stack: new ones are generated |
| Google access | **Disconnect** in the wizard's step 2, and remove the app at <https://myaccount.google.com/permissions> |

## Secrets

- Everything secret lives in `data/secrets/` (mode 700, each file 0600): generated tokens, the
  admin password hash, the Google OAuth client, refresh token and Keep service account key. The
  Tailscale auth key and node state are in `data/tailscale/`, the only folder the sidecar sees.
  Nothing secret is in `.env`, `config.yaml`, the wiki or the image.
- Containers run with `no-new-privileges`, read-only root filesystems and all capabilities
  dropped (the brain keeps `CHOWN`/`SETUID`/`SETGID` only to drop to your user id at start).
