"""SQLite FTS5 search index plus a link table for backlinks.

The markdown files in git are the source of truth; this index is derived and is
rebuilt from scratch on every startup, then kept in sync on each write. Links are body links,
`related` pages and cited raw `sources`.

Every read that serves a client takes `private`: False leaves out documents marked
`private: true`, in SQL, so a LIMIT still returns as many visible rows as there are.

A page's `aliases` are searchable with its title. A duplicate page (`canonical:` set) is left
out of search, and the pages linking to it count as backlinks of its canonical page.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from wiki_mcp.wiki.links import extract_links
from wiki_mcp.wiki.okf import Document, aliases_of, canonical_of, is_private
from wiki_mcp.wiki.paths import PathError, doc_id, normalize_ref

_TOKEN = re.compile(r"\w+", re.UNICODE)
_PART = re.compile(r"-part-(\d+)$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    type TEXT,
    title TEXT,
    description TEXT,
    tags TEXT,
    status TEXT,
    trust TEXT,
    created TEXT,
    updated TEXT,
    private INTEGER NOT NULL DEFAULT 0,
    aliases TEXT,
    canonical TEXT,
    merge_consolidated INTEGER NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    id UNINDEXED, title, description, tags, body,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS links (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    PRIMARY KEY (src, dst)
);
CREATE INDEX IF NOT EXISTS links_dst ON links(dst);
CREATE INDEX IF NOT EXISTS docs_canonical ON docs(canonical);
CREATE TABLE IF NOT EXISTS activity (
    id TEXT PRIMARY KEY,
    at TEXT
);
"""


@dataclass
class DocRow:
    id: str
    path: str
    type: str | None
    title: str
    description: str
    tags: list[str]
    status: str | None
    trust: str | None
    created: str | None
    updated: str | None
    private: bool = False
    aliases: list[str] = field(default_factory=list)
    canonical: str | None = None  # a duplicate: the id of the page it redirects to

    merge_consolidated: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "type": self.type,
            "title": self.title,
            "description": self.description,
            "tags": self.tags,
            "status": self.status,
            "updated": self.updated,
            "private": self.private,
            "aliases": self.aliases,
            "canonical": self.canonical,
            "merge_consolidated": self.merge_consolidated,
        }


@dataclass
class SearchHit:
    row: DocRow
    snippet: str


def _row(r: sqlite3.Row) -> DocRow:
    return DocRow(
        id=r["id"],
        path=r["path"],
        type=r["type"],
        title=r["title"] or "",
        description=r["description"] or "",
        tags=json.loads(r["tags"] or "[]"),
        status=r["status"],
        trust=r["trust"],
        created=r["created"],
        updated=r["updated"],
        private=bool(r["private"]),
        aliases=json.loads(r["aliases"] or "[]"),
        canonical=r["canonical"],
        merge_consolidated=bool(r["merge_consolidated"]),
    )


def cited_sources(fm: dict[str, Any]) -> list[str]:
    """Ids of the raw sources a page cites in `sources` ({resource: /raw/….md} or a raw/ id).
    Indexed as links, so a source's backlinks are the pages written from it."""
    out: list[str] = []
    sources = fm.get("sources")
    for src in sources if isinstance(sources, list) else []:
        ref = src.get("resource") if isinstance(src, dict) else src
        if not isinstance(ref, str) or not ref.lstrip("/").startswith("raw/"):
            continue
        try:
            sid = doc_id(normalize_ref(ref))
        except PathError:
            continue
        if sid not in out:
            out.append(sid)
    return out


# Words that carry no topic in a conversational query ("tell me more about my company"),
# English and Dutch. Dropped before searching unless nothing else is left.
_FILLER_WORDS = """
    a about all also am an and any are as at be been but by can could did do does for from
    get give had has have how i if in into is it its just know let me more most my of on
    or our please s show some tell than that the their them there these this those to us was
    we were what when where which who why will with would you your
    aan al alle als ben bij dan dat de deze die dit doe een en er heb hebben heeft het hoe
    ik in is je jij kan kun kunnen maar me meer met mij mijn na naar niet nog of om ons onze
    ook op over te toch tot uit van vertel voor waar wat was we wel wie wij zijn
"""
FILLER = frozenset(_FILLER_WORDS.split())


