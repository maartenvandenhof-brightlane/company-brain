"""REST API on the local listener: the brain without MCP (scripts, editors, the Drive adapter).

  GET  /pages/{id}               one page: id, path, frontmatter, body, backlinks; a duplicate
                                 answers with its canonical page and `redirected_from`
  GET  /search?q=&type=&limit=   full-text search: id, title, type, description, snippet
  PUT  /pages/{id}               create or replace a page: {frontmatter, body, reason}. The same
                                 path as MCP write_concept: validation, trust and proposals (a
                                 low-trust caller's write becomes a proposal, answered 202)
  POST /ingest                   store raw source material: {title, content, source_url?,
                                 trust?, private?}, as MCP ingest_source

Who calls is decided by local.py (admin session or bearer token) and passed in as an Actor; a
private page is answered like a missing one unless the actor holds wiki:private. Scope checks
and the audit log are the MCP tools' own (mcp.tools.run_tool). GET /health is in local.py.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from wiki_mcp.audit import AuditLog
from wiki_mcp.mcp.tools import READ, WRITE, ScopeDenied, run_tool
from wiki_mcp.portal_reads import dumps
from wiki_mcp.wiki.actor import Actor
from wiki_mcp.wiki.paths import doc_id
from wiki_mcp.wiki.store import WikiStore

MAX_BODY = 2 * 1024 * 1024  # a page is at most 256 KB, a source 1 MB; JSON escaping adds some

ActorOf = Callable[[Request], Actor | None]


class _JSON(JSONResponse):
    """Frontmatter may hold dates or other YAML scalars: render them as strings."""

    def render(self, content: Any) -> bytes:
        return dumps(content).encode()


def _error(message: str, status: int) -> Response:
    return _JSON({"error": message}, status_code=status, headers={"Cache-Control": "no-store"})


async def _payload(request: Request) -> dict[str, Any]:
    # JSON only: a cross-origin page then needs a CORS preflight, which is never granted.
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise ValueError("send Content-Type: application/json")
    length = request.headers.get("content-length", "")
    if not length.isdigit() or int(length) > MAX_BODY:
        raise ValueError(f"send a JSON body with a Content-Length of at most {MAX_BODY} bytes")
    try:
        data = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("the body must be JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("the body must be a JSON object")
    return data


def build_rest_routes(store: WikiStore, audit: AuditLog, actor_of: ActorOf) -> list[Route]:
    async def call(
        request: Request,
        tool: str,
        scope: str,
        target: str | None,
        fn: Callable[[Actor], Awaitable[Any]],
        *,
        missing: int = 400,
    ) -> Response:
        actor = actor_of(request)
        if actor is None:
            return _error("sign in at /setup or send Authorization: Bearer <token>", 401)
        try:
            result = await run_tool(audit, actor, tool, scope, target, fn)
        except ScopeDenied as exc:
            return _error(str(exc), 403)
        except ToolError as exc:
            return _error(str(exc), missing)
        status = 202 if isinstance(result, dict) and result.get("status") == "proposed" else 200
        return _JSON(result, status_code=status)

    async def get_page(request: Request) -> Response:
        ref = request.path_params["id"]

        async def go(actor: Actor) -> dict[str, Any]:
            rel, doc, _text, redirected = store.read_redirected(ref, actor)
            did = doc_id(rel)
            return {
                "id": did,
                "path": str(rel),
                **({"redirected_from": redirected} if redirected else {}),
                "frontmatter": doc.frontmatter,
                "body": doc.body,
                "backlinks": [r.id for r in store.index.backlinks(did, private=actor.sees_private)],
            }

        # Unknown, unreadable and hidden pages all answer 404, so private pages stay unseen.
        return await call(request, "read_concept", READ, ref, go, missing=404)

    async def search(request: Request) -> Response:
        query = request.query_params.get("q", "")[:500]
        kind = request.query_params.get("type") or None
        try:
            limit = max(1, min(int(request.query_params.get("limit", 10)), 50))
        except ValueError:
            limit = 10

        async def go(actor: Actor) -> dict[str, Any]:
            hits = store.index.search(query, type=kind, limit=limit, private=actor.sees_private)
            return {
                "query": query,
                "results": [
                    {
                        "id": h.row.id,
                        "title": h.row.title,
                        "type": h.row.type,
                        "description": h.row.description,
                        "snippet": h.snippet,
                    }
                    for h in hits
                ],
            }

        return await call(request, "search_wiki", READ, None, go)

    async def put_page(request: Request) -> Response:
        ref = request.path_params["id"]
        try:
            data = await _payload(request)
        except ValueError as exc:
            return _error(str(exc), 400)
        fm, body, reason = data.get("frontmatter"), data.get("body"), data.get("reason")
        if not isinstance(fm, dict) or not isinstance(body, str) or not isinstance(reason, str):
            return _error("expected {frontmatter: object, body: string, reason: string}", 400)

        async def go(actor: Actor) -> dict[str, Any]:
            return (await store.write_concept(ref, fm, body, reason, actor)).as_dict()

        return await call(request, "write_concept", WRITE, ref, go)

    async def ingest(request: Request) -> Response:
        try:
            data = await _payload(request)
        except ValueError as exc:
            return _error(str(exc), 400)
        title, content = data.get("title"), data.get("content")
        url, trust, private = data.get("source_url"), data.get("trust"), data.get("private", False)
        if (
            not isinstance(title, str)
            or not isinstance(content, str)
            or not (url is None or isinstance(url, str))
            or not (trust is None or isinstance(trust, str))
            or not isinstance(private, bool)
        ):
            return _error(
                "expected {title: string, content: string, source_url?: string, trust?: "
                "string, private?: boolean}",
                400,
            )

        async def go(actor: Actor) -> dict[str, Any]:
            result = await store.ingest_source(title, content, url, trust, actor, private=private)
            return result.as_dict()

        return await call(request, "ingest_source", WRITE, url or title, go)

    return [
        Route("/pages/{id:path}", get_page, methods=["GET"]),
        Route("/pages/{id:path}", put_page, methods=["PUT"]),
        Route("/search", search, methods=["GET"]),
        Route("/ingest", ingest, methods=["POST"]),
    ]
