"""Path validation. Every path coming from a client goes through this module."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from wiki_mcp.wiki.types import ConceptType, TypeRegistry

RESERVED_NAMES = frozenset({"index.md", "log.md"})
_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_ANY_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,119}$")
MAX_DEPTH = 6


class PathError(ValueError):
    """A client-supplied path is invalid or unsafe."""


def normalize_ref(ref: str) -> PurePosixPath:
    """Turn a client-supplied path or id into a bundle-relative `.md` path.

    Accepts `wiki/people/jane`, `wiki/people/jane.md` and `/wiki/people/jane.md`.
    Rejects traversal, absolute filesystem paths, backslashes and control chars.
    """
    if not isinstance(ref, str):
        raise PathError("path must be a string")
    ref = ref.strip()
    if not ref or len(ref) > 400:
        raise PathError("path must be 1-400 characters")
    if "\\" in ref or "\x00" in ref or any(ord(c) < 32 for c in ref):
        raise PathError("path contains forbidden characters")
    if ref.startswith("//") or "://" in ref:
        raise PathError("expected a bundle path, not a URL")
    ref = ref.lstrip("/")
    if "#" in ref:
        ref = ref.split("#", 1)[0]
    if any(seg in ("", ".", "..") for seg in ref.split("/")):
        raise PathError("path must not contain empty, '.' or '..' segments")
    rel = PurePosixPath(ref)
    if rel.suffix != ".md":
        rel = rel.with_name(rel.name + ".md")
    parts = rel.parts
    if not parts or len(parts) > MAX_DEPTH:
        raise PathError(f"path depth must be 1-{MAX_DEPTH}")
    for part in parts:
        if part in ("", ".", "..") or part.startswith("."):
            raise PathError("path must not contain '.', '..' or hidden segments")
        if not _ANY_SEGMENT.match(part):
            raise PathError(f"invalid path segment {part!r}")
    return rel


def doc_id(rel: PurePosixPath) -> str:
    """Stable id of a document: its bundle path without the `.md` suffix."""
    return str(rel.with_suffix(""))


def bundle_link(rel: PurePosixPath) -> str:
    """OKF bundle-absolute link target for a document."""
    return "/" + str(rel)


def resolve_in_bundle(root: Path, rel: PurePosixPath) -> Path:
    """Map a normalized relative path onto disk, refusing anything that escapes the
    bundle root or passes through a symlink."""
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*rel.parts)
    current = root_resolved
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise PathError("symlinks are not allowed in the wiki")
    try:
        candidate.resolve().relative_to(root_resolved)
    except ValueError as exc:
        raise PathError("path escapes the wiki") from exc
    if rel.parts[0] == ".git":
        raise PathError("path escapes the wiki")
    return candidate


def check_concept_path(rel: PurePosixPath, registry: TypeRegistry, type_name: str) -> ConceptType:
    """Validate that `rel` is a writable location for a concept of `type_name`."""
    ctype = registry.get(type_name)
    if ctype is None:
        known = ", ".join(t.name for t in registry.writable())
        raise PathError(f"unknown type {type_name!r}; known types: {known}")
    if ctype.managed:
        by = f"; they are written by {ctype.written_by}" if ctype.written_by else ""
        raise PathError(
            f"pages of type {type_name!r} are managed by the server and read-only for agents{by}"
        )
    if rel.name in RESERVED_NAMES:
        raise PathError(f"{rel.name} is reserved")
    folder_parts = ctype.folder.parts
    if rel.parts[: len(folder_parts)] != folder_parts or len(rel.parts) <= len(folder_parts):
        raise PathError(
            f"a {type_name!r} page must live under {ctype.folder}/, e.g. {ctype.folder}/<slug>.md"
        )
    for part in rel.parts[len(folder_parts) :]:
        stem = part[:-3] if part.endswith(".md") else part
        if not _SEGMENT.match(stem):
            raise PathError(
                f"invalid slug {stem!r}: use lowercase letters, digits, '-' or '_' (max 80 chars)"
            )
    return ctype


def slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:max_len].rstrip("-")) or "untitled"
