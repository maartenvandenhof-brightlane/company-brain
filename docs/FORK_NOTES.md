# Fork notes

The Company Brain is a fork of the owner's **Second Brain** (git remote `upstream`, a local
clone at `~/workspace/second_brain`), forked at `370657e`. This page says what was kept, what
is new and what differs, so fixes can still flow from upstream.

## Reused from the Second Brain

- **The wiki core** (`wiki-mcp/src/wiki_mcp/wiki/`): the OKF page model (markdown + YAML
  frontmatter, `config/wiki-types.yaml`), the git layer (one commit per write, `log.md`,
  `index.md`, debounced push), the SQLite FTS5 `SearchIndex`, `ingest_source` and raw sources,
  trust levels and proposals, private pages, lint.
- **The house rules** (`wiki-mcp/seed/AGENTS.md`), reworded for a company.
- **The OAuth MCP server** (`auth/`, `public.py`, `mcp/tools.py`): OAuth 2.1 with CIMD/DCR,
  the login and consent page, rate limits, audit log, the internal listener.
- **The portal's wiki explorer** and proposal review, login and connected apps.

## New

- **Setup wizard and settings page** (`local.py`, `templates/setup.html`, `brain.py`): the mode
  and settings in `data/config.yaml`, secrets in `data/secrets/`, no terminal steps.
- **Supervisor** (`supervisor.py`): adapters per mode, started and restarted without a
  container restart.
- **REST API** (`rest.py`) on the localhost-only listener, sharing MCP's write path.
- **File watcher** (`watcher.py`): hand edits reach the index (and the Drive export).
- **Session context**: `context/soul.md`, `user.md`, `memory.md` and the `start_session` tool
  (`context.py`).
- **Google Drive export** (`gdrive.py`, `google.py`): pages as native Google Docs, plus
  *Start here* and *Index*.
- **Inbox** (`inbox.py`): Google Tasks or Google Keep notes into `raw/inbox/`.
- **Tailscale Funnel sidecar** (`tailscale/`, `compose.yaml`): the public HTTPS URL of the MCP
  connector, driven by files the wizard writes ([tailscale](tailscale.md)).

## Different from upstream

- **Removed services:** LibreChat, LiteLLM, SearXNG, MongoDB, Cloudflare Tunnel and Caddy
  (`edge/`), with their config, the access modes (`UI_ACCESS_MODE`, `MCP_EXPOSURE_MODE` in
  compose) and their docs. The stack is `brain` + `portal` + `tailscale`.
- **Removed scripts:** everything in `scripts/` but `synthetic_wiki.py`: onboarding, access
  configuration, model refresh, secret rotation, smoke tests and **backup/restore** (back up
  the `data/` folder instead).
- **Personal features unregistered, code kept:** assistant dreams, observing chats and personal
  patterns (`dream/`, `mcp/dream_tools.py`, `mcp/pattern_tools.py`, `wiki/patterns.py`) are not
  registered as MCP tools; the portal's ideas, patterns, dreams, assistant, chat import, API
  keys and usage pages are not routed. The code stays so upstream changes still merge.
- **Renames:** the service `wiki-mcp` is `brain`; `WikiStore._reindex` is public `reindex`
  (the watcher calls it); the seed concept page `wiki/concepts/second-brain` is
  `wiki/concepts/company-brain`; `INBOX_ADAPTER` names the inbox (tasks | keep).
- **Smaller changes in shared files:** server instructions (`start_session` instead of
  `get_briefing`), `INDEXED_ROOTS` includes `context`, `ingest_source` takes a subfolder and
  extra frontmatter (inbox), the public listener is an adapter (`McpPublicAdapter`) whose URL
  comes from the sidecar, and the portal's navigation, config and Dockerfile.

## Pulling fixes from upstream

```sh
git fetch upstream
git log --oneline HEAD..upstream/main          # what is new upstream
git cherry-pick <sha>                          # prefer picking fixes one by one
# or: git merge upstream/main (expect the conflicts below)
make test
```

Expect conflicts in: `mcp/tools.py` (instructions, tool registration), `public.py`,
`runtime.py`, `config.py`, `wiki/store.py` (`reindex`, `ingest_source`), `seed/AGENTS.md` and
`seed/README.md`, the portal's `app.py`, `config.py`, `Dockerfile` and templates, and
`compose.yaml`, `Makefile`, `.env.example`, `README.md`, the `uv.lock` files and `docs/`
(deleted pages come back as modify/delete conflicts: keep them deleted). Changes upstream to the
wiki core, search, auth and lint usually apply cleanly. Fixes to the personal features merge
too, but nothing here exercises them beyond their tests.
