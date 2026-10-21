"""OKF v0.2 document parsing, serialization and validation.

A document is UTF-8 markdown with YAML frontmatter between `---` lines. OKF
requires only `type`; this wiki additionally requires `title` and `description`
on concept pages and adds extension keys (`id`, `created`, `updated`, `trust`,
`related`, `private`, `aliases`, `canonical`). Unknown keys are preserved on round-trip as the
spec requires.

Identities: `aliases` lists other names a page is known by (searchable). A duplicate page
redirects to its canonical page with `canonical: <page id>` (and `status: deprecated`); it is
never deleted. `canonical` is set only by an accepted identity merge (the server's), never by
a write.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import yaml

from wiki_mcp.wiki.paths import PathError, doc_id, normalize_ref

OKF_VERSION = "0.2"
STATUSES = ("draft", "stable", "deprecated")
SERVER_KEYS = frozenset(
    {
        "id",
        "created",
        "updated",
        "generated",
        "verified",
        "trust",
        "canonical",
        "merge_history",
        "merge_consolidated",
    }
)
KEY_ORDER = (
    "type",
    "id",
    "title",
    "description",
    "aliases",
    "resource",
    "tags",
    "status",
    "canonical",
    "private",
    "generated",
    "verified",
    "stale_after",
    "sources",
    "related",
    "trust",
    "created",
    "updated",
)
_EXTRA_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_TAG = re.compile(r"^[a-z0-9][a-z0-9/_-]{0,39}$")
_SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
MAX_EXTRA_KEYS = 20
MAX_LIST = 50
MAX_ALIASES = 20
# Generic words for the owner, always the owner besides the profile's title and aliases (as
# name_key compares them): no person page is created for them either.
OWNER_WORDS = ("owner", "the owner", "eigenaar", "de eigenaar")
_TRAILING_PARENS = re.compile(r"\s*\([^()]*\)\s*$")


class DocumentError(ValueError):
    """A document or frontmatter is malformed. `errors` lists every problem found."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


# --------------------------------------------------------------------- YAML


class _NoAliasSafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects YAML aliases (no 'billion laughs' expansion)."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("YAML aliases are not allowed in frontmatter")
        return super().compose_node(parent, index)


