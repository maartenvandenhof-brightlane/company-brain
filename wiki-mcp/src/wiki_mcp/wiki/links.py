"""Markdown link extraction and resolution (OKF §6: standard markdown links)."""

from __future__ import annotations

import posixpath
import re
from pathlib import PurePosixPath

from wiki_mcp.wiki.paths import PathError, doc_id, normalize_ref

_FENCE = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_LINK = re.compile(r"(?<!!)\[([^\]\n]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")


def strip_code(body: str) -> str:
    return _INLINE_CODE.sub("", _FENCE.sub("", body))


def extract_links(body: str, source: PurePosixPath) -> list[str]:
    """Return ids of bundle documents linked from `body` (order preserved, deduplicated).

    Absolute links (`/wiki/x.md`) resolve against the bundle root, relative links
    against the linking document's directory. External URLs are ignored.
    """
    out: list[str] = []
    for _label, target in _LINK.findall(strip_code(body)):
        resolved = resolve_link(target, source)
        if resolved is not None and resolved not in out:
            out.append(resolved)
    return out


def resolve_link(target: str, source: PurePosixPath) -> str | None:
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
        return None
    path = target.split("#", 1)[0].split("?", 1)[0]
    if not path.endswith(".md"):
        return None
    if path.startswith("/"):
        joined = path.lstrip("/")
    else:
        joined = posixpath.normpath(posixpath.join(str(source.parent), path))
        if joined.startswith(".."):
            return None
    try:
        return doc_id(normalize_ref(joined))
    except PathError:
        return None


def markdown_link(title: str, rel: PurePosixPath) -> str:
    label = title.replace("[", "(").replace("]", ")") or rel.stem
    return f"[{label}](/{rel})"
