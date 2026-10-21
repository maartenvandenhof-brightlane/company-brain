"""JSON API for the portal, mounted on the internal listener at /api/portal/v1.

It has its own bearer token (PORTAL_BEARER_TOKEN), separate from LibreChat's. The portal
is the owner's (behind the owner login), so the token holder writes as the owner where the
owner acts, and as the dream actor for dreams. The writes: POST /import, which can only
create new raw sources from chat exports (never change or delete a file); POST /file, which
files one batch-processed conversation (new draft pages, and lines appended to existing
pages, all tagged for review); the owner's review, goals, statements and decisions on
proposals; and a dream's changes, which wiki-mcp checks itself (wiki/dreamops.py: reviewed
pages only, never raw/, no bullet line removed or reworded). The portal never opens the wiki
volumes itself.

  GET /index                     index.md, the page types and every indexed page
  GET /docs?type=&folder=&tag=&status=&include_merged=&merged_only=   page summaries;
                                 redirects hidden by default, optional history view
  GET /doc?id=                   one file: frontmatter, body, backlinks, outlinks
  GET /search?q=&type=&limit=    full-text search with snippets
  GET /graph?include=raw,proposals&since=   the link graph (wiki.graph.build_graph): nodes,
                                 edges, ghosts for missing pages; raw sources and
                                 proposals only when included. `version` changes with
                                 every write; with since=<version> an unchanged graph is
                                 answered with {"version", "unchanged": true} only
  GET /recent?limit=&since=      git history
  GET /log                       log.md
  GET /proposals                 pending proposals
  GET /unprocessed?rerun=&all=&limit=  imported chats to process (list_unprocessed_sources;
                                 all: every imported chat, to process them all again)
  GET /processing                how many imported chats are unprocessed, or filed before
                                 the current playbook (to re-process)
  GET /renamed?id=               where git history shows a page was renamed to (dream hygiene
                                 repairs a broken link only when there is exactly one)
  GET /open-loops                every unchecked `- [ ]` line on stable pages and the owner
                                 profile: page, its status, whether it is reviewed, the date
  POST /import                   store chat transcripts as new raw sources (one commit)
  POST /file                     file one processed conversation (one commit)
  POST /review {id}              the owner marks a page reviewed: status stable, the
                                 from-chat-import tag removed
  POST /owner                    the owner's own writes on wiki/owner/profile, trust owner:
                                 {action: goals, add: [...], drop: [<exact lines>]} or
                                 {action: statement, text, section}
  POST /dream/commit             a dream pass's direct changes, one commit with a Dream trailer:
                                 {dream, pass, reason, operations}
  POST /dream/proposal           a dream's change as a proposal for the owner, with the base
                                 commit of each page: {dream, pass, summary, reason,
                                 speculative, operations}
  POST /dream/revert {dream}     revert a dream's direct commits and reject its proposals
  POST /dream/session            open an assistant dream's session (dream/assistant.py) and
                                 make its hygiene fixes: {dream, client, started, number,
                                 since_base, kind}; kind "patterns" (the Patterns page's
                                 Reassess in Claude.ai / ChatGPT) opens one for the patterns
                                 pass, without hygiene. Refused when the dream had one or
                                 another is open. Answers its state, as GET does
  GET /dream/session?dream=      a session's state: kind, status (open, expired, finished), client,
                                 opened, expires, finished, ended, passes_done, passes_to_do,
                                 commits, proposals, flagged, skipped, log_page. Only reads:
                                 an expired session is closed by POST /dream/session/close
  POST /dream/session/close {dream}   write the dream log and finish the session: an open one
                                 as "closed by the owner", an expired one as "expired";
                                 answers its state
  POST /proposal {id, decision: accept|reject|refresh, reason?, review_token?}   the owner accepts or rejects a
                                 pending proposal; identity merges (merge_identity) are
                                 accepted only here with the preview review_token. Both snapshots must match;
                                 refresh explicitly renews review. Never accepted over MCP
  POST /merge-review {duplicate, canonical}   preview content consolidation for a previously
                                 accepted redirect-only merge; still requires owner approval
  GET /apps                      the connected OAuth apps by label (kind, created, last_used,
                                 active grants, consented with wiki:private, the owner's
                                 toggle, effective private access), and the internal client
                                 (LibreChat: INTERNAL_SCOPES, with the same toggle)
  POST /apps/private {label, access}   the owner's private-access toggle for one app or LibreChat:
                                 on | off | default (as consented)
  GET /observations/pending?limit=&outdated=   imported chats without an observation page
                                 (with outdated=<extractor>: also those another extractor
                                 observed), oldest first: {total, sources: [{id, parts, ...}]}
  POST /observations {source, extractor, classification, observations}   the observation page
                                 of one imported chat, checked against its transcript
                                 (wiki/observations.py), in one commit; {status: exists} when
                                 that extractor wrote it already
  GET /patterns/material         the patterns pass's material (dream/patterns.py): the sampled
                                 observations, their refs, the coverage, the existing patterns
  POST /patterns/feedback {id, verdict, note}   the owner's verdict on a personal pattern
                                 (agree, dispute, correct, unsure), one commit
  GET /drive/start               the folder mode's export (data/drive/Brain): the text of
                                 "00 Start here.md" and "00 Index.md" {start_here, index}
  GET /drive/brain.zip           the whole export as a zip with a top-level Brain/ folder, to
                                 upload to Google Drive by hand. Both 404 without an export

An import item is {key, title, content, part, parts, messages, last_message_at, after}; the
last three let a later import store only the messages added since (a continuation, `after`
= messages imported before). Existing items are answered with what was imported so far
(`imported_messages`, `last_message_at`) so the portal can tell whether a chat continued.

The portal is the owner's own UI and sees private pages too: every doc summary carries
`private` (as do /doc, /unprocessed entries, /proposals, /open-loops entries and /recent
commits, which are private when they touched a private page; graph nodes get `private: true`).
In POST /file a page may carry `"private": true` (created private, or an existing page marked
private; never un-marked), and `"private_sources": true` marks the filed sources private.
"""

