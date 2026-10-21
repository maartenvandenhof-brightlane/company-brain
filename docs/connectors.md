# Connecting a chat app (mode `mcp`)

Every app connects to the same endpoint: `https://<machine>.<tailnet>.ts.net/mcp`, the
**Connector URL** on the brain's settings page (http://localhost:8080/setup). It exists once the
[Tailscale sidecar](tailscale.md) has logged in.

When an app connects, your browser opens the brain's **sign-in page**: user name `owner`
(`OWNER_USERNAME`) and the **admin password** from the setup wizard. Choose the permissions and
approve:

| Permission | Allows |
| --- | --- |
| Read and search (always granted) | `start_session`, `search`, `fetch`, `search_wiki`, `read_concept`, `list_concepts`, … |
| Create and update pages | `write_concept`, `append_to_concept`, `ingest_source`, `link_concepts`, `merge_identity` (a proposal) |
| Accept or reject proposals | `accept_proposal`, `reject_proposal` (identity merges only in the portal) |
| Private pages (never pre-checked) | seeing pages marked `private: true`, and making them visible again (`set_private`) |

Each app gets its own tokens: a 1-hour access token and a rotating 30-day refresh token. Every
change it makes is a git commit authored by that app (`claude.ai`, `chatgpt`, …). Low-trust
writes become proposals you review in the portal ([wiki format](wiki-format.md#writes-trust-and-proposals)).

Menu names in these apps change often. If a step does not match, follow the app's official
guide, linked in each section. Instructions to paste into a claude.ai Project or a Gemini Gem
are in [CHAT_INSTRUCTIONS.md](CHAT_INSTRUCTIONS.md).

## Claude.ai (web, desktop, mobile)

1. **Settings → Connectors → Add custom connector** (Team/Enterprise: an Owner adds it under
   Organization settings → Connectors).
2. Name it `Company Brain`; URL `https://<machine>.<tailnet>.ts.net/mcp`.
3. Authentication: OAuth. **Use Claude's published identity** (Client ID Metadata Document) and
   **Register automatically** (Dynamic Client Registration) both work.
4. Sign in on the page that opens (`owner` + admin password) and approve.
5. In a chat, enable the connector from the **+** menu → Connectors.

Official guide: <https://claude.com/docs/connectors/custom/remote-mcp>

## ChatGPT (developer mode)

1. Settings → **Apps & Connectors** → Advanced settings: turn on **Developer mode**.
2. **Create** a connector: name `Company Brain`, MCP server URL
   `https://<machine>.<tailnet>.ts.net/mcp`, authentication **OAuth**.
3. Sign in and approve.

The `search` and `fetch` tools follow ChatGPT's required shapes, so the brain also works for
deep research. Official guide: <https://developers.openai.com/api/docs/mcp>

## Revoking an app

```sh
make clients                                  # list registered apps and their active grants
make clients ARGS="revoke <client_id>"        # revoke that app's tokens and forget it
make clients ARGS="private chatgpt off"       # private pages per app: on | off | default
```

The portal's **Connected apps** page has the same private-pages toggle. Also remove the
connector in the app itself. To cut all public access at once: `docker compose stop tailscale`.

## Checking the endpoint

```sh
curl -s https://<machine>.<tailnet>.ts.net/.well-known/oauth-protected-resource/mcp
curl -s https://<machine>.<tailnet>.ts.net/.well-known/oauth-authorization-server
curl -si -X POST https://<machine>.<tailnet>.ts.net/mcp | grep -i www-authenticate   # 401
```

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) runs the full OAuth flow:
`npx @modelcontextprotocol/inspector`, transport **Streamable HTTP**, the URL above, then
**Quick OAuth Flow**.
