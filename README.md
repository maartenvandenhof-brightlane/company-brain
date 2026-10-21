# Company Brain

A locally running knowledge base for a company: a markdown wiki in git that employees use from the
chat apps they already have (claude.ai, ChatGPT, Gemini). Forked from the owner's Second Brain
(remote `upstream`); the wiki core, search, proposals and the MCP server are the same.

- **One wiki.** Markdown + YAML frontmatter in git
  ([Open Knowledge Format](https://github.com/GoogleCloudPlatform/open-knowledge-format)), in a host
  folder you can also edit in Obsidian or VS Code: search picks up your edits within seconds.
- **Several ways in, chosen in a web wizard.** `mcp`: a custom MCP connector (OAuth 2.1) through
  Tailscale Funnel. `gdrive`: the wiki exported to Google Docs plus an inbox for notes, for chat apps
  that block custom connectors. Or `both`. Or `folder`: the same for Gemini, by hand, where Google
  Cloud is switched off.
- **A REST API and a portal** on localhost: read, search and write pages without MCP; explore the
  wiki (tree, search, graph) and review proposals.

## Quick start

```sh
git clone <this repo> company-brain && cd company-brain
make up            # or: docker compose up -d
```

Then open **http://localhost:8080/setup** (the brain logs this URL while it waits for setup):

1. Choose the admin password. It protects the settings page and is also the owner login for the
   MCP connector and the portal (user name `owner`).
2. Choose the mode, then only that mode's settings, then **Check & start**. The mode is saved in
   `data/config.yaml` and its adapters start right away, without a restart. `/setup` stays the
   settings page: change the mode or settings at any time.

The portal is at **http://localhost:3090**. Nothing needs a terminal prompt or a `.env`; copy
`.env.example` to `.env` only to move ports or folders or to prefill the wizard. `make up` passes
your user id so the files in `./data` stay yours (`docker compose up -d` assumes uid 1000).

**Accounts.** Each brain has exactly one account: user name `owner` and the admin password from
step 1. It signs in to the settings page, the portal and the MCP connector; there is no sign-up
and no way to add users. Every employee runs their own brain on their own machine, with their own
password, wiki and (in `folder` mode) `Brain` folder in their own Google Drive. To try a second
brain on one machine, give it its own data folder and ports, e.g.
`BRAIN_DATA_DIR=./data-test BRAIN_PORT=8086 PORTAL_PORT=3096 docker compose -p brain-test up -d`.

**On a server over SSH.** The wizard and the portal listen on the server's localhost only. Reach
them from your own computer with a tunnel, then open the same localhost links there:
`ssh -L 8080:localhost:8080 -L 3090:localhost:3090 <user>@<server>`. If 8080 or 3090 is already
taken on the server, set `BRAIN_PORT` / `PORTAL_PORT` in `.env` and tunnel those ports instead
(with Google sign-in, the redirect URI then uses the new `BRAIN_PORT`).

Which mode:

| Mode | For | Needs |
| --- | --- | --- |
| `mcp` | Chat apps that allow custom MCP connectors (claude.ai, ChatGPT developer mode): they search, read and write the wiki directly | A free Tailscale account (Funnel gives the public HTTPS URL) |
| `gdrive` | Chat apps that block custom connectors but read Google Drive (Gemini): pages as Google Docs, notes saved through an inbox | A Google account and a Google Cloud OAuth client |
| `both` | Both of the above | Both |
| `folder` | Gemini when the Workspace admin has Google Cloud switched off: pages as Markdown files you upload to Drive, Gemini's new pages and versions brought back by downloading the folder | Nothing but a few minutes of copying files by hand |

Then tell the chat app how to use the brain: copy-paste instructions for a claude.ai Project
or a Gemini Gem, per mode, are in [docs/CHAT_INSTRUCTIONS.md](docs/CHAT_INSTRUCTIONS.md).

## Mode `mcp`: MCP connector over Tailscale Funnel

The brain's MCP endpoint (Streamable HTTP, OAuth 2.1 in front of every route) gets a public
HTTPS URL, `https://<machine>.<tailnet>.ts.net/mcp`, from the `tailscale` sidecar container.
Only that endpoint is public: the wizard, the REST API and the portal stay on localhost
([details](docs/tailscale.md)).

**Tailscale, once** ([admin console](https://login.tailscale.com/admin)):

1. *DNS*: enable **MagicDNS** and **HTTPS Certificates**.
2. *Access controls*: allow Funnel in the tailnet policy file (merge into an existing
   `nodeAttrs`):

   ```json
   "nodeAttrs": [
     { "target": ["autogroup:member"], "attr": ["funnel"] }
   ]
   ```

3. *Settings → Keys → Generate auth key*: **Reusable off**, **Ephemeral off**.
4. In the wizard (step 2), paste the auth key and pick a machine name (default
   `company-brain`). The sidecar logs in within a minute; step 3 checks that the URL answers
   from the internet, and the settings page shows the **Connector URL**.

**claude.ai custom connector:**

1. *Settings → Connectors → Add custom connector* (Team/Enterprise: an Owner adds it under
   Organization settings).
2. Name `Company Brain`, URL `https://<machine>.<tailnet>.ts.net/mcp` (the Connector URL).
3. Connect: the brain's sign-in page opens. Sign in as `owner` with the **admin password**,
   choose the permissions and approve.
4. In a chat, enable the connector from the **+** menu. Every conversation starts with
   `start_session` (the `context/` pages).

ChatGPT and revoking apps: [docs/connectors.md](docs/connectors.md).

## Mode `gdrive`: Google Drive + inbox

The wiki is exported to native Google Docs in a Drive folder (default `Company Brain`, created by
the brain, owner-only: never shared), which Gemini's Google Drive app can find and read. Notes
you save from the chat app land in an inbox the brain reads every few minutes; they arrive in
`data/wiki/raw/inbox/YYYY-MM-DD/` as untrusted sources for review. Nothing flows back from Drive:
the markdown stays the source of truth.

**Google Cloud, once per company** (each company uses its own OAuth client; nothing is shared
with anyone else):

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project with the
   company's Google account.
2. *APIs & Services → Library*: enable the **Google Drive API** and the **Google Tasks API**
   (and the **Google Keep API** for the Keep inbox).
3. *OAuth consent screen*: user type **Internal** (only the company's own accounts; Google then
   never has to verify the app).
4. *Credentials → Create credentials → OAuth client ID*: type **Web application**, authorized
   redirect URI exactly `http://localhost:8080/setup/google/callback` (with your `BRAIN_PORT`;
   the wizard shows the exact URI).
5. In the wizard (step 2), paste the client id and secret or upload the client's JSON file, then
   **Connect Google**: Google asks for access to the files this app creates
   (`drive.file`) and to Google Tasks, and sends you back to the wizard.

**Inbox.** *Google Tasks* (default) works with any Google account: tell your chat app to save by
creating a task titled `[brain] <short title>` with the content in its details (Gemini adds
tasks to your default list; another list can be chosen by name). *Google Keep* only works with
Google Workspace (not consumer Gmail) and needs a Workspace admin: create a service account in
the same project, download its JSON key and upload it in the wizard, enter the user whose notes
are read, and have the admin grant the service account's client id domain-wide delegation for
`https://www.googleapis.com/auth/keep.readonly` (or `.../auth/keep` when notes are deleted after
they are saved; off by default, `KEEP_DELETE_AFTER_INGEST`).

**What is exported.** Every page under `wiki/` that people write (people, projects, decisions,
...), one Doc per page titled `<Type> – <Page title>` with its description as the first line,
text only (images become their alt text, links `Title (wiki path)`). Never exported: private
pages (`private: true` or a type that is private by default), observations, raw sources,
proposals and generated pages (briefings, inferences, reports). Two more Docs are rebuilt on
every sync: `Company Brain – Start here` (the context pages plus how to use and save to the
brain) and `Company Brain – Index` (one line per page). A changed page updates the same Doc;
a removed one is titled `[DEPRECATED] …` and emptied, never deleted. The export runs at start,
a minute after wiki edits and every hour; `/health` and the settings page show the last run and
any errors. Copy-paste instructions for a Gemini Gem or a claude.ai Project are in
[docs/CHAT_INSTRUCTIONS.md](docs/CHAT_INSTRUCTIONS.md).

## Mode `folder`: Google Drive by hand (no Google Cloud)

For companies whose Workspace admin has Google Cloud switched off: no OAuth client, sign-in or
tunnel. The brain keeps `data/drive/Brain/` as a Markdown copy of exactly the pages the `gdrive`
mode exports (one `.md` file per page, in the wiki's folder layout, plus `00 Start here.md` and
`00 Index.md`), updated at start, a minute after wiki edits and every hour. Gemini reads the
`.md` files in the Drive folder `Brain` and saves pages in the folder `Brain inbox`, each named
for where it goes, `brain--<subfolder>--<name>.md` (Gemini cannot reliably choose a folder, so
the name counts, wherever the file ends up): a new page as a new file, an update as
`brain--<subfolder>--<name> v2.md`, `v3`, ... with the whole new page (Gemini cannot edit
files). Every minute the brain imports each zip or folder in
`data/drive/inbox/`: of every page the
highest version, unless it is text the brain exported itself, is written to the wiki like any
other write (validation, secret scan, a git commit per page), but only for pages the export
shares (not private, managed or retired ones). The download then moves to `done/` (or to
`refused/` when it cannot be read); the settings page shows what was created, updated and
refused.

**Once:** in Google Drive, create the folders `Brain` and `Brain inbox` directly under My Drive
(do not share them). Give Gemini the `folder` Gem from
[docs/CHAT_INSTRUCTIONS.md](docs/CHAT_INSTRUCTIONS.md): it reads `Brain` and saves its
`brain--` files in `Brain inbox`.

**Each time:**

1. In: in Google Drive, search for `brain--`, select all results and Download (Drive makes a
   `.zip`; unzipped is fine too), and drop it into `data/drive/inbox`. The zip may hold Word files
   (`.docx`) for the files Gemini made as Google Docs; that is fine. Within a minute the brain
   applies the new pages and versions (the settings page shows them imported); then delete
   those files in Drive.
2. Out: a minute later the brain has exported again: download the fresh `Brain` (the portal's
   Google Drive page, or `data/drive/Brain`), delete the `Brain` folder in Drive and upload the
   new one in its place.

## Mode `both`

Do both setups above in one pass of the wizard: step 2 shows the Tailscale and the Google
settings, step 3 checks all of them. Use the `mcp` instructions in apps that allow custom
connectors and the `gdrive` ones in the others.

## Data

| Path | What |
| --- | --- |
| `data/wiki/` | The wiki, a git repo (`BRAIN_WIKI_DIR` moves it). `context/soul.md`, `user.md`, `memory.md` are what every conversation starts with |
| `data/config.yaml` | The saved mode and settings |
| `data/secrets/` | Generated tokens, the admin password hash and the Google credentials (OAuth client, refresh token, Keep service account key), each 0600. `internal_token` is the REST API's bearer token |
| `data/state/`, `data/portal/` | Search index, OAuth state, audit logs, the Drive mapping (`gdrive.sqlite`) the ingested inbox items (`inbox.sqlite`) and what the folder mode exported (`folder.sqlite`) |
| `data/drive/` | The `folder` mode: `Brain/` (the Markdown export to upload to Drive) and `inbox/` (Gemini's `brain--` files downloaded back from Drive, a zip or folder; imported ones move to `inbox/done/`, unreadable ones to `inbox/refused/`) |
| `data/tailscale/` | The Tailscale sidecar's auth key, machine name, published DNS name and login state; the only folder it sees |

Back up the whole `data/` folder; the wiki also has its full history in git.

## REST API

On `http://localhost:8080` (localhost only), signed in to `/setup` or with
`Authorization: Bearer $(cat data/secrets/internal_token)`:

| Call | |
| --- | --- |
| `GET /health` | Status, setup state, mode, adapters and their details (why one waits, the last Drive sync or inbox poll and its errors); no auth |
| `GET /pages/{id}` | A page: frontmatter, body, backlinks |
| `GET /search?q=&type=&limit=` | Full-text search |
| `PUT /pages/{id}` | Create or replace a page: `{frontmatter, body, reason}`. The same checks as MCP `write_concept`: low-trust writes become proposals (`202`) |
| `POST /ingest` | Store a raw source: `{title, content, source_url?, trust?, private?}` |

Private pages are visible only to the signed-in admin (or a token with `wiki:private` in
`INTERNAL_SCOPES`).

## Development

`make test` runs ruff, mypy and pytest for the brain (`wiki-mcp/`) and the portal in Docker;
`make fmt` formats, `make lock` re-resolves dependencies.

More: [wiki format, types, trust and proposals](docs/wiki-format.md),
[security model](docs/security.md), [connecting chat apps](docs/connectors.md),
[Tailscale Funnel](docs/tailscale.md), [the portal](docs/portal.md), and what differs from the
Second Brain this was forked from: [fork notes](docs/FORK_NOTES.md).
