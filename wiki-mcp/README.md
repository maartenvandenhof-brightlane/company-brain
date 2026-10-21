# wiki-mcp

MCP server for the Second Brain OKF wiki. See the repository root README and `docs/`.

## Scopes and private pages

- OAuth scopes: `wiki:read` (always granted), `wiki:write`, `wiki:admin` (accept/reject
  proposals) and `wiki:private`. The last one lets a client see pages marked `private: true`;
  it is never in the default grant and is never pre-checked on the consent page. The internal
  listener grants `INTERNAL_SCOPES` (default without `wiki:private`); the portal API's actor
  has it.
- Without `wiki:private`, every MCP tool and resource treats private pages as missing (`WikiStore.read(ref, actor)`, `read_visible`; `SearchIndex` reads take `private=` and
  filter in SQL), `index.md` never lists them, `log.md` logs their changes without title or
  reason, and `recent_changes` drops commits with the `Private: yes` trailer or touching a page
  that is private now. New private pages can still be written.
- `private` is sticky on writes; a type with `private: true` in `config/wiki-types.yaml`
  (`life-event`) and `wiki/owner/personal` default to private.
- Tool `set_private(path, private, reason)` marks or un-marks a concept page or raw source; on
  a raw source only that frontmatter key changes. Marking needs only `wiki:write`, on an item
  the caller sees (a private one is not found without `wiki:private`); un-marking needs
  `wiki:private` too. Either way the side effects of a page turning private run (inferences on
  it marked private, its lines dropped from the public briefing).
- The owner toggles private access per app label (`oauth.sqlite` table `private_access`: `on`,
  `off`, or no row = as consented). `OAuthProvider.load_access_token` applies it on every
  request (`effective_scopes`), so it takes effect without reconnecting; stored token scopes,
  and so refreshes, stay as consented. Set it with `wiki-mcp clients private <label>
  on|off|default` (`make clients ARGS="private chatgpt on"`; `clients list` shows the effect and
  its source) or the portal API (`GET /apps`, `POST /apps/private {label, access}`, known labels
  only). Every change is audited (`private_access`, with `by`: cli or portal). No toggle for the
  internal listener: `INTERNAL_SCOPES`.
- `ingest_source` takes `private: true` for personal material.

## Filing conversations

- `AGENTS.md` (seed: `seed/AGENTS.md`) holds the *Filing a conversation* playbook; missing seed
  sections are appended to an existing wiki's `AGENTS.md` at startup.
- Prompt `file-conversation` (optional `source_id`) points to that playbook.
- Tool `list_unprocessed_sources(tag="chat-import", limit=20)`: raw sources no page cites yet,
  one entry per transcript with its size in bytes and its parts. Cited `sources` are indexed as
  links, so `get_backlinks` on a source lists the pages written from it.
  `rerun=true` adds transcripts filed before the playbook reached the wiki (the first commit
  adding its section to `AGENTS.md`), with `filed_at`. The portal API's `GET /processing` returns
  both counts for the Import page.
- `read_concept(path, offset=0, limit=40000)` returns the body in windows of `limit` characters
  (ending at a line break where possible) with `total_chars`, `more` and `next_offset`, so a
  model can page through a long transcript.
- A `write_concept` that cites only imported chats (tag `chat-import`) and sets no `status`
  creates a draft tagged `from-chat-import` for the owner to review; an existing page keeps its
  status.
- Portal API `POST /file` files one batch-processed conversation in a single commit
  (`WikiStore.file_conversation`): new draft pages, lines appended to existing pages (duplicates
  left out), `sources`, `related` and the `from-chat-import` tag added, status kept; or, with
  `skip`, a line on `wiki/references/skipped-conversations`. With `replace` (processing a
  conversation again), the lines citing it on pages still tagged for review, and its skip line, are
  taken out first in the same commit; reviewed pages are only flagged again. `GET /unprocessed`
  lists what is left (`all=1`: every imported chat). A page may carry `"private": true` (new
  page created private, existing page marked private; never un-marked); top-level
  `"private_sources": true` marks the filed sources private in the same commit, and a skip line
  for a private conversation reads `- [Private conversation](/raw/….md)` with no reason.
- Every doc summary in the portal API (`/index`, `/docs`, `/search`, backlinks of `/doc`) carries
  `private`; so do `/doc`, `/unprocessed` entries and `/proposals`. Graph nodes get
  `private: true` when private.

## Review, the owner and dreams

- Portal API `GET /open-loops`: every unchecked `- [ ]` line on stable pages and on
  `wiki/owner/profile` (whatever its status), without superseded ones, with the page, its
  status, whether it is reviewed and the line's date.