def fts_query(text: str, *, mode: str = "AND") -> str | None:
    """Turn free text into a safe FTS5 expression (every token quoted; last one prefix).
    Filler words are dropped unless the query consists of nothing else."""
    words = [t for t in _TOKEN.findall(text.lower()) if t]
    tokens = [t for t in words if t not in FILLER][:20] or words[:20]
    if not tokens:
        return None
    quoted = [f'"{t}"' for t in tokens]
    quoted[-1] += "*"
    return f" {mode} ".join(quoted)


def activity_of(fm: dict[str, Any]) -> str | None:
    """When a document was last active: a chat's last message, else its last update."""
    for key in ("last_message_at", "updated", "created"):
        value = fm.get(key)
        if value:
            return str(value)
    return None


class SearchIndex:
    def __init__(self, path: Path | str) -> None:
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(docs)")}
        if columns and not {"private", "aliases", "canonical", "merge_consolidated"} <= columns:
            self.db.execute("DROP TABLE docs")  # an index from before these; rebuilt anyway
        self.db.executescript(_SCHEMA)
        # Bumped by every write, so readers (the portal's graph) can tell cheaply whether
        # anything changed since they last looked. Restarts reset it: pair it with a boot id.
        self.generation = 0

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ writes

    def _upsert(self, rel: PurePosixPath, doc: Document) -> None:
        fm = doc.frontmatter or {}
        did = doc_id(rel)
        raw_tags = fm.get("tags")
        tags: list[Any] = raw_tags if isinstance(raw_tags, list) else []
        title = doc.title or rel.stem
        description = fm.get("description") if isinstance(fm.get("description"), str) else ""
        aliases = aliases_of(fm)
        self.db.execute("DELETE FROM fts WHERE id = ?", (did,))
        self.db.execute("DELETE FROM links WHERE src = ?", (did,))
        self.db.execute(
            "INSERT OR REPLACE INTO activity (id, at) VALUES (?, ?)", (did, activity_of(fm))
        )
        self.db.execute(
            "INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                did,
                str(rel),
                doc.type,
                title,
                description,
                json.dumps([str(t) for t in tags]),
                fm.get("status"),
                fm.get("trust"),
                fm.get("created"),
                fm.get("updated"),
                int(is_private(fm)),
                json.dumps(aliases),
                canonical_of(fm),
                int(fm.get("merge_consolidated") is True),
            ),
        )
        # The type and folder are searchable as keywords, so "my projects" finds project pages.
        kinds = [
            doc.type or "",
            rel.parts[1] if rel.parts[0] == "wiki" and len(rel.parts) > 2 else "",
        ]
        self.db.execute(
            "INSERT INTO fts (id, title, description, tags, body) VALUES (?,?,?,?,?)",
            (
                did,
                " ".join([title, *aliases]),  # an alias finds the page like its title
                description,
                " ".join([*(str(t) for t in tags), *kinds]),
                doc.body,
            ),
        )
        targets = extract_links(doc.body, rel)
        related = fm.get("related")
        for item in [
            *(str(r) for r in (related if isinstance(related, list) else [])),
            *cited_sources(fm),
        ]:
            if item not in targets:
                targets.append(item)
        self.db.executemany(
            "INSERT OR IGNORE INTO links (src, dst) VALUES (?, ?)",
            [(did, t) for t in targets if t != did],
        )

    def upsert(self, rel: PurePosixPath, doc: Document) -> None:
        with self.db:
            self.db.execute("BEGIN")
            self._upsert(rel, doc)
        self.generation += 1

    def remove(self, did: str) -> None:
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute("DELETE FROM docs WHERE id = ?", (did,))
            self.db.execute("DELETE FROM fts WHERE id = ?", (did,))
            self.db.execute("DELETE FROM links WHERE src = ?", (did,))
            self.db.execute("DELETE FROM activity WHERE id = ?", (did,))
        self.generation += 1

    def rebuild(self, docs: Iterable[tuple[PurePosixPath, Document]]) -> int:
        count = 0
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute("DELETE FROM docs")
            self.db.execute("DELETE FROM fts")
            self.db.execute("DELETE FROM links")
            self.db.execute("DELETE FROM activity")
            for rel, doc in docs:
                self._upsert(rel, doc)
                count += 1
        self.generation += 1
        return count

    # ------------------------------------------------------------------ reads

    def get(self, did: str) -> DocRow | None:
        r = self.db.execute("SELECT * FROM docs WHERE id = ?", (did,)).fetchone()
        return _row(r) if r else None

    def search(
        self,
        text: str,
        *,
        type: str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
        private: bool = True,
    ) -> list[SearchHit]:
        """Pages matching all words first, then pages matching any word (also when all words
        only matched raw sources); curated pages
        (anything outside raw/) before raw sources, so long transcripts cannot push them out,
        and deprecated pages last within each. Duplicates (`canonical:` set) are left out:
        their canonical page is the hit (it carries their title as an alias)."""
        limit = max(1, min(limit, 50))
        hits: dict[str, tuple[int, SearchHit]] = {}
        for mode in ("AND", "OR"):
            if len(hits) >= limit and any(
                not h.row.path.startswith("raw/") for _, h in hits.values()
            ):
                break
            expr = fts_query(text, mode=mode)
            if expr is None:
                return []
            sql = (
                "SELECT d.*, snippet(fts, 4, '**', '**', '…', 16) AS snip "
                "FROM fts JOIN docs d ON d.id = fts.id WHERE fts MATCH ? AND d.canonical IS NULL"
            )
            params: list[Any] = [expr]
            if not private:
                sql += " AND d.private = 0"
            if type:
                sql += " AND d.type = ?"
                params.append(type)
            sql += (
                " ORDER BY d.path LIKE 'raw/%', COALESCE(d.status, '') = 'deprecated',"
                " bm25(fts, 0.0, 10.0, 4.0, 3.0, 1.0) LIMIT ?"
            )
            params.append(limit * 5 if tags else limit)
            rows = self.db.execute(sql, params).fetchall()
            found = [SearchHit(_row(r), r["snip"] or "") for r in rows]
            if tags:
                wanted = {t.lower() for t in tags}
                found = [h for h in found if wanted <= set(h.row.tags)]
            for h in found:
                hits.setdefault(h.row.id, (len(hits), h))
        ranked = sorted(
            hits.values(),
            key=lambda p: (
                p[1].row.path.startswith("raw/"),
                p[1].row.status == "deprecated",
                p[0],
            ),
        )
        return [h for _, h in ranked][:limit]

    def list_docs(
        self,
        *,
        type: str | None = None,
        tag: str | None = None,
        folder: str | None = None,
        status: str | None = None,
        limit: int = 200,
        private: bool = True,
        include_merged: bool = False,
        merged_only: bool = False,
    ) -> list[DocRow]:
        sql = "SELECT * FROM docs WHERE 1=1"
        params: list[Any] = []
        if merged_only:
            sql += " AND canonical IS NOT NULL"
        elif not include_merged:
            sql += " AND canonical IS NULL"
        if not private:
            sql += " AND private = 0"
        if type:
            sql += " AND type = ?"
            params.append(type)
        if status:
            sql += " AND status = ?"
            params.append(status)
        if folder:
            sql += " AND (path LIKE ? ESCAPE '\\')"
            escaped = (
                folder.strip("/").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            params.append(escaped + "/%")
        sql += " ORDER BY path"
        rows = [_row(r) for r in self.db.execute(sql, params).fetchall()]
        if tag:
            rows = [r for r in rows if tag.lower() in r.tags]
        return rows[: max(1, min(limit, 1000))]

    def all(self) -> list[DocRow]:
        return [_row(r) for r in self.db.execute("SELECT * FROM docs ORDER BY path").fetchall()]

    def private_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT id FROM docs WHERE private = 1")}

    def backlinks(self, did: str, *, private: bool = True) -> list[DocRow]:
        """Pages linking to `did`, or to a duplicate merged into it (links to a duplicate are
        never rewritten: they reach the canonical page through the redirect)."""
        rows = self.db.execute(
            "SELECT DISTINCT d.* FROM links l JOIN docs d ON d.id = l.src "
            "WHERE d.canonical IS NULL AND (l.dst = ? OR l.dst IN (SELECT id FROM docs WHERE canonical = ? "
            "AND (? OR private = 0))) AND d.id != ? AND (? OR d.private = 0) ORDER BY d.path",
            (did, did, private, did, private),
        ).fetchall()
        return [_row(r) for r in rows]

    def merged_into(self, did: str, *, private: bool = True) -> list[str]:
        """Ids of the duplicates whose `canonical` is `did`."""
        rows = self.db.execute(
            "SELECT id FROM docs WHERE canonical = ? AND (? OR private = 0) ORDER BY path",
            (did, private),
        )
        return [r[0] for r in rows]

    def uncited_sources(
        self, *, tag: str | None = None, cited_before: str | None = None, private: bool = True
    ) -> list[tuple[list[DocRow], str | None]]:
        """Raw sources that no page cites or links to, oldest first, each with None. The
        `-part-N` files of one transcript form one group (parts in order), which counts as
        cited when any part is. With `cited_before` (an ISO timestamp), also the transcripts
        whose citing pages were all last updated before it, with that latest update. An
        observation page (the evidence for personal patterns) never counts as citing one."""
        rows = self.db.execute(
            "SELECT d.*, (SELECT MAX(COALESCE(s.updated, '')) FROM links l "
            "JOIN docs s ON s.id = l.src "
            "WHERE l.dst = d.id AND COALESCE(s.type, '') NOT IN ('source', 'observation')) "
            "AS cited_at "
            "FROM docs d WHERE d.type = 'source' AND d.path LIKE 'raw/%' AND (? OR d.private = 0) "
            "ORDER BY d.created, d.path",
            (private,),
        ).fetchall()
        groups: dict[str, list[tuple[int, DocRow]]] = {}
        cited: dict[str, str] = {}  # base id -> latest update of a citing page
        for r in rows:
            row = _row(r)
            m = _PART.search(row.id)
            base = row.id[: m.start()] if m else row.id
            groups.setdefault(base, []).append((int(m.group(1)) if m else 1, row))
            if r["cited_at"] is not None:
                cited[base] = max(cited.get(base, ""), r["cited_at"])
        wanted = tag.lower() if tag else None
        return [
            ([row for _, row in sorted(parts, key=lambda p: p[0])], cited.get(base))
            for base, parts in groups.items()
            if (wanted is None or wanted in parts[0][1].tags)
            and (base not in cited or (cited_before is not None and cited[base] < cited_before))
        ]

    def outlinks(self, did: str) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT dst FROM links WHERE src = ?", (did,))]

    def all_links(self) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self.db.execute("SELECT src, dst FROM links")]

    def backlink_counts(self) -> dict[str, int]:
        """How many documents link to each target (existing or not), in one query."""
        rows = self.db.execute("SELECT dst, COUNT(*) FROM links GROUP BY dst")
        return {r[0]: int(r[1]) for r in rows}

    def activity(self) -> dict[str, str]:
        """Each document's last activity (see activity_of), for sorting by date."""
        rows = self.db.execute("SELECT id, at FROM activity WHERE at IS NOT NULL")
        return {r[0]: r[1] for r in rows}
