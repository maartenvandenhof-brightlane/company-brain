"""Data for the wiki workspace (static/js/workspace/): the graph, the file explorer and unlinked
mentions, all derived from wiki-mcp's link graph (GET /graph, cached by WikiClient.graph).

Nothing here renders markdown or reads page bodies: pages come to the browser as fragments
rendered by the same templates as the full pages (wiki.WikiRoutes), sanitised by render.py.

Imported chats can number in the thousands, most of them linked to nothing. The explorer
therefore lists one folder level at a time, a page of entries at a time, with a chat's
`-part-N` files and `-after-N` continuations grouped under the conversation, newest first.
"""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass, field
from typing import Any

ROOT_ORDER = {"wiki": 0, "raw": 1, "proposals": 2}
MAX_PAGE = 500
OBSERVATIONS = "wiki/observations/"
_PART_TITLE = re.compile(r"(?: \(continued\))?(?: \(part \d+ of \d+\))?$")


def hidden(node: dict[str, Any]) -> bool:
    """Observation pages: machine-written evidence for personal patterns, one per imported
    chat. Never browsed; the patterns link to them and their URLs keep working."""
    return node.get("type") == "observation" or str(node["id"]).startswith(OBSERVATIONS)


def graph_variant(graph: dict[str, Any], *, raw: bool, proposals: bool) -> dict[str, Any]:
    """The graph as the workspace shows it: raw sources and proposals only when asked for,
    observation pages never. Ghost nodes stay only while a kept edge points to them."""

    def kept(node: dict[str, Any]) -> bool:
        nid = str(node["id"])
        if hidden(node):
            return False
        if nid.startswith("raw/"):
            return raw
        if nid.startswith("proposals/"):
            return proposals
        return True

    nodes = {n["id"]: n for n in graph.get("nodes", []) if kept(n)}
    edges = [e for e in graph.get("edges", []) if e["source"] in nodes and e["target"] in nodes]
    pointed = {e["target"] for e in edges}
    return {
        "version": graph.get("version", ""),
        "skipped_page": graph.get("skipped_page", ""),
        "nodes": [n for n in nodes.values() if not n.get("ghost") or n["id"] in pointed],
        "edges": edges,
    }