from __future__ import annotations

import asyncio
import hmac
import json
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from wiki_mcp.audit import AuditLog
from wiki_mcp.auth.provider import PRIVATE_ACCESS, OAuthProvider
from wiki_mcp.config import Trust
from wiki_mcp.dream import assistant
from wiki_mcp.dream import patterns as patterns_pass
from wiki_mcp.dream.sessions import DreamSessions
from wiki_mcp.portal_reads import (
    MAX_DOCS,
    MAX_RECENT,
    doc_body,
    docs_body,
    dumps,
    graph_body,
    index_body,
    open_loops_body,
    recent_body,
    renamed_body,
)
from wiki_mcp.wiki.actor import PRIVATE_SCOPE, Actor
from wiki_mcp.wiki.paths import PathError
from wiki_mcp.wiki.store import (
    MAX_FILING_PAGES,
    MAX_LISTED_SOURCES,
    ChatImport,
    ChatSource,
    FilingPage,
    FilingSection,
    WikiError,
    WikiStore,
)

# The writes: new raw sources from chat exports, and pages filed from them, at trust
# "agent" (half of every transcript is assistant text). Git history shows the portal as
# the author. It is the owner's own UI and its batch processing files personal chats, so it
# sees and writes private pages.
PORTAL_ACTOR = Actor("portal", Trust.AGENT, frozenset({"wiki:read", "wiki:write", PRIVATE_SCOPE}))
# The owner acting in the portal: reviews, goals, statements, reverting a dream.
OWNER_ACTOR = Actor(
    "portal",
    Trust.OWNER,
    frozenset({"wiki:read", "wiki:write", "wiki:admin", PRIVATE_SCOPE}),
    human="owner",
)
MAX_IMPORT_BYTES = 16 * 1024 * 1024
MAX_IMPORT_ITEMS = 500
WRITE_ROUTES = (
    "/import",
    "/file",
    "/review",
    "/owner",
    "/dream/commit",
    "/dream/proposal",
    "/dream/revert",
    "/dream/session",
    "/dream/session/close",
    "/proposal",
    "/merge-review",
    "/apps/private",
    "/observations",
    "/patterns/feedback",
)
MAX_PENDING = 10_000


