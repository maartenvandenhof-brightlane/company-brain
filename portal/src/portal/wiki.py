"""Wiki pages: index, type folders, pages with frontmatter and backlinks, search,
log.md, recent changes, pending proposals and the link graph. Read-only.

  GET /wiki                  index.md rendered, types, shortcuts
  GET /wiki/type/{type}      pages of one type
  GET /wiki/tag/{tag}        pages with one tag
  GET /wiki/page/{id}        one page (wiki/, raw/, proposals/ or a root guide)
  GET /wiki/search?q=&type=  full-text search (wiki-mcp's index)
  GET /wiki/log              log.md
  GET /wiki/recent           git history
  GET /wiki/proposals        proposals waiting for review (changes not in the wiki yet)
  GET /wiki/merged           optional merged-page history with legacy consolidation review
  POST /wiki/merged/review   propose content consolidation of a redirect-only merge
  GET /wiki/drafts           pages with status: draft (in the wiki, to be checked)
  GET /wiki/graph?page=      the link graph: a list of pages and their links, which the
                             workspace script turns into the graph view
  POST /wiki/review          the owner marks a page reviewed (status stable, the
                             from-chat-import tag removed), from the drafts list or the page
  POST /wiki/proposals/decide  the owner accepts or rejects an identity merge (duplicate ->
                             canonical, proposed by an assistant with merge_identity), which
                             only the owner may accept, here

JSON for the workspace script (static/js/workspace/), owner session only, same-origin only
(Sec-Fetch-Site), never cached by the browser; a request with X-Portal-Background: 1 (an
automatic refresh) does not count as activity for the session's idle timeout:

  GET /wiki/api/graph?raw=&proposals=&v=     nodes and edges (portal.workspace.graph_variant);
                             {"version", "unchanged": true} when v is still current
  GET /wiki/api/tree?folder=&q=&only=&offset=&limit=   the explorer: one folder level, or a
                             flat list matching q / only=drafts|unprocessed, a page at a time
  GET /wiki/api/fragment/{id}  a page as the full page renders it: {title, type, html (the
                             article), info_html (backlinks, links, properties), backlinks,
                             outlinks, frontmatter, draft, raw, …}
  GET /wiki/api/view?path=   any other page above (/wiki/recent, /wiki/type/person…), the same
                             way; {"redirect": url} where the page would redirect
  GET /wiki/api/mentions/{id}  unlinked mentions of a page (search for its title, minus pages
                             that link to it) and snippets for its backlinks
"""

from __future__ import annotations

import html
import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Match, Route
from wiki_mcp.wiki.links import resolve_link
from wiki_mcp.wiki.okf import DocumentError, canonical_of, parse_document
from wiki_mcp.wiki.paths import PathError, normalize_ref

from portal.render import Link, LinkResolver, render_markdown, render_snippet
from portal.sessions import Session
from portal.web import Portal, owner
from portal.wikiclient import NotFound, WikiClient, WikiUnavailable
from portal.workspace import Tree, encode, graph_variant, plain_snippet, unlinked_mentions

log = logging.getLogger(__name__)

SPECIAL = {"index": "/wiki", "log": "/wiki/log"}
DRAFTS = "/wiki/drafts"
REVIEW_TAG = "from-chat-import"  # stable pages with it are not reviewed yet (dreams skip them)
FRAGMENT = "portal.fragment"  # request scope key: render the page's blocks as JSON
GRAPH_LIST_PAGE = 200  # pages per page of the graph's list view
NEARLY_EMPTY = 10  # fewer wiki pages than this: the graph page points to importing chats
FRAGMENT_BLOCKS = ("title", "content", "sidebar_right")


class _JSON(JSONResponse):
    """Frontmatter may hold dates: render them as strings. Never cached (the default for
    portal responses; see security.GuardMiddleware)."""

    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=False, default=str, separators=(",", ":")).encode()


def page_href(did: str) -> str:
    return SPECIAL.get(did) or "/wiki/page/" + quote(did, safe="/")