def encode(body: dict[str, Any]) -> tuple[bytes, bytes]:
    """JSON bytes and their gzip, computed once per graph version."""
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    return raw, gzip.compress(raw, compresslevel=6)


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _mark(entry: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    for key in ("draft", "unprocessed", "skipped"):
        if node.get(key):
            entry[key] = True
    return entry


def _page(node: dict[str, Any]) -> dict[str, Any]:
    entry = {
        "kind": "page",
        "id": node["id"],
        "title": node.get("title") or _name(node["id"]),
        "type": node.get("type") or "",
    }
    if node.get("date"):
        entry["date"] = node["date"]
    return _mark(entry, node)


def _part_label(node: dict[str, Any]) -> str:
    after, part = node.get("after"), node.get("part")
    label = f"Continued after {after} messages" if after else "Conversation"
    return f"{label}, part {part}" if part else label


def _conversation(key: str, members: list[dict[str, Any]]) -> dict[str, Any]:
    """One entry for the files of one imported conversation, in reading order."""
    members = sorted(members, key=lambda n: (n.get("after", 0), n.get("part", 1)))
    if len(members) == 1:
        return _page(members[0])
    first = members[0]
    dates = [str(n["date"]) for n in members if n.get("date")]
    entry: dict[str, Any] = {
        "kind": "conversation",
        "id": key,
        "title": _PART_TITLE.sub("", str(first.get("title") or "")) or _name(key),
        "type": first.get("type") or "source",
        "parts": [{**_page(n), "label": _part_label(n)} for n in members],
    }
    if dates:
        entry["date"] = max(dates)
    if any(n.get("unprocessed") for n in members):
        entry["unprocessed"] = True
    elif all(n.get("skipped") for n in members):
        entry["skipped"] = True
    return entry


def _entries(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Explorer entries for these documents: conversations grouped, chats newest first,
    everything else by title."""
    groups: dict[str, list[dict[str, Any]]] = {}
    out: list[dict[str, Any]] = []
    for node in nodes:
        key = node.get("conversation")
        if key:
            groups.setdefault(str(key), []).append(node)
        else:
            out.append(_page(node))
    chats = [_conversation(k, members) for k, members in groups.items()]
    out.sort(key=lambda e: (str(e["title"]).casefold(), e["id"]))
    chats.sort(key=lambda e: (str(e.get("date") or ""), e["id"]), reverse=True)
    return out + chats


@dataclass
class _Folder:
    path: str
    children: set[str] = field(default_factory=set)
    nodes: list[dict[str, Any]] = field(default_factory=list)
    count: int = 0
    drafts: int = 0
    unprocessed: int = 0


class Tree:
    """One graph version, indexed by folder for the explorer, without observation pages.
    Entry lists are sorted on first use and kept, so paging through a big folder costs a
    slice."""

    def __init__(self, graph: dict[str, Any]) -> None:
        self.version = str(graph.get("version", ""))
        self.folders: dict[str, _Folder] = {"": _Folder("")}
        self.nodes = [n for n in graph.get("nodes", []) if not n.get("ghost") and not hidden(n)]
        for node in self.nodes:
            folder = str(node.get("folder") or "")
            self._folder(folder).nodes.append(node)
            unit = not node.get("part")  # a conversation's later parts are not counted twice
            path = folder
            while True:
                f = self._folder(path)
                f.count += 1
                f.drafts += bool(node.get("draft"))
                f.unprocessed += bool(node.get("unprocessed")) and unit
                if not path:
                    break
                path = path.rsplit("/", 1)[0] if "/" in path else ""
        self._sorted: dict[str, list[dict[str, Any]]] = {}

    def _folder(self, path: str) -> _Folder:
        f = self.folders.get(path)
        if f is None:
            f = self.folders[path] = _Folder(path)
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            self._folder(parent).children.add(path)
        return f

    def _summary(self, path: str) -> dict[str, Any]:
        f = self.folders[path]
        entry: dict[str, Any] = {"kind": "folder", "path": path, "name": _name(path)}
        entry["count"] = f.count
        if f.drafts:
            entry["drafts"] = f.drafts
        if f.unprocessed:
            entry["unprocessed"] = f.unprocessed
        return entry

    def level(self, folder: str, offset: int = 0, limit: int = 200) -> dict[str, Any]:
        """The subfolders and a page of the entries directly in `folder`."""
        folder = folder.strip("/")
        f = self.folders.get(folder)
        if f is None:
            raise KeyError(folder)
        entries = self._sorted.get(folder)
        if entries is None:
            entries = self._sorted[folder] = _entries(f.nodes)
        subfolders = sorted(f.children, key=lambda p: (ROOT_ORDER.get(p, 9), p.casefold()))
        return self._page(folder, [self._summary(p) for p in subfolders], entries, offset, limit)

    def matching(
        self, q: str, only: str = "", folder: str = "", offset: int = 0, limit: int = 200
    ) -> dict[str, Any]:
        """A flat list across folders: names containing `q` (title or id, any case), only
        drafts or only unprocessed chats, within `folder` when given. Wiki pages first."""
        needle = q.casefold().strip()
        prefix = folder.strip("/")
        picked = []
        for node in self.nodes:
            if prefix and not str(node["id"]).startswith(prefix + "/"):
                continue
            if only == "drafts" and not node.get("draft"):
                continue
            if only == "unprocessed" and not node.get("unprocessed"):
                continue
            if needle and needle not in f"{node.get('title', '')}\n{node['id']}".casefold():
                continue
            picked.append(node)
        entries = _entries(picked)
        entries.sort(key=lambda e: str(e["id"]).startswith("raw/"))  # stable: keeps the order
        page = self._page(prefix, [], entries, offset, limit)
        # transcripts, as processing counts them (a continuation is one of its own)
        page["transcripts"] = sum(1 for n in picked if not n.get("part"))
        return page

    @staticmethod
    def _page(
        folder: str,
        folders: list[dict[str, Any]],
        entries: list[dict[str, Any]],
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        offset = max(0, offset)
        limit = max(1, min(limit, MAX_PAGE))
        items = entries[offset : offset + limit]
        end = offset + len(items)
        return {
            "folder": folder,
            "folders": folders if offset == 0 else [],
            "items": items,
            "total": len(entries),
            "offset": offset,
            "next_offset": end if end < len(entries) else None,
        }


_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_TARGET = re.compile(r"\]?\([^)\s]*\.md[^)\s]*\)?")
_MD_HEADING = re.compile(r"(^|\s)#{1,6}\s")
_MD_CUT = re.compile(r"^(…?)[^\s(]*\.md\)")  # a snippet that starts inside a link target


def plain_snippet(snippet: str) -> str:
    """A search snippet (markdown with **match** markers) as readable text: link targets,
    brackets and heading marks dropped, the markers kept."""
    text = _MD_LINK.sub(r"\1", _MD_CUT.sub(r"\1", snippet))
    text = _MD_TARGET.sub("", text)
    text = _MD_HEADING.sub(r"\1", text).replace("[", "").replace("]", "")
    return " ".join(text.split())


def unlinked_mentions(
    title: str, did: str, linked: set[str], hits: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """From search hits for a page's title: the pages that mention it without linking to it
    (the snippet must contain the whole title), and the snippets of those that do link to
    it, as context for its backlinks. Hits carry `snippet_html` (render.render_snippet)."""
    wanted = " ".join(title.casefold().split())
    mentions: list[dict[str, Any]] = []
    context: dict[str, str] = {}
    for hit in hits:
        hid = str(hit.get("id") or "")
        if not hid or hid == did:
            continue
        plain = " ".join(str(hit.get("snippet") or "").replace("**", "").casefold().split())
        if hid in linked:
            context[hid] = str(hit.get("snippet_html") or "")
        elif wanted and wanted in plain:
            mentions.append(
                {
                    "id": hid,
                    "title": hit.get("title") or hid,
                    "type": hit.get("type") or "",
                    "snippet_html": hit.get("snippet_html") or "",
                }
            )
    return mentions, context
