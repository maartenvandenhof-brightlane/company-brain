"""Client for wiki-mcp's portal API (the only way the portal sees the wiki).

Everything is read-only except import_chats(), which can only add new raw sources; file(),
which files one batch-processed conversation; the owner's review(), goals(), statement() and
decide_proposal(); a dream's writes (dream_commit(), dream_proposal(), dream_revert()), which
wiki-mcp checks; an assistant dream's session (dream_session_open(), dream_session_close(),
which write its hygiene fixes and its log); the owner's private-access toggle per
connected app (set_private_access()); a chat's observations (observe(), which wiki-mcp checks
against the transcript) and the owner's feedback on a personal pattern (pattern_feedback()).

The index (navigation, known page ids) and the link graph (the workspace's graph, explorer and
switcher) are cached briefly. Both writes drop those caches, so what an import job or a
processing run adds shows at once; writes made elsewhere (an assistant over MCP) show within
the cache time. The graph is revalidated with its version, which costs wiki-mcp almost nothing
when nothing changed.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx2


class WikiUnavailable(RuntimeError):
    """wiki-mcp could not be reached or answered with an unexpected error."""


class NotFound(LookupError):
    pass


class WikiClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.http = httpx2.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )
        self._ids: set[str] = set()
        self._index: dict[str, Any] = {}
        self._ids_at = -1e9
        self._graph: dict[str, Any] = {}
        self._graph_at = -1e9
        self.generation = 0  # bumped by every write through this client (and invalidate())

    async def close(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        try:
            r = await self.http.get(path, params=clean)
        except httpx2.HTTPError as exc:
            raise WikiUnavailable(f"wiki-mcp unreachable ({type(exc).__name__})") from exc
        if r.status_code == 404:
            raise NotFound(r.json().get("error", "not found"))
        if r.status_code == 400:
            raise ValueError(r.json().get("error", "bad request"))
        if r.status_code != 200:
            raise WikiUnavailable(f"wiki-mcp answered HTTP {r.status_code}")
        body = r.json()
        assert isinstance(body, dict)
        return body

    async def index(self) -> dict[str, Any]:
        body = await self._get("/index")
        self._ids = set(body.get("known_ids", [])) | {d["id"] for d in body.get("docs", [])}
        self._index = body
        self._ids_at = time.monotonic()
        return body

    async def cached_index(self, max_age: float = 30.0) -> dict[str, Any]:
        """The index, fetched again when older than max_age (for navigation)."""
        if time.monotonic() - self._ids_at > max_age:
            await self.index()
        return self._index

    async def known_ids(self, max_age: float = 30.0) -> set[str]:
        """Ids of indexed pages, for marking broken links (cached briefly)."""
        await self.cached_index(max_age)
        return self._ids

    def invalidate(self) -> None:
        """Forget the cached index and graph: the wiki just changed."""
        self._ids_at = self._graph_at = -1e9
        self.generation += 1

    async def graph(self, max_age: float = 10.0) -> dict[str, Any]:
        """The whole link graph, raw sources and proposals included (GET /graph), cached for
        max_age seconds and then revalidated by version."""
        if time.monotonic() - self._graph_at <= max_age:
            return self._graph
        generation = self.generation
        since = self._graph.get("version")
        body = await self._get("/graph", include="raw,proposals", since=since)
        if not body.get("unchanged"):
            self._graph = body
        if generation == self.generation:  # no write landed while this was in flight
            self._graph_at = time.monotonic()
        return self._graph

    async def docs(
        self,
        *,
        type: str | None = None,
        tag: str | None = None,
        status: str | None = None,
        include_merged: bool = False,
        merged_only: bool = False,
    ) -> list[dict[str, Any]]:
        return list(
            (
                await self._get(
                    "/docs",
                    type=type,
                    tag=tag,
                    status=status,
                    include_merged="true" if include_merged else None,
                    merged_only="true" if merged_only else None,
                )
            ).get("docs", [])
        )

    async def drafts(self) -> list[dict[str, Any]]:
        """Pages to review: `status: draft` in their current frontmatter (raw sources and
        observation pages aside, which are never reviewed), whatever their tags, trust or
        origin."""
        return [
            d
            for d in await self.docs(status="draft")
            if not d["path"].startswith("raw/") and d.get("type") != "observation"
        ]

    async def doc(self, ref: str) -> dict[str, Any]:
        return await self._get("/doc", id=ref)

    async def search(self, q: str, *, type: str | None = None) -> list[dict[str, Any]]:
        return list((await self._get("/search", q=q, type=type, limit=30)).get("results", []))

    async def recent(self, limit: int = 50, *, since: str | None = None) -> list[dict[str, Any]]:
        """Commits, newest first; `since` is an ISO date or datetime."""
        return list((await self._get("/recent", limit=limit, since=since)).get("changes", []))

    async def log(self) -> str:
        return str((await self._get("/log")).get("markdown", ""))

    async def processing(self) -> dict[str, Any]:
        """Counts of imported chats to process, and to re-process under the current playbook."""
        return await self._get("/processing")

    async def proposals(self) -> list[dict[str, Any]]:
        return list((await self._get("/proposals")).get("proposals", []))

    async def open_loops(self) -> list[dict[str, Any]]:
        """Unchecked follow-ups on stable pages and the owner profile (GET /open-loops)."""
        return list((await self._get("/open-loops")).get("loops", []))

    async def renamed(self, ref: str) -> list[str]:
        """The page ids git history shows `ref` was renamed to (GET /renamed)."""
        return [str(i) for i in (await self._get("/renamed", id=ref)).get("to", [])]

    async def unprocessed(self, *, scope: str = "new", limit: int = 1000) -> dict[str, Any]:
        """Imported chats to process: no page cites them yet ("new"), also those filed before
        the playbook ("older"), or all of them ("all")."""
        return await self._get(
            "/unprocessed",
            rerun="1" if scope == "older" else None,
            all="1" if scope == "all" else None,
            limit=limit,
        )

    async def file(self, filing: dict[str, Any]) -> dict[str, Any]:
        """File one processed conversation (POST /file); ValueError when it is refused."""
        try:
            return await self._post("/file", filing)
        finally:
            self.invalidate()

    async def import_chats(
        self, sources: list[dict[str, Any]], *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Store chat transcripts as raw sources; existing ones are reported, never changed."""
        try:
            return await self._post("/import", {"dry_run": dry_run, "sources": sources})
        finally:
            if not dry_run:
                self.invalidate()

    async def review(self, ref: str) -> dict[str, Any]:
        """The owner marks a page reviewed: stable, without the from-chat-import tag."""
        return await self._write("/review", {"id": ref})

    async def decide_proposal(
        self, pid: str, accept: bool, review_token: str = ""
    ) -> dict[str, Any]:
        """The owner accepts or rejects a pending proposal (the only way an identity merge is
        accepted); ValueError when wiki-mcp refuses."""
        decision = "accept" if accept else "reject"
        return await self._write(
            "/proposal", {"id": pid, "decision": decision, "review_token": review_token}
        )

    async def refresh_merge(self, pid: str) -> dict[str, Any]:
        return await self._write("/proposal", {"id": pid, "decision": "refresh"})

    async def review_merged(self, duplicate: str, canonical: str) -> dict[str, Any]:
        return await self._write("/merge-review", {"duplicate": duplicate, "canonical": canonical})

    async def goals(self, add: list[str], drop: list[str]) -> dict[str, Any]:
        """The owner's goals: new ones appended, dropped ones marked superseded."""
        return await self._write("/owner", {"action": "goals", "add": add, "drop": drop})

    async def statement(self, text: str, section: str = "Facts") -> dict[str, Any]:
        """Something the owner states about himself, filed on his profile with its source."""
        return await self._write(
            "/owner", {"action": "statement", "text": text, "section": section}
        )

    async def dream_commit(
        self, dream: int, pass_: str, reason: str, operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """A dream pass's direct changes as one commit: {commit, pages}."""
        payload = {"dream": dream, "pass": pass_, "reason": reason, "operations": operations}
        return await self._write("/dream/commit", payload)

    async def dream_proposal(
        self,
        dream: int,
        pass_: str,
        summary: str,
        reason: str,
        operations: list[dict[str, Any]],
        *,
        speculative: bool = False,
    ) -> dict[str, Any]:
        """A dream's change as a proposal for the owner: {proposal_id, ...}."""
        payload = {
            "dream": dream,
            "pass": pass_,
            "summary": summary,
            "reason": reason,
            "operations": operations,
            "speculative": speculative,
        }
        return await self._write("/dream/proposal", payload)

    async def dream_revert(self, dream: int) -> dict[str, Any]:
        """Revert a dream's direct commits and reject its proposals: {reverted, rejected,
        conflict}."""
        return await self._write("/dream/revert", {"dream": dream})

    async def dream_session_open(
        self,
        dream: int,
        client: str,
        started: str,
        number: int,
        since_base: str | None,
        kind: str = "dream",
    ) -> dict[str, Any]:
        """Open an assistant dream's session (kind "patterns": a patterns session, the patterns
        pass only); for a dream wiki-mcp makes the hygiene fixes at once. Its state (as
        dream_session); ValueError when refused (the dream had a session, another one is
        open)."""
        payload = {
            "dream": dream,
            "client": client,
            "started": started,
            "number": number,
            "since_base": since_base,
            "kind": kind,
        }
        return await self._write("/dream/session", payload)

    async def dream_session(self, dream: int) -> dict[str, Any]:
        """An assistant dream's session: {status: open | expired | finished, client, opened,
        expires, finished, ended, passes_done, passes_to_do, commits, proposals, log_page,
        ...}; NotFound when the dream has none."""
        return await self._get("/dream/session", dream=dream)

    async def dream_session_close(self, dream: int) -> dict[str, Any]:
        """Write the dream log and finish the session (an expired one as expired, an open one
        as closed by the owner); its state. ValueError when it was closed before."""
        return await self._write("/dream/session/close", {"dream": dream})

    async def apps(self) -> dict[str, Any]:
        """The connected OAuth apps with their private access, and the internal client
        (LibreChat, no toggle): {apps, internal} (GET /apps)."""
        return await self._get("/apps")

    async def set_private_access(self, label: str, access: str) -> dict[str, Any]:
        """The owner's private-access toggle for one app: on, off or default (as consented).
        ValueError for an unknown app; answers the app as /apps lists it."""
        return await self._post("/apps/private", {"label": label, "access": access})

    async def observations_pending(
        self, *, limit: int = 100, outdated: str | None = None
    ) -> dict[str, Any]:
        """Imported chats without an observation page (with `outdated`, an extractor version:
        also those another one observed), oldest first: {total, sources: [{id, parts}]}."""
        return await self._get("/observations/pending", limit=limit, outdated=outdated)

    async def observe(self, observations: dict[str, Any]) -> dict[str, Any]:
        """Write one chat's observation page (POST /observations): {status: committed |
        exists, observations, dropped, ...}; ValueError when it is refused."""
        return await self._write("/observations", observations)

    async def patterns_material(self) -> dict[str, Any]:
        """The patterns pass's material, built by wiki-mcp (dream/patterns.py Material)."""
        try:
            r = await self.http.get("/patterns/material", timeout=120.0)
        except httpx2.HTTPError as exc:
            raise WikiUnavailable(f"wiki-mcp unreachable ({type(exc).__name__})") from exc
        if r.status_code != 200:
            raise WikiUnavailable(f"wiki-mcp answered HTTP {r.status_code}")
        body = r.json()
        assert isinstance(body, dict)
        return body

    async def pattern_feedback(self, ref: str, verdict: str, note: str) -> dict[str, Any]:
        """The owner's verdict on a personal pattern: agree, dispute, correct or unsure."""
        return await self._write(
            "/patterns/feedback", {"id": ref, "verdict": verdict, "note": note}
        )

    async def drive_start(self) -> dict[str, Any]:
        """The folder mode's "00 Start here.md" and "00 Index.md": {start_here, index};
        NotFound without an export."""
        return await self._get("/drive/start")

    async def drive_zip(self) -> bytes:
        """The folder mode's export as a zip that unzips to a folder Brain; NotFound without
        an export."""
        try:
            r = await self.http.get("/drive/brain.zip", timeout=120.0)
        except httpx2.HTTPError as exc:
            raise WikiUnavailable(f"wiki-mcp unreachable ({type(exc).__name__})") from exc
        if r.status_code == 404:
            raise NotFound(r.json().get("error", "not found"))
        if r.status_code != 200:
            raise WikiUnavailable(f"wiki-mcp answered HTTP {r.status_code}")
        return r.content

    async def _write(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._post(path, payload)
        finally:
            self.invalidate()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        content = json.dumps(payload, ensure_ascii=False).encode()
        try:
            r = await self.http.post(
                path,
                content=content,
                headers={"Content-Type": "application/json"},
                timeout=120.0,
            )
        except httpx2.HTTPError as exc:
            raise WikiUnavailable(f"wiki-mcp unreachable ({type(exc).__name__})") from exc
        if r.status_code in (400, 413):
            raise ValueError(r.json().get("error", "bad request"))
        if r.status_code != 200:
            raise WikiUnavailable(f"wiki-mcp answered HTTP {r.status_code}")
        body = r.json()
        assert isinstance(body, dict)
        return body
