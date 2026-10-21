"""Lossless, deterministic page consolidation. No semantic claim is selected as truth."""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Callable
from typing import Any

from wiki_mcp.config import Trust
from wiki_mcp.wiki.okf import Document, alias_from_title, aliases_of, name_key, serialize_document

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_ITEM = re.compile(r"^[-*+]\s|^\d+[.)]\s")
_DEFINITION = re.compile(r"^\[([^\]]+)\]:\s*(.*)$", re.M)


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def union(a: list[Any], b: list[Any]) -> list[Any]:
    out = list(a)
    for value in b:
        if value not in out:
            out.append(value)
    return out


def sections(body: str) -> list[tuple[tuple[str, ...], str, str]]:
    """Full heading paths and their bodies; headings inside fences are literal content."""
    out: list[tuple[tuple[str, ...], str, str]] = []
    path: list[tuple[int, str]] = []
    heading, fence = "", ""
    lines: list[str] = []
    for line in body.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if not fence:
                fence = marker[1]
            elif (
                marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and line.strip() == marker[1]
            ):
                fence = ""
        match = _HEADING.match(line) if not fence and not marker else None
        if match:
            out.append((tuple(p[1] for p in path), heading, "\n".join(lines)))
            level = len(match[1])
            while path and path[-1][0] >= level:
                path.pop()
            # The page title is identity, not a content section.
            if level > 1:
                path.append((level, match[2]))
            heading, lines = line, []
        else:
            lines.append(line)
    out.append((tuple(p[1] for p in path), heading, "\n".join(lines)))
    return out


def blocks(body: str) -> list[str]:
    """Paragraphs, complete list items and fenced code; never discard continuation lines."""
    out: list[str] = []
    lines: list[str] = []
    fence = ""
    for line in body.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if not fence and (not line.strip() or (_ITEM.match(line) and lines)):
            if lines:
                out.append("\n".join(lines).rstrip("\n"))
                lines = []
            if not line.strip():
                continue
        lines.append(line)
        if marker:
            if not fence:
                fence = marker[1]
            elif (
                marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and line.strip() == marker[1]
            ):
                fence = ""
    if lines:
        out.append("\n".join(lines).rstrip("\n"))
    return out


def rewrite_markdown(body: str, transform: Callable[[str], str]) -> str:
    """Rewrite Markdown references outside fenced and inline code."""
    lines = []
    fence = ""
    for line in body.splitlines(keepends=True):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if not fence:
                fence = marker[1]
            elif (
                marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and line.strip() == marker[1]
            ):
                fence = ""
            lines.append(line)
        elif fence:
            lines.append(line)
        else:
            pieces = re.split(r"(`+[^`]*`+)", line)
            lines.append("".join(p if i % 2 else transform(p) for i, p in enumerate(pieces)))
    return "".join(lines)


def rebase(body: str, duplicate: str) -> str:
    """Preserve relative Markdown link/image and definition targets after moving content."""

    def target(value: str) -> str:
        if value.startswith(("/", "#")) or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value):
            return value
        return "/" + posixpath.normpath(posixpath.join(posixpath.dirname(duplicate), value))

    body = rewrite_markdown(
        body, lambda text: re.sub(r"(\]\(<?)([^\s)>]+)", lambda m: m[1] + target(m[2]), text)
    )
    return rewrite_markdown(
        body,
        lambda text: re.sub(
            r"^(\[(?!\^)[^\]]+\]:\s*<?)([^\s>]+)", lambda m: m[1] + target(m[2]), text, flags=re.M
        ),
    )


