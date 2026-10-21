# The wiki: OKF bundle format

The wiki is an [Open Knowledge Format](https://github.com/GoogleCloudPlatform/open-knowledge-format)
v0.2 bundle: a git repository of UTF-8 markdown files with YAML frontmatter. It is readable in
any editor and in Obsidian.

```
index.md            generated table of contents (root carries okf_version: "0.2")
log.md              human-readable change log, newest date first
AGENTS.md           house rules for agents (type: guide)
README.md           what this bundle is (type: guide)
context/            soul.md, user.md, memory.md: what start_session returns (type: context)
raw/                immutable sources (type: source), from ingest_source, REST and the inbox
wiki/<folder>/      concept pages, one folder per type (config/wiki-types.yaml)
wiki/maintenance/   nightly lint reports
proposals/          changes awaiting owner review
```

## Frontmatter

OKF fields, as the spec defines them:

| Key | Notes |
| --- | --- |
| `type` | required; one of the types in `config/wiki-types.yaml` |
| `title`, `description` | required by this wiki (recommended by OKF) |
| `resource` | URI of the underlying thing, if any |
| `tags` | lowercase list |
| `status` | `draft` \| `stable` (default) \| `deprecated` |
| `generated` | `{by, at}`: the agent that last wrote the page, e.g. `claude.ai/wiki-mcp` |
| `verified` | `[{by: human:owner, at}]`, added when you accept a proposal |
| `stale_after` | ISO datetime after which lint flags the page |
| `sources` | `[{id, resource: /raw/….md}]` or URLs; cite in the body with `[^id]` footnotes |
| `private` | extension of this wiki, not OKF: `true` shows the page only to apps with the Private pages permission ([below](#private-pages)) |
| `aliases` | extension: other names the page is known by, searchable like its title ([below](#duplicates-and-aliases)) |
| `canonical` | extension: on a duplicate, the id of the page it is the same as; set only by an accepted identity merge ([below](#duplicates-and-aliases)) |

Extension keys managed by the server: `id` (path without `.md`), `created`, `updated` and `trust`
(`owner` \| `agent` \| `external`). `related` is a list of page ids. `private` (absent or `false`: a
normal page) is set by clients like `status`, and so are `aliases` (a write that does not
mention them keeps them). `canonical` is the server's: a write cannot set it. Other custom keys are
allowed and preserved.

Links are standard markdown with bundle-absolute paths: `[Jane Doe](/wiki/people/jane-doe.md)`.

## Types

`person`, `organization`, `project`, `concept`, `decision`, `meeting`, `howto`, `owner`,
`life-event` and `reference` are writable by agents. `owner` is meant for two pages:
`wiki/owner/profile` for your preferences, background and goals as assistants learn them, and
the private `wiki/owner/personal` for your personal life, relationships, feelings and values.
`person` pages cover who someone is to you, your shared history and how you feel about them.
`life-event` pages (`wiki/life/`, private by default) record a significant event or period:
what happened, who was involved, how you felt and what you took from it. `source`,
`maintenance-report`, `guide` and `proposal` are managed by the server. So are `inference`,
`briefing` and `observation`, kept from the Second Brain's personal features, which nothing in
the Company Brain writes ([FORK_NOTES.md](FORK_NOTES.md)). These managed types are read-only for
agents: `get_wiki_guide` lists them separately
(`read_only_types`, with who writes them) and `write_concept` refuses them. To add a type (for
example `customer`), add it to `config/wiki-types.yaml` with its folder and run `make up`; a
type with `private: true` there creates private pages unless a write says `private: false`.

## Private pages

Pages about your personal life can be marked `private: true`. Only apps you granted the
**Private pages** permission (scope `wiki:private`, never granted by default and never
pre-checked on the consent page) can see them. For every other app a private page does not
exist: it is left out of search, lists, backlinks, `related` and `sources` of other pages, the
graph, lint findings and proposals, and reading it answers the same "no page" error as a
missing page. `index.md` never lists private pages. A change to a private page is logged in
`log.md` as "a private page" without its title or reason (and the reason of any change in the
same commit is withheld), while the git commit keeps the full reason and a `Private: yes`
trailer; `recent_changes` leaves such commits out for apps without the permission, and
`log.md` lines about pages that were marked private later are dropped when read.

- **Who sees them:** the portal, the signed-in admin and apps you granted the permission. The
  REST token does not, unless you add `wiki:private` to `INTERNAL_SCOPES`. They are never
  exported to Google Drive.
- **Writing without the permission:** an app can still create new private pages (write-only),
  but any write, append or link to an existing private page fails as if it did not exist, and
  a `write_concept` to such a path is refused without saying why.
- **Sticky:** a write that does not mention `private` keeps the page's value. Only an app with
  the permission can un-mark a page (`private: false`, or `set_private`). New pages of a type
  with `private: true` in `config/wiki-types.yaml` (such as `life-event`), and
  `wiki/owner/personal`, are private by default.
- **`set_private(path, private, reason)`** marks or un-marks a page or a raw source (needs the
  permission). On a raw source only that frontmatter key changes; the content and its
  `content_sha256` stay as they are. A non-private page cannot link to a private one with
  `link_concepts`.
- **Limits:** private pages are ordinary files in the wiki's git repository, pushed to
  `GIT_REMOTE_URL` when one is set, and included in backups. Links written by hand in a
  non-private page's body are not hidden: the house rules tell agents not to write them.

The *Personal life and private pages* section of `AGENTS.md` tells agents what to file and when
to mark it private.

## Duplicates and aliases

One person or thing has one page. Other names it is known by go in `aliases`, which search
matches like the title. When two pages turn out to be the same, an assistant calls
**`merge_identity(duplicate, canonical, reason)`**: two page ids and a reason, never rewritten
prose (the motivating case: an import created `wiki/people/<your name>` next to
`wiki/owner/profile`, and some apps' moderation blocks tool calls that write about a person's
identity). It is always a proposal, whatever the app's trust, and only you accept it: in the
portal (**Wiki › Proposals**, *Accept merge*). An assistant's `accept_proposal` refuses it.

Accepted, only frontmatter changes, in one commit; git history, bodies and sources stay:

- the duplicate gets `canonical: <canonical id>` and `status: deprecated`, and keeps its text;
- the canonical page gets the duplicate's title in `aliases`, without a trailing parenthetical
  ("Eigenaar (redirect)" gives "Eigenaar") and only when it is not its title or an alias already.

From then on `read_concept`, `fetch` and `get_backlinks` on the duplicate answer with the
canonical page and `redirected_from` (`read_concept` with `follow_canonical: false` reads the
duplicate itself); search never lists the duplicate; links to it are not rewritten and count as
backlinks of the canonical page. Writes to the duplicate are refused with a pointer to the
canonical page. The portal shows the duplicate itself, with a banner linking the canonical page.

Refused: a page merged into itself, a non-concept page, a duplicate that is merged already
(`canonical` set; a deprecated page without it, such as a hand-made redirect page, can be merged),
the owner profile as the duplicate, a canonical page that is itself merged or a duplicate
that others are merged into (no chains), and a private page with a non-private one. A redirect
never shows a private page to an app without the Private pages permission: the duplicate is then
answered as missing too.

The owner is always `wiki/owner/profile`: a new `person` page titled exactly like the profile,
one of its aliases or a generic word for the owner ("owner", "the owner", "eigenaar",
"de eigenaar"), case and spacing aside, is refused, and batch processing files what it learns
about such a person on the profile instead.

## Writes, trust and proposals

- Every write is one git commit authored by the calling app (`claude.ai`, `chatgpt`,
  `local` for the REST token, `admin` for the signed-in admin), with the reason in the message. It also updates `index.md` and `log.md`.
  Writes are serialised by a lock.
- The effective trust of a write is the lowest of the app's trust and the trust of every cited
  source. With `PROPOSALS_FOR=external`, anything derived from `external` sources becomes a proposal.
  Review proposals with `review_proposals` and apply them with `accept_proposal` /
  `reject_proposal` (the owner's `wiki:admin` permission).
- Pages that cite only imported chats (tag `chat-import`) and set no status become drafts with
  the tag `from-chat-import`. The portal lists every page with `status: draft` under *Drafts*
  (`/wiki/drafts`); once checked, press **Mark reviewed** there (it sets `status: stable` and
  removes the tag).

## Facts, superseded lines and inferences

The wiki keeps what you stated apart from what was concluded from it (`wiki_mcp/wiki/facts.py`;
the house rules have a section *Facts and inferences*, which reaches existing wikis at startup).

- A **stated line** is a bullet line on a page that is not an inference, citing a raw source with
  a footnote `[^source-id]`. Its date is its leading `YYYY-MM[-DD]:`, else its source's date.
  Only stated lines are evidence.
- An outdated line is never deleted or reworded. It keeps its text byte for byte and gets
  ` *(superseded YYYY-MM-DD: <reason>)*` appended (the word `superseded` always in English, the
  reason in the page's language); the replacement is a new dated line with its own source.
  Pages are not deleted either: a merged or retired page becomes `status: deprecated` with a
  note and keeps its body.
- **Inferences** (`wiki/inferred/…`, type `inference`) are conclusions the Second Brain's
  dreams drew; nothing in the Company Brain writes them. Lint still checks the rules for any
  that exist, and `bad-superseded` (warning) flags a superseded mark not in the exact form.

## Filing conversations

`AGENTS.md` has one playbook, *Filing a conversation*, for turning a chat into pages. Every path
refers to it: the `file-conversation` MCP prompt, the server instructions and the
`ingest-source` prompt. In short: only file what
you stated or accepted (assistant-only claims are skipped or kept as drafts), date claims and keep
changing facts under a `Timeline` section, record decisions with their options and rationale,
open questions and facts about you, and cite the conversation's source id. A second section,
*Personal life and private pages*, covers the people you talk about, how you feel, and life
events: it says a conversation without a decision can still be worth filing, where each thing
goes, and what to mark private.

Citing is also how processing is tracked. The search index treats the raw sources a page lists in
`sources` as links, so a source's backlinks are the pages written from it, and the
`list_unprocessed_sources` tool lists the raw sources (by default the imported chats, tag
`chat-import`) that no page cites or links to yet. A transcript with nothing worth filing is
linked from `wiki/references/skipped-conversations` so it is not offered again. With
`rerun: true` the tool also lists transcripts whose citing pages were all last updated before the
playbook reached the wiki (`filed_at`), to process them again under its rules. The index is
rebuilt from the files at every start, so older wikis pick this up after an upgrade.

## Editing by hand and Obsidian

Open `data/wiki/` (`BRAIN_WIKI_DIR`) as an Obsidian vault or in VS Code. Search picks up hand
edits within seconds (the file watcher re-indexes them, and in the `gdrive` mode the export
follows); they are committed at the brain's next start ("Record out-of-band changes").

Obsidian writes vault-absolute links *without* a leading slash, and its support for
leading-slash links (`/wiki/...md`, OKF's recommended form) has not been verified here.
If links don't resolve for you, the community plugin *Better Markdown Links* handles them.
Frontmatter, tags and backlinks work normally.