class _JSON(JSONResponse):
    """Frontmatter may hold dates or other YAML scalars: render them as strings."""

    def render(self, content: Any) -> bytes:
        return dumps(content).encode()


def _int(request: Request, name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(request.query_params.get(name, default))
    except ValueError:
        value = default
    return max(lo, min(value, hi))


def _opt(request: Request, name: str, max_len: int = 200) -> str | None:
    value = request.query_params.get(name, "").strip()
    return value[:max_len] or None


def _error(message: str, status: int) -> Response:
    return _JSON({"error": message}, status_code=status, headers={"Cache-Control": "no-store"})


async def _body(request: Request) -> bytes | Response:
    """The request body, at most MAX_IMPORT_BYTES (else an error response)."""
    length = request.headers.get("content-length", "")
    if not length.isdigit() or int(length) > MAX_IMPORT_BYTES:
        return _error(f"send a Content-Length of at most {MAX_IMPORT_BYTES} bytes", 413)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_IMPORT_BYTES:
            return _error("request body too large", 413)
    return bytes(body)


def _chat_items(payload: Any) -> list[ChatSource]:
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
        raise WikiError('expected {"sources": [...]}')
    raw = payload["sources"]
    if not 1 <= len(raw) <= MAX_IMPORT_ITEMS:
        raise WikiError(f"send 1 to {MAX_IMPORT_ITEMS} sources per request")
    items = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise WikiError("each source must be an object")
        key, title, content = entry.get("key"), entry.get("title"), entry.get("content")
        part, parts = entry.get("part", 1), entry.get("parts", 1)
        messages, after = entry.get("messages"), entry.get("after", 0)
        last = entry.get("last_message_at")
        if not (isinstance(key, str) and isinstance(title, str) and isinstance(content, str)):
            raise WikiError("each source needs string 'key', 'title' and 'content'")
        if not all(_is_int(v) for v in (part, parts, after)):
            raise WikiError("'part', 'parts' and 'after' must be integers")
        if not (messages is None or _is_int(messages)) or not (
            last is None or isinstance(last, str)
        ):
            raise WikiError("'messages' must be an integer and 'last_message_at' a string")
        items.append(ChatSource(key, title, content, part, parts, messages, last, after))
    return items


def _strings(value: Any, what: str, most: int) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > most
        or not all(isinstance(v, str) for v in value)
    ):
        raise WikiError(f"'{what}' must be a list of at most {most} strings")
    return tuple(value)


def _filing(payload: Any) -> tuple[list[str], list[FilingPage], str, str | None]:
    if not isinstance(payload, dict):
        raise WikiError("expected a JSON object")
    sources = list(_strings(payload.get("sources"), "sources", 100))
    reason, skip = payload.get("reason"), payload.get("skip")
    if not isinstance(reason, str) or not (skip is None or isinstance(skip, str)):
        raise WikiError("'reason' must be a string and 'skip' a string or null")
    for key in ("replace", "private_sources"):
        if not isinstance(payload.get(key, False), bool):
            raise WikiError(f"'{key}' must be true or false")
    pages = []
    raw_pages = payload.get("pages", [])
    if not isinstance(raw_pages, list) or len(raw_pages) > MAX_FILING_PAGES:
        raise WikiError(f"'pages' must be a list of at most {MAX_FILING_PAGES} pages")
    for entry in raw_pages:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise WikiError("each page needs a string 'path'")
        sections = entry.get("sections", [])
        if not isinstance(sections, list) or len(sections) > 30:
            raise WikiError("'sections' must be a list of at most 30 sections")
        parsed = []
        for s in sections:
            if not isinstance(s, dict) or not isinstance(s.get("heading"), str):
                raise WikiError("each section needs a string 'heading' and 'lines'")
            parsed.append(FilingSection(s["heading"], _strings(s.get("lines"), "lines", 400)))
        fields = {k: entry.get(k) for k in ("type", "title", "description")}
        if not all(v is None or isinstance(v, str) for v in fields.values()):
            raise WikiError("'type', 'title' and 'description' must be strings")
        if not isinstance(entry.get("private", False), bool):
            raise WikiError("'private' must be true or false")
        pages.append(
            FilingPage(
                entry["path"],
                tuple(parsed),
                tags=_strings(entry.get("tags", []), "tags", 20),
                related=_strings(entry.get("related", []), "related", 50),
                private=entry.get("private") is True,
                **fields,
            )
        )
    return sources, pages, reason, skip