def consolidate(
    duplicate: Document,
    canonical: Document,
    duplicate_id: str,
    canonical_id: str,
    provenance: dict[str, Any],
) -> str:
    df, cf = duplicate.frontmatter or {}, dict(canonical.frontmatter or {})
    try:
        cf["trust"] = Trust.lowest(
            Trust(str(cf.get("trust") or "agent")), Trust(str(df.get("trust") or "agent"))
        ).value
    except ValueError:
        cf["trust"] = Trust.EXTERNAL.value
    body = rebase(duplicate.body, duplicate_id)
    sources = [dict(s) for s in df.get("sources") or []]
    for source in sources:
        if isinstance(source.get("resource"), str):
            source["resource"] = rebase("[x](" + source["resource"] + ")", duplicate_id)[4:-1]

    # Footnotes and reference links share a label namespace. Rename collisions before
    # deduplication, so equal words citing different evidence remain distinct entries.
    def evidence_map(fm_sources: list[dict[str, Any]], text: str) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        for source in fm_sources:
            evidence.setdefault(str(source.get("id")), {})["source"] = source
        label = ""
        for line in text.splitlines():
            definition = _DEFINITION.match(line)
            if definition:
                label = definition[1].lstrip("^")
                evidence.setdefault(label, {})["definition"] = definition[2]
            elif label and (line.startswith((" ", "\t")) or not line):
                evidence[label]["definition"] += "\n" + line
            else:
                label = ""
        for value in evidence.values():
            if "definition" in value:
                value["definition"] = value["definition"].rstrip("\n")
        return evidence

    have = evidence_map(cf.get("sources") or [], canonical.body)
    incoming = evidence_map(sources, body)
    occupied = set(have) | set(incoming)
    for label, evidence in incoming.items():
        if label not in have or have[label] == evidence:
            continue
        suffix = fingerprint(duplicate_id)[:8]
        renamed = f"{label}-merged-{suffix}"
        while renamed in occupied:
            renamed += "-2"
        occupied.add(renamed)
        for old, new in ((label, renamed), ("^" + label, "^" + renamed)):

            def rename_reference(text: str, old: str = old, new: str = new) -> str:
                return text.replace("[" + old + "]", "[" + new + "]")

            body = rewrite_markdown(body, rename_reference)
        for source in sources:
            if str(source.get("id")) == label:
                source["id"] = renamed
    aliases = aliases_of(cf)
    known = {name_key(a) for a in [canonical.title or "", *aliases]}
    for alias in [alias_from_title(duplicate.title or ""), *aliases_of(df)]:
        if alias and name_key(alias) not in known:
            aliases.append(alias)
            known.add(name_key(alias))
    if aliases:
        cf["aliases"] = aliases
    cf["sources"] = union(cf.get("sources") or [], sources)
    for key in ("related", "relationships", "tags"):
        if key in df:
            cf[key] = union(cf.get(key) or [], df.get(key) or [])
    cf["related"] = [
        r
        for r in cf.get("related") or []
        if str(r).strip("/").removesuffix(".md") not in (duplicate_id, canonical_id)
    ]
    conflicts = []
    identity = {
        "id",
        "title",
        "type",
        "status",
        "canonical",
        "aliases",
        "sources",
        "related",
        "relationships",
        "tags",
        "created",
        "updated",
        "generated",
        "verified",
        "trust",
        "private",
        "merge_history",
        "merge_consolidated",
    }
    for key, value in df.items():
        if key in identity:
            continue
        if key not in cf:
            cf[key] = value
        elif cf[key] != value:
            conflicts.append({"field": key, "canonical": cf[key], "duplicate": value})
    merged = sections(canonical.body)
    for path, heading, content in sections(body):
        additions = blocks(content)
        if not additions:
            continue
        index = next((i for i in range(len(merged) - 1, -1, -1) if merged[i][0] == path), None)
        if index is None:
            # Add absent ancestors once, placing children within their parent's subtree.
            for depth in range(1, len(path) + 1):
                prefix = path[:depth]
                if any(p == prefix for p, _, _ in merged):
                    continue
                parent = prefix[:-1]
                candidates = [
                    i for i, (p, _, _) in enumerate(merged) if parent and p[: len(parent)] == parent
                ]
                position = candidates[-1] + 1 if candidates else len(merged)
                h = heading if depth == len(path) else "#" * (depth + 1) + " " + prefix[-1]
                merged.insert(position, (prefix, h, ""))
            index = next((i for i, (p, _, _) in enumerate(merged) if p == path), None)
            if index is None:
                merged.append((path, "", ""))
                index = len(merged) - 1
        p, h, old = merged[index]
        have_blocks = blocks(old)
        unique = [b for b in additions if b not in have_blocks]
        unique = union([], unique)
        if unique:
            old = old.rstrip("\n") + ("\n\n" if old.strip() else "") + "\n\n".join(unique)
            merged[index] = p, h, old
    merged_body = "\n\n".join(
        "\n\n".join(v for v in (h, b.strip("\n")) if v) for _, h, b in merged
    ).strip()
    cf["merge_history"] = [
        *(cf.get("merge_history") or []),
        {**provenance, "page": duplicate_id, "frontmatter": df, "metadata_conflicts": conflicts},
    ]
    notice = (
        f"## Merge review: {duplicate.title or duplicate_id}\n\n"
        f"Content consolidated from original page `/{duplicate_id}.md`. "
        "All distinct claims and their citations are retained. Claims that disagree "
        "remain unresolved and require review; this merge does not choose a claim as truth. "
        "Original dates and provenance are recorded in merge_history and Git history."
    )
    if conflicts:
        notice += (
            "\n\nConflicting metadata (both values retained in merge_history): "
            + ", ".join(str(c["field"]) for c in conflicts)
            + "."
        )
    return serialize_document(cf, merged_body + "\n\n" + notice + "\n")