def wiki_resolver(source: PurePosixPath, known: set[str]) -> LinkResolver:
    """Resolve a bundle link (absolute /wiki/x.md or relative) from the page at `source`."""

    def resolve(target: str) -> Link:
        did = resolve_link(target, source)
        if did is None:
            return Link(None, "broken", f"Not a wiki page: {target[:100]}")
        fragment = target.partition("#")[2]
        href = page_href(did) + (f"#{quote(fragment)}" if fragment else "")
        if did in known or did in SPECIAL or did.startswith("proposals/"):
            return Link(href)
        return Link(href, "broken", "This page does not exist (yet)")

    return resolve


def nav_types(index: dict[str, Any]) -> list[dict[str, Any]]:
    """Types for the wiki navigation: the writable types in configuration order, then any
    other type that has pages (raw sources, guides, reports), each with its page count."""
    types = [dict(t) for t in index.get("types", [])]
    listed = {t["type"] for t in types}
    counts = Counter(str(d.get("type") or "") for d in index.get("docs", []))
    for name, count in sorted(counts.items()):
        if name and name not in listed:
            types.append({"type": name, "folder": "", "description": "", "count": count})
    return types


def _source_of(did: str) -> PurePosixPath:
    return PurePosixPath(did + ".md")


class WikiRoutes:
    def __init__(self, portal: Portal, client: WikiClient) -> None:
        self.p = portal
        self.client = client
        # the same handlers, unguarded, for rendering a page as a fragment (api_view)
        self._views = [Route(path, handler) for path, handler in self._pages()]
        self._tree: Tree | None = None
        self._variants: dict[tuple[str, bool, bool], tuple[bytes, bytes]] = {}

    def _pages(self) -> list[tuple[str, Callable[[Request, Session], Awaitable[Response]]]]:
        return [
            ("/wiki", self.index),
            ("/wiki/type/{type}", self.by_type),
            ("/wiki/tag/{tag}", self.by_tag),
            ("/wiki/page/{id:path}", self.page),
            ("/wiki/search", self.search),
            ("/wiki/log", self.log),
            ("/wiki/recent", self.recent),
            ("/wiki/proposals", self.proposals),
            ("/wiki/merged", self.merged),
            ("/wiki/drafts", self.drafts),
            ("/wiki/graph", self.graph),
        ]

    def routes(self) -> list[Route]:
        api = self._api
        return [
            *(Route(path, self._guarded(handler)) for path, handler in self._pages()),
            Route("/wiki/api/graph", api(self.api_graph)),
            Route("/wiki/api/tree", api(self.api_tree)),
            Route("/wiki/api/fragment/{id:path}", api(self.api_fragment)),
            Route("/wiki/api/view", api(self.api_view)),
            Route("/wiki/api/mentions/{id:path}", api(self.api_mentions)),
            Route("/wiki/review", owner(self.p)(self.review), methods=["POST"]),
            Route("/wiki/merged/review", owner(self.p)(self.review_merged), methods=["POST"]),
            Route("/wiki/proposals/decide", owner(self.p)(self.decide), methods=["POST"]),
        ]

    async def review(self, request: Request, session: Session, form: FormData | None) -> Response:
        """Mark a page reviewed, then back to where the button was (a /wiki/ page)."""
        assert form is not None
        ref = str(form.get("id") or "")[:400]
        back = str(form.get("next") or "")
        back = back if back.startswith("/wiki/") and "//" not in back else DRAFTS
        try:
            result = await self.client.review(ref)
        except ValueError as exc:
            session.notify(f"Not marked reviewed: {exc}", ok=False)
        except WikiUnavailable:
            session.notify("The wiki service is not answering. Try again.", ok=False)
        else:
            done = result.get("status") == "unchanged"
            session.notify("Already reviewed." if done else "Marked reviewed.")
        return RedirectResponse(back, status_code=303)

    async def decide(self, request: Request, session: Session, form: FormData | None) -> Response:
        """Accept or reject an identity merge, then back to the proposals."""
        assert form is not None
        pid = str(form.get("id") or "")[:400]
        accept = form.get("decision") == "accept"
        try:
            if form.get("decision") == "refresh":
                await self.client.refresh_merge(pid)
            else:
                await self.client.decide_proposal(pid, accept, str(form.get("review_token") or ""))
        except ValueError as exc:
            session.notify(f"Nothing changed: {exc}", ok=False)
        except WikiUnavailable:
            session.notify("The wiki service is not answering. Try again.", ok=False)
        else:
            session.notify(
                "Preview refreshed. Review it before accepting."
                if form.get("decision") == "refresh"
                else "Merge accepted."
                if accept
                else "Merge rejected."
            )
        return RedirectResponse("/wiki/proposals", status_code=303)

    def _api(
        self, handler: Callable[[Request, Session], Awaitable[Response]]
    ) -> Callable[[Request], Awaitable[Response]]:
        """JSON endpoints: the owner's session (401 instead of a login redirect), requests
        from the portal's own pages only, errors as JSON."""

        async def endpoint(request: Request) -> Response:
            if request.method != "GET":
                return _JSON({"error": "read-only"}, status_code=405)
            background = request.headers.get("x-portal-background") == "1"
            session = self.p.session_of(request, touch=not background)
            if session is None or not session.authenticated:
                return _JSON({"error": "Your session has expired. Sign in again."}, 401)
            if request.headers.get("sec-fetch-site", "same-origin") != "same-origin":
                return _JSON({"error": "same-origin requests only"}, 403)
            try:
                return await handler(request, session)
            except NotFound:
                return _JSON({"error": "This wiki page does not exist."}, 404)
            except (KeyError, ValueError):
                return _JSON({"error": "Bad request."}, 400)
            except WikiUnavailable as exc:
                log.warning("wiki API: %s", exc)
                return _JSON({"error": "The wiki service is not reachable right now."}, 502)

        return endpoint

    def _guarded(
        self, handler: Callable[[Request, Session], Awaitable[Response]]
    ) -> Callable[[Request], Awaitable[Response]]:
        async def run(request: Request, session: Session, _: FormData | None) -> Response:
            try:
                return await handler(request, session)
            except NotFound:
                return self.p.render(
                    request,
                    "error.html",
                    session,
                    status=404,
                    message="This wiki page does not exist.",
                )
            except WikiUnavailable as exc:
                log.warning("wiki API: %s", exc)
                return self.p.render(
                    request,
                    "error.html",
                    session,
                    status=502,
                    message="The wiki service is not reachable right now. Check the dashboard.",
                )

        return owner(self.p)(run)

    async def _render(
        self,
        request: Request,
        template: str,
        session: Session,
        *,
        fragment: dict[str, Any] | None = None,
        **context: Any,
    ) -> Response:
        """Render a wiki template with the type navigation (from the briefly cached index).
        For the workspace (FRAGMENT in the request scope), only the page's own blocks, as
        JSON, with `fragment` added: the same markup as the full page, in a tab."""
        index = await self.client.cached_index()
        context["nav"] = nav_types(index)
        if not request.scope.get(FRAGMENT):
            return self.p.render(request, template, session, **context)
        parts = self.p.render_parts(request, template, session, FRAGMENT_BLOCKS, **context)
        url = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return _JSON(
            {
                "url": url,
                "kind": "view",
                "title": html.unescape(parts["title"].strip()) or "Wiki",
                "type": "",
                "html": parts["content"],
                "info_html": parts["sidebar_right"],
                **(fragment or {}),
            }
        )

    async def index(self, request: Request, session: Session) -> Response:
        data = await self.client.index()
        known = {d["id"] for d in data["docs"]}
        text = _body_of(data["markdown"])
        body = render_markdown(text, wiki_resolver(PurePosixPath("index.md"), known))
        return await self._render(
            request,
            "wiki/index.html",
            session,
            wiki_title=data["title"],
            types=data["types"],
            count=len(data["docs"]),
            body=body,
        )

    async def by_type(self, request: Request, session: Session) -> Response:
        type_name = request.path_params["type"][:80]
        data = await self.client.index()
        info = next((t for t in nav_types(data) if t["type"] == type_name), None)
        if info is None:
            raise NotFound(type_name)
        docs = await self.client.docs(type=type_name)
        return await self._render(
            request, "wiki/type.html", session, info=info, docs=docs, href=page_href
        )

    async def by_tag(self, request: Request, session: Session) -> Response:
        tag = request.path_params["tag"][:40].lower()
        docs = [d for d in await self.client.docs(tag=tag) if d.get("type") != "source"]
        return await self._render(
            request,
            "wiki/tag.html",
            session,
            tag=tag,
            review=False,
            heading=f"Tag: {tag}",
            docs=docs,
            href=page_href,
        )

    async def drafts(self, request: Request, session: Session) -> Response:
        """Pages whose current status is draft: already in the wiki (unlike proposals, which
        wait outside it), to be checked. Only `status` decides; provenance (trust, generated,
        the from-chat-import tag) does not."""
        docs = await self.client.drafts()
        # Stable pages that still carry the review tag: dreams leave them alone until the
        # owner marks them reviewed.
        unmarked = [
            d
            for d in await self.client.docs(tag=REVIEW_TAG)
            if d.get("status") in (None, "stable") and not str(d["path"]).startswith("raw/")
        ]
        return await self._render(
            request,
            "wiki/tag.html",
            session,
            tag="",
            review=True,
            heading="Drafts to review",
            docs=docs,
            unmarked=unmarked,
            href=page_href,
        )

    async def page(self, request: Request, session: Session) -> Response:
        ref = request.path_params["id"]
        try:
            rel = normalize_ref(ref)
        except PathError as exc:
            raise NotFound(ref) from exc
        if str(rel) in ("index.md", "log.md"):
            return RedirectResponse(SPECIAL[str(rel)[:-3]], status_code=303)
        doc = await self.client.doc(str(rel))
        known = await self.client.known_ids()
        kinds = {d["id"]: d.get("type") or "" for d in (await self.client.cached_index())["docs"]}
        resolve = wiki_resolver(PurePosixPath(doc["path"]), known)
        fm = doc.get("frontmatter") or {}
        outlinks = [
            (i, page_href(i), i in known, kinds.get(i, "")) for i in doc.get("outlinks", [])
        ]
        return await self._render(
            request,
            "wiki/page.html",
            session,
            doc=doc,
            body=render_markdown(doc["body"], resolve),
            meta=_frontmatter_rows(fm, known),
            crumbs=_crumbs(doc["id"]),
            outlinks=outlinks,
            canonical=canonical_of(fm),
            href=page_href,
            fragment={
                "kind": "page",
                "id": doc["id"],
                "type": fm.get("type")
                or ("proposal" if doc["path"].startswith("proposals/") else ""),
                "status": fm.get("status") or "",
                "draft": fm.get("status") == "draft" and not doc["path"].startswith("raw/"),
                "raw": doc["path"].startswith("raw/"),
                "private": fm.get("private") is True or doc.get("private") is True,
                "frontmatter": fm,
                "backlinks": [
                    {"id": b["id"], "title": b.get("title") or b["id"], "type": b.get("type") or ""}
                    for b in doc.get("backlinks", [])
                ],
                "outlinks": [
                    {"id": i, "exists": exists, "type": kind} for i, _, exists, kind in outlinks
                ],
            },
        )

    async def search(self, request: Request, session: Session) -> Response:
        q = request.query_params.get("q", "").strip()[:500]
        type_name = request.query_params.get("type", "").strip()[:80] or None
        data = await self.client.index()
        results = await self.client.search(q, type=type_name) if q else []
        for r in results:
            r["snippet_html"] = render_snippet(r.get("snippet") or "")
        return await self._render(
            request,
            "wiki/search.html",
            session,
            q=q,
            type=type_name or "",
            types=data["types"],
            results=results,
            href=page_href,
        )

    async def log(self, request: Request, session: Session) -> Response:
        text = await self.client.log()
        known = await self.client.known_ids()
        body = render_markdown(text, wiki_resolver(PurePosixPath("log.md"), known))
        return await self._render(request, "wiki/log.html", session, body=body)

    async def recent(self, request: Request, session: Session) -> Response:
        changes = await self.client.recent(100)
        known = await self.client.known_ids()
        for change in changes:
            for f in change["files"]:
                path = str(f["path"])
                did = path[:-3] if path.endswith(".md") else None
                f["href"] = page_href(did) if did and (did in known or did in SPECIAL) else None
        return await self._render(request, "wiki/recent.html", session, changes=changes)

    async def merged(self, request: Request, session: Session) -> Response:
        docs = [
            d
            for d in await self.client.docs(include_merged=True, merged_only=True)
            if d.get("canonical")
        ]
        return await self._render(request, "wiki/merged.html", session, docs=docs, href=page_href)

    async def review_merged(
        self, request: Request, session: Session, form: FormData | None
    ) -> Response:
        assert form is not None
        try:
            await self.client.review_merged(
                str(form.get("duplicate") or ""), str(form.get("canonical") or "")
            )
        except ValueError as exc:
            session.notify(str(exc), ok=False)
        except WikiUnavailable:
            session.notify("The wiki service is not answering. Try again.", ok=False)
        return RedirectResponse("/wiki/proposals", status_code=303)

    async def proposals(self, request: Request, session: Session) -> Response:
        items = await self.client.proposals()
        known = await self.client.known_ids()
        for item in items:
            target = str(item.get("target") or "")
            tid = target.removeprefix("/").removesuffix(".md")
            item["target_href"] = page_href(tid) if tid in known else None
            pid = str(item.get("proposal_id") or "")
            item["href"] = page_href(pid) if pid.startswith("proposals/") else None
            for key in ("dream", "pass", "summary"):  # set for proposals a dream made
                item.setdefault(key, None)
            item["speculative"] = item.get("speculative") is True
            item["identity"] = item.get("action") == "identity"
            if item["identity"] and item.get("preview"):
                preview = parse_document(str(item["preview"]))
                item["preview_html"] = render_markdown(
                    preview.body, wiki_resolver(normalize_ref(str(item["canonical"])), known)
                )
            for key in ("duplicate", "canonical"):  # set for identity merges
                did = str(item.get(key) or "")
                item[f"{key}_href"] = page_href(did) if did in known else None
        return await self._render(request, "wiki/proposals.html", session, proposals=items)

    async def graph(self, request: Request, session: Session) -> Response:
        """The link graph without JS: every page with its links, a page of pages at a time.
        The workspace script draws the graph from /wiki/api/graph over this list."""
        full = await self.client.graph()
        data = graph_variant(full, raw=False, proposals=False)
        unprocessed = sum(
            1 for n in full.get("nodes", []) if n.get("unprocessed") and not n.get("part")
        )
        nodes = {n["id"]: n for n in data["nodes"]}
        links: dict[str, list[dict[str, Any]]] = {}
        for e in data["edges"]:
            links.setdefault(e["source"], []).append(nodes[e["target"]])
        pages = sorted(
            (n for n in data["nodes"] if not n.get("ghost")),
            key=lambda n: (str(n.get("title") or "").casefold(), n["id"]),
        )
        try:
            number = max(1, int(request.query_params.get("page", "1")))
        except ValueError:
            number = 1
        last = max(1, -(-len(pages) // GRAPH_LIST_PAGE))
        number = min(number, last)
        shown = pages[(number - 1) * GRAPH_LIST_PAGE : number * GRAPH_LIST_PAGE]
        return await self._render(
            request,
            "wiki/graph.html",
            session,
            pages=shown,
            links=links,
            total=len(pages),
            edges=len(data["edges"]),
            broken=sum(1 for e in data["edges"] if e.get("broken")),
            orphans=sum(1 for n in pages if not n.get("backlink_count") and n["id"] not in links),
            number=number,
            last=last,
            unprocessed=unprocessed,
            nearly_empty=sum(1 for n in pages if str(n["id"]).startswith("wiki/")) < NEARLY_EMPTY,
            href=page_href,
        )

    # ------------------------------------------------------------------ JSON (workspace)

    async def api_graph(self, request: Request, _: Session) -> Response:
        graph = await self.client.graph()
        version = str(graph.get("version", ""))
        if request.query_params.get("v") == version:
            return _JSON({"version": version, "unchanged": True})
        key = (
            version,
            request.query_params.get("raw") == "1",
            request.query_params.get("proposals") == "1",
        )
        body = self._variants.get(key)
        if body is None:
            self._variants = {k: v for k, v in self._variants.items() if k[0] == version}
            body = self._variants[key] = encode(graph_variant(graph, raw=key[1], proposals=key[2]))
        headers = {"Vary": "Accept-Encoding"}
        if "gzip" in request.headers.get("accept-encoding", ""):
            headers["Content-Encoding"] = "gzip"
            return Response(body[1], media_type="application/json", headers=headers)
        return Response(body[0], media_type="application/json", headers=headers)

    async def _tree_of(self) -> Tree:
        graph = await self.client.graph()
        if self._tree is None or self._tree.version != graph.get("version"):
            self._tree = Tree(graph)
        return self._tree

    async def api_tree(self, request: Request, _: Session) -> Response:
        tree = await self._tree_of()
        q = request.query_params
        offset = int(q.get("offset", "0") or 0)
        limit = int(q.get("limit", "200") or 200)
        folder = q.get("folder", "")[:300]
        query = q.get("q", "").strip()[:200]
        only = q.get("only", "")
        if query or only in ("drafts", "unprocessed"):
            body = tree.matching(query, only, folder, offset, limit)
        else:
            body = tree.level(folder, offset, limit)
        return _JSON({"version": tree.version, **body})

    async def api_fragment(self, request: Request, session: Session) -> Response:
        return await self._view(request, session, "/wiki/page/" + request.path_params["id"])

    async def api_view(self, request: Request, session: Session) -> Response:
        return await self._view(request, session, request.query_params.get("path", ""))

    async def _view(self, request: Request, session: Session, url: str) -> Response:
        """Render the wiki page at `url` (a portal path) as a fragment."""
        parts = urlsplit(url[:2000])
        path = unquote(parts.path)
        if (
            parts.scheme
            or parts.netloc
            or not (path == "/wiki" or path.startswith("/wiki/"))
            or path.startswith("/wiki/api/")
        ):
            raise ValueError(url)
        scope = {
            **request.scope,
            "path": path,
            "raw_path": parts.path.encode(),
            "query_string": parts.query.encode(),
            "root_path": "",
            FRAGMENT: True,
        }
        for route in self._views:
            match, child = route.matches(scope)
            if match == Match.FULL:
                scope.update(child)
                response: Response = await route.endpoint(Request(scope, request.receive), session)
                if isinstance(response, RedirectResponse):
                    return _JSON({"redirect": response.headers["location"]})
                return response
        raise NotFound(path)

    async def api_mentions(self, request: Request, _: Session) -> Response:
        doc = await self.client.doc(request.path_params["id"])
        title = str(doc.get("title") or "")
        linked = {str(b["id"]) for b in doc.get("backlinks", [])}
        hits = await self.client.search(title) if title.strip() else []
        for h in hits:
            h["snippet_html"] = render_snippet(plain_snippet(h.get("snippet") or ""))
        mentions, context = unlinked_mentions(title, doc["id"], linked, hits)
        return _JSON({"id": doc["id"], "title": title, "mentions": mentions, "context": context})


def _body_of(text: str) -> str:
    """index.md without its frontmatter (rendered, it would show as a stray heading)."""
    try:
        return parse_document(text).body
    except DocumentError:
        return text


def _crumbs(did: str) -> list[str]:
    return did.split("/")[:-1]


def _frontmatter_rows(fm: dict[str, Any], known: set[str]) -> list[tuple[str, Any]]:
    """Frontmatter as (key, value) rows; values stay structured for the template macro.

    Strings that are ids of existing pages become {"link": ...} so the template can
    link them (related:, sources:, proposal targets). Everything is escaped by Jinja.
    """

    def conv(value: Any, depth: int = 0) -> Any:
        if depth > 4:
            return str(value)[:200]
        if isinstance(value, dict):
            return {"dict": [(str(k), conv(v, depth + 1)) for k, v in list(value.items())[:50]]}
        if isinstance(value, list | tuple):
            return {"list": [conv(v, depth + 1) for v in list(value)[:100]]}
        text = "" if value is None else str(value)
        did = text.removeprefix("/").removesuffix(".md")
        if did in known:
            return {"link": did, "href": page_href(did)}
        return text[:1000]

    return [(str(k), conv(v)) for k, v in fm.items() if k not in ("title",)]