- `POST /review {id}` marks a page reviewed (`WikiStore.review_page`): `status: stable`, the
  `from-chat-import` tag removed, the owner's `verified` entry added. Reviewed means exactly
  that: stable and without the tag.
- `POST /owner` is the owner's own writing, trust `owner`: `{action: goals, add, drop}` appends
  `- <goal> (set YYYY-MM-DD)` to `## Goals` of the profile and marks dropped goals superseded
  (never deleted); `{action: statement, text, section}` stores the statement as a raw source
  with trust `owner` and adds a dated line citing it to the section, in one commit.
- `POST /dream/commit`, `/dream/proposal` and `/dream/revert` are the portal's dreams
  (`wiki/dreamops.py`, `WikiStore.dream_commit` and friends, actor `process:dream`). A direct
  commit may only add footnoted sources, replace an exact substring, or write the briefing or a
  dream log; it carries the trailers `Dream: <id>` and `Dream-pass: <pass>`. Everything else
  (append, supersede, related, inferences, deprecating a page) is a proposal with action
  `dream`, which records each page's last commit: accepting it after that page changed is
  refused ("page changed since the dream; run it again"). Every change must leave all bullet
  lines of a page in place (`wiki/facts.py`), except on the regenerated briefing and logs, and
  may only touch reviewed pages. Revert runs `git revert` on each of the dream's commits, newest
  first, one commit each, and rejects its pending proposals; a revert that conflicts on a page
  is aborted and reported.
- `POST /dream/session`, `GET /dream/session?dream=` and `POST /dream/session/close` are an
  assistant dream's session (`dream/assistant.py`, `dream/sessions.py`, state in
  `dreams.sqlite`): the owner starts it on the portal's Dreams page, opening it makes the
  hygiene fixes, and closing it (or its expiry after two hours) writes the dream log. Its
  commits also carry `Dream-client: <client>`. The dream code reads the wiki in-process
  through `dream/local.py`, which builds the same bodies as the portal API's reads
  (`portal_reads.py`).
- What each dream pass reads, its prompts and the checks on its answer are `wiki_mcp.dream`
  (`pages`, `prompts`, `hygiene`, `merge`, `deduction`, `induction`, `inference`,
  `hot_memory`, `log`), shared by the portal's passes (`portal/dreams/`) and the assistant
  dream's MCP tools (`mcp/dream_tools.py`, driven by `dream/passes.py`, scope `wiki:write`):
  `dream_material(dream, pass_name, window)` gives a part of a pass (prompt, material, limits),
  `dream_submit(dream, pass_name, answer, window)` checks the answer and makes the portal's
  proposals (hot memory: its one commit after the last part), `dream_finish(dream)` writes the
  log; prompt `run-dream`. Passes go in order, one submission per part; the session keeps a
  fingerprint of each part's material and refuses an answer when it changed. Without
  `wiki:private` the material is read with `LocalReader(private=False)` and the tools' answers
  pass through `passes.Hidden`, which leaves out private ids, titles and lines.

## The link graph

- Portal API `GET /graph?include=raw,proposals&since=<version>` (read-only): the wiki's pages as nodes (`id`, `title`, `type`, `folder`, `tags`,
  `backlink_count`, `date`; `draft` for pages whose frontmatter has `status: draft`, whatever their tags or trust; for imported chats their
  `conversation`, `part`, `after`, and `unprocessed` from the same rule as `GET /unprocessed`, or
  `skipped` when only the skipped-conversations page cites them) and their links as edges. A link
  to a missing page points to a `ghost` node and is `broken`. Raw sources and proposals only when
  included. `version` changes with every index write; `since` equal to it answers
  `{"version", "unchanged": true}`. Built in one pass from the index (`wiki/graph.py`).

Identity merges consolidate the full page after owner review in the portal. For example,
`merge_identity(duplicate="wiki/projects/ai-agentic-system", canonical="wiki/projects/second-brain-vault", reason="Same project")`
previews facts, timelines, decisions, follow-ups, citations and metadata on Second brain vault.
Exact content duplicates are removed within a section; distinct evidence and conflicting claims
remain unresolved for review. Relative Markdown links are rebased and conflicting citation labels
renamed. `merge_history` records original metadata and both source snapshots/commits. Approval
writes both pages and a retry receipt in one Git commit; page changes invalidate the preview.
Original page edits must already be committed to Git. The old path becomes a minimal redirect.
Use **Show merged pages** for history and **Review content consolidation** to migrate accepted
redirect-only merges; MCP can propose this explicitly with `consolidate_merged=true`.