def _str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise WikiError(f"'{key}' must be a string")
    return value


def _dream(payload: dict[str, Any]) -> int:
    value = payload.get("dream")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise WikiError("'dream' must be the dream's id, a positive integer")
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _item(item: ChatSource, **extra: Any) -> dict[str, Any]:
    return {"key": item.key, "part": item.part, "after": item.after, **extra}


def _existing(item: ChatSource, result: ChatImport) -> dict[str, Any]:
    state = result.state.get(item.key)
    return _item(
        item,
        imported_messages=state.messages if state else None,
        last_message_at=state.last_message_at if state else None,
    )


def build_portal_api(
    store: WikiStore,
    audit: AuditLog | None = None,
    *,
    oauth: OAuthProvider | None = None,
    internal: tuple[str, bool] | None = None,
    sessions: DreamSessions | None = None,
    drive_dir: Path | None = None,
) -> Starlette:
    """`oauth` holds the connected apps (None: no OAuth state); `internal` is the internal
    listener's client name and whether INTERNAL_SCOPES include wiki:private; `sessions` the
    assistant dreams' sessions (None: not available); `drive_dir` the folder mode's
    (None: no export)."""
    index = store.index

    async def get_index(_: Request) -> Response:
        return _JSON(index_body(store))

    async def get_docs(request: Request) -> Response:
        body = docs_body(
            store,
            type=_opt(request, "type"),
            tag=_opt(request, "tag"),
            folder=_opt(request, "folder"),
            status=_opt(request, "status"),
            limit=_int(request, "limit", MAX_DOCS, 1, MAX_DOCS),
            include_merged=request.query_params.get("include_merged") == "true",
            merged_only=request.query_params.get("merged_only") == "true",
        )
        return _JSON(body)

    async def get_doc(request: Request) -> Response:
        try:
            return _JSON(doc_body(store, request.query_params.get("id", "")))
        except WikiError as exc:
            return _error(str(exc), 404)

    async def get_search(request: Request) -> Response:
        query = request.query_params.get("q", "")[:500]
        hits = index.search(
            query, type=_opt(request, "type"), limit=_int(request, "limit", 20, 1, 50)
        )
        return _JSON(
            {"results": [{**h.row.summary(), "snippet": h.snippet} for h in hits], "query": query}
        )

    async def get_graph(request: Request) -> Response:
        include = {p.strip() for p in request.query_params.get("include", "").split(",")}
        return _JSON(graph_body(store, include, request.query_params.get("since")))

    async def get_recent(request: Request) -> Response:
        try:
            body = recent_body(
                store, _opt(request, "since", 40), _int(request, "limit", 50, 1, MAX_RECENT)
            )
        except WikiError as exc:
            return _error(str(exc), 400)
        return _JSON(body)

    async def get_log(_: Request) -> Response:
        return _JSON({"markdown": store.read_text_file("log.md")})

    async def get_proposals(_: Request) -> Response:
        return _JSON({"proposals": store.list_proposals()})

    async def get_unprocessed(request: Request) -> Response:
        return _JSON(
            store.unprocessed_sources(
                _opt(request, "tag") or "chat-import",
                _int(request, "limit", 200, 1, MAX_LISTED_SOURCES),
                rerun=request.query_params.get("rerun") in ("1", "true"),
                everything=request.query_params.get("all") in ("1", "true"),
            )
        )

    async def get_processing(_: Request) -> Response:
        new = store.unprocessed_sources(limit=1)["total"]
        rerun = store.unprocessed_sources(limit=1, rerun=True)
        return _JSON(
            {
                "unprocessed": new,
                "imported": store.unprocessed_sources(limit=1, everything=True)["total"],
                "filed_before_playbook": rerun["total"] - new,
                "playbook_since": rerun["playbook_since"],
            }
        )

    async def post_file(request: Request) -> Response:
        body = await _body(request)
        if isinstance(body, Response):
            return body
        try:
            payload = json.loads(body)
            sources, pages, reason, skip = _filing(payload)
            replace = payload.get("replace") is True  # a dict: checked by _filing
            private_sources = payload.get("private_sources") is True
            result = await store.file_conversation(
                sources,
                pages,
                reason,
                PORTAL_ACTOR,
                skip=skip,
                replace=replace,
                private_sources=private_sources,
            )
        except (ValueError, RecursionError) as exc:
            return _error(str(exc) if isinstance(exc, WikiError) else "invalid JSON", 400)
        if audit is not None:
            audit.event(
                "portal_file",
                client="portal",
                ip=(request.client.host if request.client else "?"),
                sources=len(sources),
                created=len(result.created),
                updated=len(result.updated),
                proposed=len(result.proposed),
                skipped=skip is not None,
                replaced=replace,
                private=private_sources or any(p.private for p in pages),
                commit=(result.commit or "")[:12],
            )
        return _JSON(
            {
                "commit": result.commit,
                "created": result.created,
                "updated": result.updated,
                "unchanged": result.unchanged,
                "proposed": result.proposed,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def post_import(request: Request) -> Response:
        body = await _body(request)
        if isinstance(body, Response):
            return body
        try:
            payload = json.loads(body)
            items = _chat_items(payload)
        except (ValueError, RecursionError) as exc:
            message = str(exc) if isinstance(exc, WikiError) else "invalid JSON"
            return _error(message, 400)
        dry_run = payload.get("dry_run") is True
        try:
            result = await store.import_chats(items, PORTAL_ACTOR, dry_run=dry_run)
        except WikiError as exc:
            return _error(str(exc), 400)
        if audit is not None and not dry_run:
            audit.event(
                "portal_import",
                client="portal",
                ip=(request.client.host if request.client else "?"),
                created=len(result.created),
                existing=len(result.existing),
                rejected=len(result.rejected),
                commit=(result.commit or "")[:12],
            )
        return _JSON(
            {
                "dry_run": dry_run,
                "commit": result.commit,
                "created": [_item(i, id=did) for i, did in result.created],
                "existing": [_existing(i, result) for i in result.existing],
                "rejected": [_item(i, error=why) for i, why in result.rejected],
            },
            headers={"Cache-Control": "no-store"},
        )

    async def get_renamed(request: Request) -> Response:
        try:
            return _JSON(await renamed_body(store, request.query_params.get("id", "")))
        except PathError as exc:
            return _error(str(exc), 400)

    async def get_open_loops(_: Request) -> Response:
        return _JSON(open_loops_body(store))

    async def _json_write(request: Request, event: str | None, write: Any) -> Response:
        """Read a JSON body, run `write(payload)`, answer its result; 400 on a refusal."""
        body = await _body(request)
        if isinstance(body, Response):
            return body
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise WikiError("expected a JSON object")
            result = await write(payload)
        except (ValueError, RecursionError) as exc:
            return _error(str(exc) if isinstance(exc, WikiError) else "invalid JSON", 400)
        if audit is not None and event is not None:
            audit.event(
                event,
                client="portal",
                ip=(request.client.host if request.client else "?"),
                outcome="ok",
            )
        return _JSON(result, headers={"Cache-Control": "no-store"})

    async def post_review(request: Request) -> Response:
        async def write(p: dict[str, Any]) -> dict[str, Any]:
            return (await store.review_page(_str(p, "id"), OWNER_ACTOR)).as_dict()

        return await _json_write(request, "portal_review", write)

    async def post_owner(request: Request) -> Response:
        async def write(p: dict[str, Any]) -> dict[str, Any]:
            if p.get("action") == "goals":
                add = list(_strings(p.get("add", []), "add", 30))
                drop = list(_strings(p.get("drop", []), "drop", 50))
                return (await store.owner_goals(add, drop, OWNER_ACTOR)).as_dict()
            if p.get("action") == "statement":
                section = p.get("section") or "Facts"
                if not isinstance(section, str):
                    raise WikiError("'section' must be a string")
                result = await store.owner_statement(_str(p, "text"), section, OWNER_ACTOR)
                return result.as_dict()
            raise WikiError("'action' must be goals or statement")

        return await _json_write(request, "portal_owner", write)

    async def post_dream_commit(request: Request) -> Response:
        async def write(p: dict[str, Any]) -> dict[str, Any]:
            sha, pages = await store.dream_commit(
                _dream(p), _str(p, "pass"), _str(p, "reason"), p.get("operations")
            )
            return {"commit": sha, "pages": pages}

        return await _json_write(request, "portal_dream_commit", write)

    async def post_dream_proposal(request: Request) -> Response:
        async def write(p: dict[str, Any]) -> dict[str, Any]:
            result = await store.dream_proposal(
                _dream(p),
                _str(p, "pass"),
                str(p.get("summary") or ""),
                _str(p, "reason"),
                p.get("operations"),
                speculative=p.get("speculative") is True,
            )
            return result.as_dict()

        return await _json_write(request, "portal_dream_proposal", write)

    async def post_dream_revert(request: Request) -> Response:
        async def write(p: dict[str, Any]) -> dict[str, Any]:
            return await store.revert_dream(_dream(p), OWNER_ACTOR)

        return await _json_write(request, "portal_dream_revert", write)

    def _sessions() -> DreamSessions:
        if sessions is None:
            raise WikiError("assistant dreams are not available")
        return sessions

    async def post_dream_session(request: Request) -> Response:
        async def write(p: dict[str, Any]) -> dict[str, Any]:
            since = p.get("since_base")
            session = await assistant.open_session(
                _sessions(),
                store,
                dream=_dream(p),
                client=_str(p, "client"),
                started=_str(p, "started"),
                number=p.get("number", 0),  # checked with the others by sessions.create
                since_base=since if since is None else str(since),
                kind=str(p.get("kind") or "dream"),
            )
            return _sessions().state(session)

        return await _json_write(request, "portal_dream_session", write)

    async def get_dream_session(request: Request) -> Response:
        value = request.query_params.get("dream", "")
        if not value.isdigit():
            return _error("'dream' must be the dream's id, a positive integer", 400)
        if sessions is None or (session := sessions.get(int(value))) is None:
            return _error(f"dream {value} has no session", 404)
        return _JSON(sessions.state(session), headers={"Cache-Control": "no-store"})

    async def post_dream_session_close(request: Request) -> Response:
        async def write(p: dict[str, Any]) -> dict[str, Any]:
            dream, live = _dream(p), _sessions()
            session = live.get(dream)
            if session is None:
                raise WikiError(f"dream {dream} has no session")
            status = live.state(session)["status"]
            ended = "expired" if status == "expired" else "closed by the owner"
            return live.state(await assistant.close(live, store, dream, ended))

        return await _json_write(request, "portal_dream_session_close", write)

    async def post_proposal(request: Request) -> Response:
        async def write(p: dict[str, Any]) -> dict[str, Any]:
            pid, decision = _str(p, "id"), p.get("decision")
            if decision == "refresh":
                return (await store.refresh_identity_preview(pid, OWNER_ACTOR)).as_dict()
            if decision not in ("accept", "reject"):
                raise WikiError("'decision' must be accept or reject")
            reason = p.get("reason") or f"{decision.capitalize()}ed by the owner in the portal"
            if not isinstance(reason, str):
                raise WikiError("'reason' must be a string")
            if decision == "accept":
                token = p.get("review_token")
                if token is not None and not isinstance(token, str):
                    raise WikiError("review_token must be a string")
                return (
                    await store.accept_proposal(
                        pid, reason, OWNER_ACTOR, review_token=token, require_review_token=True
                    )
                ).as_dict()
            return (await store.reject_proposal(pid, reason, OWNER_ACTOR)).as_dict()

        return await _json_write(request, "portal_proposal", write)

    async def post_merge_review(request: Request) -> Response:
        async def write(p: dict[str, Any]) -> dict[str, Any]:
            result = await store.propose_identity_merge(
                _str(p, "duplicate"),
                _str(p, "canonical"),
                "Review consolidation of a previously merged page",
                OWNER_ACTOR,
                consolidate_merged=True,
            )
            return result.as_dict()

        return await _json_write(request, "portal_merge_review", write)

    async def get_apps(_: Request) -> Response:
        name, configured = internal or ("", False)
        toggle = oauth.private_toggle(name) if oauth is not None and internal else None
        private = toggle == "on" or (toggle is None and configured)
        return _JSON(
            {
                "apps": oauth.apps() if oauth is not None else [],
                "internal": (
                    {"label": name, "private": private, "toggle": toggle, "consented": configured}
                    if internal
                    else None
                ),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def post_app_private(request: Request) -> Response:
        async def write(p: dict[str, Any]) -> dict[str, Any]:
            label, access = _str(p, "label"), _str(p, "access")
            if access not in PRIVATE_ACCESS:
                raise WikiError("'access' must be on, off or default")
            if oauth is None:
                raise WikiError(f"no connected app is called {label!r}")
            ip = request.client.host if request.client else "?"
            try:
                return oauth.set_private_access(label, access, by="portal", ip=ip)
            except ValueError as exc:
                raise WikiError(str(exc)) from exc

        # set_private_access audits the change itself (with who made it)
        return await _json_write(request, None, write)

    async def get_observations_pending(request: Request) -> Response:
        body = store.observations_pending(
            _int(request, "limit", 100, 1, MAX_PENDING), outdated=_opt(request, "outdated", 40)
        )
        return _JSON(body, headers={"Cache-Control": "no-store"})

    async def post_observations(request: Request) -> Response:
        body = await _body(request)
        if isinstance(body, Response):
            return body
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise WikiError("expected a JSON object")
            result = await store.write_observations(
                payload.get("source"), payload.get("extractor"), payload, PORTAL_ACTOR
            )
        except (ValueError, RecursionError) as exc:
            return _error(str(exc) if isinstance(exc, WikiError) else "invalid JSON", 400)
        if audit is not None:  # counts only: nothing of what the chat says
            audit.event(
                "portal_observations",
                client="portal",
                ip=(request.client.host if request.client else "?"),
                outcome=result["status"],
                kept=result.get("observations", 0),
                dropped=len(result.get("dropped", [])),
            )
        return _JSON(result, headers={"Cache-Control": "no-store"})

    async def get_patterns_material(_: Request) -> Response:
        found = patterns_pass.material(store.pattern_evidence(), store.pattern_pages())
        return _JSON(found.as_dict(), headers={"Cache-Control": "no-store"})

    async def post_pattern_feedback(request: Request) -> Response:
        async def write(p: dict[str, Any]) -> dict[str, Any]:
            note = p.get("note") or ""
            return (
                await store.pattern_feedback(_str(p, "id"), _str(p, "verdict"), note, OWNER_ACTOR)
            ).as_dict()

        return await _json_write(request, "portal_pattern_feedback", write)

    def _brain() -> Path | None:
        """Brain/ when the folder mode has exported it."""
        from wiki_mcp.folder import START_HERE  # folder imports runtime, which imports this

        root = drive_dir / "Brain" if drive_dir is not None else None
        return root if root is not None and (root / START_HERE).is_file() else None

    async def get_drive_start(_: Request) -> Response:
        from wiki_mcp.folder import INDEX, START_HERE

        root = _brain()
        if root is None:
            return _error("no Google Drive folder export", 404)
        return _JSON(
            {
                "start_here": (root / START_HERE).read_text(encoding="utf-8"),
                "index": (root / INDEX).read_text(encoding="utf-8"),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def get_drive_zip(_: Request) -> Response:
        from wiki_mcp.folder import export_zip

        root = _brain()
        if root is None:
            return _error("no Google Drive folder export", 404)
        data = await asyncio.to_thread(export_zip, root)
        return Response(data, media_type="application/zip", headers={"Cache-Control": "no-store"})

    return Starlette(
        routes=[
            Route("/index", get_index),
            Route("/docs", get_docs),
            Route("/doc", get_doc),
            Route("/search", get_search),
            Route("/graph", get_graph),
            Route("/recent", get_recent),
            Route("/log", get_log),
            Route("/proposals", get_proposals),
            Route("/processing", get_processing),
            Route("/unprocessed", get_unprocessed),
            Route("/import", post_import, methods=["POST"]),
            Route("/file", post_file, methods=["POST"]),
            Route("/open-loops", get_open_loops),
            Route("/renamed", get_renamed),
            Route("/review", post_review, methods=["POST"]),
            Route("/owner", post_owner, methods=["POST"]),
            Route("/dream/commit", post_dream_commit, methods=["POST"]),
            Route("/dream/proposal", post_dream_proposal, methods=["POST"]),
            Route("/dream/revert", post_dream_revert, methods=["POST"]),
            Route("/dream/session", get_dream_session),
            Route("/dream/session", post_dream_session, methods=["POST"]),
            Route("/dream/session/close", post_dream_session_close, methods=["POST"]),
            Route("/proposal", post_proposal, methods=["POST"]),
            Route("/merge-review", post_merge_review, methods=["POST"]),
            Route("/apps", get_apps),
            Route("/apps/private", post_app_private, methods=["POST"]),
            Route("/observations/pending", get_observations_pending),
            Route("/observations", post_observations, methods=["POST"]),
            Route("/patterns/material", get_patterns_material),
            Route("/patterns/feedback", post_pattern_feedback, methods=["POST"]),
            Route("/drive/start", get_drive_start),
            Route("/drive/brain.zip", get_drive_zip),
        ]
    )


class PortalTokenMiddleware:
    """Bearer check for the portal API. GET/HEAD, plus POST on the WRITE_ROUTES only; the
    LibreChat token is not accepted."""

    def __init__(self, app: ASGIApp, *, token: str, audit: AuditLog) -> None:
        self.app = app
        self.token = token.encode()
        self.audit = audit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = ""
        for key, value in scope.get("headers", []):
            if key == b"authorization":
                header = value.decode("latin-1")
                break
        presented = header[7:].strip().encode() if header.lower().startswith("bearer ") else b""
        if not self.token or not presented or not hmac.compare_digest(presented, self.token):
            ip = (scope.get("client") or ("?", 0))[0]
            self.audit.event("internal_auth", client="portal", ip=ip, outcome="denied")
            await _error("invalid_token", 401)(scope, receive, send)
            return
        method = scope.get("method")
        route = str(scope.get("path", "")).removeprefix(str(scope.get("root_path", "")))
        if method not in ("GET", "HEAD") and not (method == "POST" and route in WRITE_ROUTES):
            await _error("read-only API", 405)(scope, receive, send)
            return
        await self.app(scope, receive, send)
