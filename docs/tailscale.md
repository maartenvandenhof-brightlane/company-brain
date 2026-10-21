# Tailscale Funnel

In the `mcp` and `both` modes, chat apps reach the brain's MCP connector at
`https://<machine>.<tailnet>.ts.net/mcp`, a public HTTPS URL with a public-CA certificate. A
**Tailscale sidecar container** (`tailscale` in `compose.yaml`, the official
`tailscale/tailscale` image, pinned) provides it. Nothing is installed on the host, and nothing
is typed in a terminal: the auth key and machine name come from the setup wizard.

## What is public

Exactly one thing: the brain's public listener (`brain:8080`, never published on the host), via
Funnel on port 443. Its routes are `/mcp` (OAuth bearer required), the OAuth endpoints
(`/authorize`, `/token`, `/register`, `/revoke`, `/login`), the discovery documents under
`/.well-known/`, `/health` and two informational pages without wiki content. The setup wizard,
the REST API (`:8090`) and the portal are never routed: the sidecar's serve config
(`tailscale/serve.json`) proxies only to `http://brain:8080`. In the `gdrive` mode the public
listener is stopped, so the Funnel URL answers 502.

## Once per tailnet (in the Tailscale admin console)

1. Sign in at <https://login.tailscale.com/admin> (a free account is enough).
2. **DNS**: enable **MagicDNS** and **HTTPS Certificates**.
3. **Access controls**: allow Funnel. Add this to the tailnet policy file (merge it into an
   existing `nodeAttrs` list):

   ```json
   "nodeAttrs": [
     { "target": ["autogroup:member"], "attr": ["funnel"] }
   ]
   ```

   To allow Funnel for the brain only, tag its auth key (for example `tag:brain`, declared under
   `tagOwners`) and use `"target": ["tag:brain"]`.
4. **Settings → Keys → Generate auth key**: **Reusable off**, **Ephemeral off** (the brain must
   keep its name across restarts). Paste it in the wizard (step 2, *Tailscale auth key*) with a
   machine name (default `company-brain`).

## How the sidecar works

`tailscale/entrypoint.sh` wraps the image's own `containerboot`. It runs as your user id with no
capabilities, userspace networking and a read-only root filesystem, and sees only
`data/tailscale/` (never `secrets/`, the wiki or the portal's data):

| File | Written by | |
| --- | --- | --- |
| `authkey` (0600) | the wizard, step 2 | used only for the first login (`TS_AUTH_ONCE`) |
| `hostname` | the wizard, step 2 | the machine name |
| `state/` | tailscaled | the node's login: survives restarts and upgrades |
| `dnsname` | the sidecar | `<machine>.<tailnet>.ts.net`, once logged in |

1. Without an auth key it waits (`waiting for the Tailscale auth key from the setup wizard` in
   `make logs s=tailscale`); it does not restart in a loop.
2. With a key it logs in with the machine name and applies the serve config (HTTPS on 443,
   Funnel on, proxy to `brain:8080`).
3. It writes the node's DNS name to `dnsname`. The brain checks that file every few seconds and
   (re)starts its public listener with `https://<dnsname>` as OAuth issuer and
   `https://<dnsname>/mcp` as resource. Until then the settings page says *waiting for the
   Tailscale sidecar*. Only a `*.ts.net` name is accepted.
4. When the wizard saves a new key or machine name, the sidecar logs in again. A wrong or used
   key stops the login; it retries every minute and on the next key.

Wizard step 3 then fetches `https://<dnsname>/.well-known/oauth-protected-resource` from the
brain, out over the internet and back in through Funnel. A brand-new name can take a few
minutes to resolve publicly.

## Forwarded headers

The brain and the sidecar share a separate Docker network (`funnel`, `10.254.80.0/29`). The
brain trusts `X-Forwarded-For` only from that subnet (`TRUSTED_PROXY_IPS`), for rate limiting
and the audit log. It never builds a URL from request headers: the issuer and resource URLs come
only from `dnsname` (or `PUBLIC_BASE_URL`, for development).

## Changes and turning it off

- **Another machine name:** change it in the settings; the URL changes, so update the
  connector in your chat apps.
- **Another tailnet or a fresh login:** stop the stack, delete `data/tailscale/state/`, start
  it and enter a new key. A new key alone is ignored while the node is still logged in.
- **Off now:** `docker compose stop tailscale` (or disable Funnel for the machine in the admin
  console). Switching to the `gdrive` mode stops the public listener itself.