def _plain(value: Any) -> Any:
    """Convert YAML-native datetimes/dates to ISO strings, recursively."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return format_ts(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def safe_yaml_load(text: str) -> Any:
    return _plain(yaml.load(text, Loader=_NoAliasSafeLoader))  # noqa: S506 - SafeLoader subclass


def format_ts(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def now_ts() -> str:
    return format_ts(dt.datetime.now(dt.UTC))


def parse_ts(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


# --------------------------------------------------------------------- documents


@dataclass
class Document:
    frontmatter: dict[str, Any] | None
    body: str

    @property
    def type(self) -> str | None:
        value = (self.frontmatter or {}).get("type")
        return value if isinstance(value, str) and value.strip() else None

    @property
    def title(self) -> str:
        value = (self.frontmatter or {}).get("title")
        return value if isinstance(value, str) else ""


def parse_document(text: str) -> Document:
    """Split frontmatter and body. Raises DocumentError if frontmatter is malformed."""
    if text.startswith("﻿"):
        text = text[1:]
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return Document(None, text)
    lines = text.splitlines(keepends=True)
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            raw = "".join(lines[1:i])
            body = "".join(lines[i + 1 :])
            try:
                data = safe_yaml_load(raw) if raw.strip() else {}
            except yaml.YAMLError as exc:
                raise DocumentError([f"invalid YAML frontmatter: {exc}"]) from exc
            if not isinstance(data, dict):
                raise DocumentError(["frontmatter must be a YAML mapping"])
            return Document(data, body.lstrip("\n"))
    raise DocumentError(["frontmatter is not terminated by a '---' line"])


def is_private(frontmatter: dict[str, Any] | None) -> bool:
    """Whether a document is marked private (only `private: true` counts)."""
    return (frontmatter or {}).get("private") is True


def name_key(name: str) -> str:
    """A name compared for identity: case-insensitive, whitespace normalised."""
    return " ".join(name.split()).casefold()


def alias_from_title(title: str) -> str:
    """The alias a merged duplicate's title gives its canonical page: without a trailing
    parenthetical ("Eigenaar (redirect)" -> "Eigenaar"), whitespace collapsed; may be empty."""
    return " ".join(_TRAILING_PARENS.sub("", title).split())


def aliases_of(frontmatter: dict[str, Any] | None) -> list[str]:
    """The other names a page is known by (`aliases`), strings only."""
    value = (frontmatter or {}).get("aliases")
    items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return [" ".join(a.split()) for a in items if isinstance(a, str) and a.strip()]


def canonical_of(frontmatter: dict[str, Any] | None) -> str | None:
    """The id of the page a duplicate redirects to (`canonical`), or None."""
    value = (frontmatter or {}).get("canonical")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return doc_id(normalize_ref(value.strip()))
    except PathError:
        return None


def _dump_frontmatter(frontmatter: dict[str, Any]) -> str:
    ordered = {k: frontmatter[k] for k in KEY_ORDER if k in frontmatter}
    ordered.update({k: v for k, v in frontmatter.items() if k not in ordered})
    dumped = yaml.safe_dump(
        ordered, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100
    )
    return f"---\n{dumped}---\n"


def serialize_document(frontmatter: dict[str, Any] | None, body: str) -> str:
    body = body.strip("\n") + "\n"
    if frontmatter is None:
        return body
    return f"{_dump_frontmatter(frontmatter)}\n{body}"


def replace_frontmatter(text: str, frontmatter: dict[str, Any]) -> str:
    """`text` with a new frontmatter block; everything after it stays byte for byte (so a raw
    source keeps its content hash)."""
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines(keepends=True)
    if lines and lines[0].rstrip("\r\n") == "---":
        for i in range(1, len(lines)):
            if lines[i].rstrip("\r\n") == "---":
                return _dump_frontmatter(frontmatter) + "".join(lines[i + 1 :])
    raise DocumentError(["document has no frontmatter"])


# --------------------------------------------------------------------- validation


@dataclass
class ConceptInput:
    """Validated, normalized client-supplied frontmatter for a concept page."""

    type: str
    title: str
    description: str
    tags: list[str] = field(default_factory=list)
    status: str = "stable"
    resource: str | None = None
    stale_after: str | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    private: bool | None = None  # None: not given (an existing page keeps its value)
    aliases: list[str] | None = None  # None: not given (an existing page keeps its value)

    def raw_source_ids(self) -> list[str]:
        """Bundle ids (raw/...) of sources that point inside the bundle."""
        out = []
        for src in self.sources:
            res = src.get("resource", "")
            if isinstance(res, str) and res.startswith("/raw/"):
                out.append(res[1:].removesuffix(".md"))
        return out


def _str_field(data: dict[str, Any], key: str, errors: list[str], *, max_len: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"'{key}' is required and must be a non-empty string")
        return ""
    value = " ".join(value.split())
    if len(value) > max_len:
        errors.append(f"'{key}' must be at most {max_len} characters")
    return value


def _is_scalarish(value: Any, depth: int = 0, list_limit: int = MAX_LIST) -> bool:
    if value is None or isinstance(value, str | int | float | bool):
        return True
    if depth >= 2:
        return False
    if isinstance(value, list):
        return len(value) <= list_limit and all(
            _is_scalarish(v, depth + 1, list_limit) for v in value
        )
    if isinstance(value, dict):
        return len(value) <= list_limit and all(
            isinstance(k, str) and _is_scalarish(v, depth + 1, list_limit) for k, v in value.items()
        )
    return False


def normalize_source(item: Any) -> dict[str, Any]:
    """Accept a raw id ('raw/2026-..'), a URL, or an OKF source mapping."""
    if isinstance(item, str):
        item = item.strip()
        if item.startswith(("http://", "https://")):
            return {"resource": item}
        rel = normalize_ref(item)
        if rel.parts[0] != "raw":
            raise PathError(f"source {item!r} must be a raw/ source id or an http(s) URL")
        return {"id": rel.stem, "resource": "/" + str(rel)}
    if isinstance(item, dict):
        resource = item.get("resource")
        if not isinstance(resource, str) or not resource.strip():
            raise PathError("each source needs a 'resource'")
        resource = resource.strip()
        out: dict[str, Any] = {}
        if resource.startswith(("http://", "https://")):
            out["resource"] = resource
        else:
            rel = normalize_ref(resource)
            out["resource"] = "/" + str(rel)
            out.setdefault("id", rel.stem)
        if "id" in item:
            sid = str(item["id"])
            if not _SOURCE_ID.match(sid):
                raise PathError(f"invalid source id {sid!r}")
            out["id"] = sid
        for key in ("title", "author", "last_modified"):
            if isinstance(item.get(key), str):
                out[key] = item[key][:200]
        return out
    raise PathError("each source must be a string or a mapping")


def validate_concept_input(
    data: Any, *, known_types: Iterable[str], consolidated: bool = False
) -> ConceptInput:
    """Validate client-supplied frontmatter. Server-managed keys are ignored."""
    if not isinstance(data, dict):
        raise DocumentError(["frontmatter must be an object"])
    errors: list[str] = []
    list_limit = 1000 if consolidated else MAX_LIST
    alias_limit = 1000 if consolidated else MAX_ALIASES
    tag_limit = 1000 if consolidated else 20
    known = list(known_types)

    ctype = data.get("type")
    if not isinstance(ctype, str) or ctype not in known:
        errors.append(f"'type' must be one of: {', '.join(known)}")
        ctype = ""
    title = _str_field(data, "title", errors, max_len=200)
    description = _str_field(data, "description", errors, max_len=500)

    tags: list[str] = []
    raw_tags = data.get("tags", [])
    if isinstance(raw_tags, str):
        raw_tags = [t for t in raw_tags.split(",") if t.strip()]
    if not isinstance(raw_tags, list) or len(raw_tags) > tag_limit:
        errors.append(f"'tags' must be a list of at most {tag_limit} strings")
    else:
        for tag in raw_tags:
            norm = str(tag).strip().lower().replace(" ", "-")
            if not _TAG.match(norm):
                errors.append(f"invalid tag {tag!r} (lowercase letters, digits, '-', '_', '/')")
            elif norm not in tags:
                tags.append(norm)

    status = data.get("status", "stable")
    if status not in STATUSES:
        errors.append(f"'status' must be one of: {', '.join(STATUSES)}")

    private = data.get("private")
    if private is not None and not isinstance(private, bool):
        errors.append("'private' must be true or false")
        private = None

    aliases: list[str] | None = None
    raw_aliases = data.get("aliases")
    if isinstance(raw_aliases, str):
        raw_aliases = [raw_aliases]
    if raw_aliases is not None:
        if not isinstance(raw_aliases, list) or len(raw_aliases) > alias_limit:
            errors.append(f"'aliases' must be a list of at most {alias_limit} names")
        else:
            aliases = []
            for alias in raw_aliases:
                name = " ".join(alias.split()) if isinstance(alias, str) else ""
                if not name or len(name) > 200:
                    errors.append(f"invalid alias {alias!r} (a name of 1-200 characters)")
                elif name_key(name) not in {name_key(a) for a in aliases}:
                    aliases.append(name)

    resource = data.get("resource")
    if resource is not None and (not isinstance(resource, str) or len(resource) > 500):
        errors.append("'resource' must be a URI string")

    stale_after = data.get("stale_after")
    if stale_after is not None:
        try:
            stale_after = format_ts(parse_ts(str(stale_after)))
        except ValueError:
            errors.append("'stale_after' must be an ISO 8601 datetime")
            stale_after = None

    sources: list[dict[str, Any]] = []
    raw_sources = data.get("sources", [])
    if not isinstance(raw_sources, list) or len(raw_sources) > list_limit:
        errors.append(f"'sources' must be a list of at most {list_limit} items")
    else:
        for item in raw_sources:
            try:
                src = normalize_source(item)
            except PathError as exc:
                errors.append(str(exc))
                continue
            if src not in sources:
                sources.append(src)

    related: list[str] = []
    raw_related = data.get("related", [])
    if not isinstance(raw_related, list) or len(raw_related) > list_limit:
        errors.append(f"'related' must be a list of at most {list_limit} page ids")
    else:
        for item in raw_related:
            try:
                rid = doc_id(normalize_ref(str(item)))
            except PathError as exc:
                errors.append(f"related {item!r}: {exc}")
                continue
            if rid not in related:
                related.append(rid)

    extra: dict[str, Any] = {}
    for key, value in data.items():
        if key in KEY_ORDER or key in SERVER_KEYS:
            continue
        if not isinstance(key, str) or not _EXTRA_KEY.match(key):
            errors.append(f"invalid frontmatter key {key!r}")
        elif not _is_scalarish(value, list_limit=list_limit):
            errors.append(f"frontmatter key {key!r} has an unsupported value")
        else:
            extra[key] = value
    if len(extra) > MAX_EXTRA_KEYS:
        errors.append(f"at most {MAX_EXTRA_KEYS} custom frontmatter keys are allowed")

    if errors:
        raise DocumentError(errors)
    return ConceptInput(
        type=ctype,
        title=title,
        description=description,
        tags=tags,
        status=str(status),
        resource=resource,
        stale_after=stale_after,
        sources=sources,
        related=related,
        extra=extra,
        private=private,
        aliases=aliases,
    )


def conformance_errors(rel_name: str, doc: Document, *, is_root: bool) -> list[str]:
    """OKF §11 conformance for a single file."""
    if rel_name in ("index.md", "log.md"):
        fm = doc.frontmatter
        if fm is None:
            return []
        if rel_name == "index.md" and is_root and set(fm) <= {"okf_version"}:
            return []
        return [f"{rel_name} must not carry frontmatter (only root index.md may set okf_version)"]
    if doc.frontmatter is None:
        return ["missing YAML frontmatter"]
    if doc.type is None:
        return ["frontmatter has no non-empty 'type'"]
    return []
