"""The wiki store: all reads and writes of the OKF bundle.

Writes are serialised by a single asyncio lock. Each write produces exactly one
git commit that also updates the root index.md and log.md, and the search index
is updated after the commit succeeds. On failure the working tree is restored.

Pages marked `private: true` are only for actors with the wiki:private scope (Actor.sees_private):
for anyone else every read behaves as if they did not exist, and writes to them fail the same
way. Such an actor may still create new private pages.

A duplicate page (`canonical: <page id>`, `status: deprecated`) redirects to its canonical page:
read_redirected follows it, writes to it are refused. Only an identity merge sets it: proposed
by propose_identity_merge, accepted by the owner in the portal (the MCP tool refuses).

Personal patterns: observation pages (wiki/observations.py, written by write_observations from
the portal's chat processing) are the evidence; pattern pages (wiki/patterns.py, a dream's
write_pattern operations) the hypotheses, with the owner's feedback (pattern_feedback). Both
are always private, and their commits carry neutral messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from wiki_mcp.config import Trust
from wiki_mcp.wiki.actor import DREAM, SYSTEM, Actor
from wiki_mcp.wiki.consolidate import consolidate, fingerprint
from wiki_mcp.wiki.dreamops import (
    BRIEFING_PAGE,
    BRIEFING_PAGES,
    BRIEFING_PRIVATE_PAGE,
    DIRECT_OPS,
    PROPOSAL_OPS,
    Op,
    OpError,
    parse_ops,
)
from wiki_mcp.wiki.facts import (
    SPECULATIVE_BANNER,
    above,
    bullet_lines,
    collapse_checkboxes,
    guard_bullets,
    is_inference_id,
    line_date,
    mark_superseded,
    opening,
    pronouns_only,
    rubric,
    superseded,
    swapped_link,
)
from wiki_mcp.wiki.gitrepo import Commit, GitError, GitRepo
from wiki_mcp.wiki.index import SearchIndex
from wiki_mcp.wiki.links import extract_links, markdown_link, resolve_link
from wiki_mcp.wiki.observations import EXTRACTOR as _EXTRACTOR
from wiki_mcp.wiki.observations import Message, ObservationError, base_source
from wiki_mcp.wiki.observations import check as check_observations
from wiki_mcp.wiki.observations import page_id as observation_page
from wiki_mcp.wiki.observations import parse as parse_transcript
from wiki_mcp.wiki.observations import render as render_observations
from wiki_mcp.wiki.observations import title as observations_title
from wiki_mcp.wiki.okf import (
    OKF_VERSION,
    OWNER_WORDS,
    SERVER_KEYS,
    STATUSES,
    ConceptInput,
    Document,
    DocumentError,
    alias_from_title,
    aliases_of,
    canonical_of,
    format_ts,
    is_private,
    name_key,
    now_ts,
    parse_document,
    parse_ts,
    replace_frontmatter,
    safe_yaml_load,
    serialize_document,
    validate_concept_input,
)
from wiki_mcp.wiki.paths import (
    RESERVED_NAMES,
    PathError,
    check_concept_path,
    doc_id,
    normalize_ref,
    resolve_in_bundle,
    slugify,
)
from wiki_mcp.wiki.patterns import Evidence, PatternError, is_pattern_id
from wiki_mcp.wiki.patterns import build as build_pattern
from wiki_mcp.wiki.patterns import evidence as pattern_evidence
from wiki_mcp.wiki.patterns import with_feedback as pattern_with_feedback
from wiki_mcp.wiki.secrets_scan import find_secrets
from wiki_mcp.wiki.types import TypeRegistry

log = logging.getLogger(__name__)

INDEXED_ROOTS = ("wiki", "raw", "context")
PROPOSALS_DIR = "proposals"
LOG_HEADER = "# Wiki Update Log"
# AGENTS.md sections added to the seed after wikis were created with it: startup appends
# them to a wiki's AGENTS.md when missing (the rest of the file is the owner's to edit).
PLAYBOOK_SECTION = "Filing a conversation"
FACTS_SECTION = "Facts and inferences"
IDENTITY_SECTION = "Duplicates and aliases"
GUIDE_SECTIONS = (
    PLAYBOOK_SECTION,
    FACTS_SECTION,
    IDENTITY_SECTION,
    "Complete page merges",
)
# Pages that are private when created, whatever their type's default.
PRIVATE_PAGES = frozenset({PurePosixPath("wiki/owner/personal.md")})
PRIVATE_TRAILER = "Private: yes"  # on every commit that touches a private page
_SINCE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:?\d{2})?)?$")


class WikiError(ValueError):
    """A client-facing error: the message explains what to fix."""


@dataclass
class WriteResult:
    id: str
    path: str
    status: str  # "committed" | "proposed"
    commit: str | None = None
    created: bool = False
    trust: str | None = None
    proposal_id: str | None = None
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "")}


CHAT_SOURCES = {"claude": "Claude.ai", "chatgpt": "ChatGPT"}
CHAT_IMPORT_TAG = "chat-import"  # on imported transcripts
FROM_IMPORT_TAG = "from-chat-import"  # on draft pages written only from them, for review
_CHAT_KEY = re.compile(r"^(claude|chatgpt)-chat:([A-Za-z0-9][A-Za-z0-9-]{0,79})$")
MAX_CHAT_PARTS = 100


_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_HEADER_MESSAGES = re.compile(r"^- Messages: (\d+)$", re.M)


@dataclass(frozen=True)
class ChatSource:
    """One part of an imported chat transcript (the portal renders and redacts it).

    `messages` and `last_message_at` describe the whole conversation as exported, and are
    recorded so a later import can tell whether it continued. A continuation carries only
    the messages after the first `after` ones."""

    key: str  # e.g. claude-chat:<uuid>; becomes the source's `resource`
    title: str
    content: str
    part: int = 1
    parts: int = 1
    messages: int | None = None
    last_message_at: str | None = None  # e.g. 2026-03-01T10:02:00Z
    after: int = 0


@dataclass(frozen=True)
class ChatState:
    """What was imported of a conversation so far (original plus continuations)."""

    messages: int | None  # None: imported before counts were recorded, and no header
    last_message_at: str | None


@dataclass
class ChatImport:
    created: list[tuple[ChatSource, str]] = field(default_factory=list)  # (item, doc id)
    existing: list[ChatSource] = field(default_factory=list)
    rejected: list[tuple[ChatSource, str]] = field(default_factory=list)  # (item, reason)
    commit: str | None = None
    state: dict[str, ChatState] = field(default_factory=dict)  # per key with existing parts


SKIPPED_PAGE = PurePosixPath("wiki/references/skipped-conversations.md")
OWNER_PAGE = PurePosixPath("wiki/owner/profile.md")
IDENTITY = "identity"  # the proposal action of an identity merge (merge_identity)
IDENTITY_OWNER_ONLY = (
    "an identity merge is accepted by the owner only, in the portal (Wiki > Proposals); "
    "ask the user to accept or reject it there"
)
INFERENCE_BANNER = "**Inferred, not stated.**"
_OPEN_LOOP = re.compile(r"^\s*[-*+]\s+\[ \]\s")
_AS_OF = re.compile(r"\(as of (\d{4}-\d{2}(?:-\d{2})?)\)")
_PASS = re.compile(r"^[a-z][a-z0-9-]{0,29}$")
_SET = re.compile(r"\s*\(set \d{4}-\d{2}-\d{2}\)\s*$")
MAX_LISTED_SOURCES = 1000
MAX_FILING_PAGES = 100
MAX_FILING_LINES = 400  # per page
_FOOTNOTE_REF = re.compile(r"\[\^[^\]\s]+\]")


@dataclass(frozen=True)
class FilingSection:
    heading: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class FilingPage:
    """What batch processing files on one page: sections of markdown lines to add, and for a
    page that does not exist yet its type, title and description."""

    path: str
    sections: tuple[FilingSection, ...]
    type: str | None = None
    title: str | None = None
    description: str | None = None
    tags: tuple[str, ...] = ()
    related: tuple[str, ...] = ()
    private: bool = False  # mark private (a new page, or an existing one; never un-marks)


@dataclass
class Filing:
    commit: str | None = None
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    proposed: list[str] = field(default_factory=list)


def _same(before: tuple[Any, ...], after: tuple[Any, ...], fm: dict[str, Any]) -> bool:
    """Whether sources, related and tags are unchanged (after normalisation)."""
    try:
        old = validate_concept_input(
            {**fm, "sources": before[0] or [], "related": before[1] or [], "tags": before[2] or []},
            known_types=[str(fm.get("type"))],
            consolidated=bool(fm.get("merge_history")),
        )
    except DocumentError:
        return False
    return (old.sources, old.related, old.tags) == after


def _line_key(line: str) -> str:
    """A line compared for duplicates: without footnote marks, spacing or case."""
    return " ".join(_FOOTNOTE_REF.sub("", line).split()).lower()


def chat_source_path(key: str, part: int = 1, after: int = 0) -> PurePosixPath:
    """raw/chats/<source>/<conversation id>[-after-N][-part-K].md: fixed per conversation
    (and per continuation, which holds the messages after the first N), so an export
    imported twice is recognised by the path alone."""
    m = _CHAT_KEY.match(key)
    if m is None:
        raise WikiError("'key' must look like claude-chat:<id> or chatgpt-chat:<id>")
    suffix = (f"-after-{after}" if after else "") + (f"-part-{part}" if part > 1 else "")
    return PurePosixPath("raw", "chats", m.group(1), f"{m.group(2).lower()}{suffix}.md")


@dataclass
class _Change:
    rel: PurePosixPath
    text: str | None  # None = delete
    log_kind: str | None = None  # e.g. "Creation", "Update"
    log_title: str = ""
    private: bool = False  # set by _commit: the file is private before or after the change


@dataclass
class _Commit:
    changes: list[_Change] = field(default_factory=list)


def _clean_reason(reason: str) -> str:
    if not isinstance(reason, str):
        raise WikiError("'reason' must be a string")
    reason = " ".join(reason.split())
    if len(reason) < 3:
        raise WikiError("'reason' is required: say briefly why this change is being made")
    return reason[:500]


def _not_found(rel: PurePosixPath) -> WikiError:
    """What a missing page and a private page the caller may not see both answer."""
    return WikiError(f"no page at {rel}; use search or list_concepts to find the right path")


def _check_not_merged(rel: PurePosixPath, fm: dict[str, Any] | None) -> None:
    """Refuse a write to a duplicate: every read of it shows its canonical page instead."""
    target = canonical_of(fm)
    if target is not None:
        raise WikiError(f"{rel} is a duplicate merged into {target}; write to {target} instead")


def _ref_id(ref: Any) -> str | None:
    """The document id a `related` entry or a `sources` item points to, if inside the bundle."""
    if isinstance(ref, dict):
        ref = ref.get("resource")
    if not isinstance(ref, str) or ref.startswith(("http://", "https://")):
        return None
    try:
        return doc_id(normalize_ref(ref))
    except PathError:
        return None


def is_private_commit(commit: Commit, paths: set[str]) -> bool:
    trailers = commit.body.rsplit("\n\n", 1)[-1].splitlines()
    return PRIVATE_TRAILER in trailers or any(p in paths for _, p in commit.files)


def _text_private(text: str) -> bool:
    end = text.find("\n---", 3)
    if end == -1 or "private" not in text[:end]:
        return False  # cheap: transcripts can be large
    try:
        return is_private(parse_document(text).frontmatter)
    except DocumentError:
        return False


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clean_body(body: str) -> str:
    if not isinstance(body, str):
        raise WikiError("'body' must be a string")
    if "\x00" in body:
        raise WikiError("body contains NUL characters")
    return body.replace("\r\n", "\n").strip("\n") + "\n"


class WikiStore:
    def __init__(
        self,
        root: Path,
        index: SearchIndex,
        registry: TypeRegistry,
        *,
        max_file_bytes: int = 256 * 1024,
        max_source_bytes: int = 1024 * 1024,
        wiki_title: str = "Second Brain",
        public_base_url: str | None = None,
        proposal_threshold: Trust | None = Trust.EXTERNAL,
        on_commit: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.root = root
        self.index = index
        self.registry = registry
        self.repo = GitRepo(root)
        self.max_file_bytes = max_file_bytes
        self.max_source_bytes = max_source_bytes
        self.wiki_title = wiki_title
        self.public_base_url = public_base_url
        self.proposal_threshold = proposal_threshold
        self.on_commit = on_commit
        self.lock = asyncio.Lock()
        # When the "Filing a conversation" playbook reached this wiki's AGENTS.md: pages last
        # written before it may be re-processed (list_unprocessed_sources rerun=True).
        self.playbook_at: str | None = None

    # ================================================================ lifecycle

    async def startup(self, seed_dir: Path | None = None) -> None:
        """Initialise the repo from the seed bundle if empty, then rebuild the index."""
        async with self.lock:
            await asyncio.to_thread(self._startup_sync, seed_dir)

    def _startup_sync(self, seed_dir: Path | None) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.repo.is_repo():
            has_content = any(p for p in self.root.iterdir() if p.name != "lost+found")
            if not has_content and seed_dir is not None:
                shutil.copytree(seed_dir, self.root, dirs_exist_ok=True)
                log.info("wiki initialised from seed bundle %s", seed_dir)
            self.repo.init()
        self.index.rebuild(self.iter_documents())
        self._write_file(PurePosixPath("index.md"), self._render_index())
        if self.repo.head() is None:
            name, email = SYSTEM.git_author
            self.repo.commit_all(
                "Initialize wiki from seed bundle", author_name=name, author_email=email
            )
        elif self.repo.is_dirty():
            name, email = SYSTEM.git_author
            self.repo.commit_all(
                "Record out-of-band changes found at startup", author_name=name, author_email=email
            )
        if seed_dir is not None and self._add_guide_sections(seed_dir):
            name, email = SYSTEM.git_author
            self.repo.commit(
                [PurePosixPath("AGENTS.md")],
                "Add new house-rule sections to AGENTS.md",
                author_name=name,
                author_email=email,
            )
        # Rebuild again so metadata reflects anything recorded above.
        self.index.rebuild(self.iter_documents())
        added = self.repo.first_added(PurePosixPath("AGENTS.md"), f"## {PLAYBOOK_SECTION}")
        self.playbook_at = format_ts(parse_ts(added)) if added else None

    def _add_guide_sections(self, seed_dir: Path) -> bool:
        """Append the GUIDE_SECTIONS of the seed's AGENTS.md that this wiki's AGENTS.md lacks.
        Existing text is never changed, so the owner's edits stay."""
        seed, current = seed_dir / "AGENTS.md", self.root / "AGENTS.md"
        if not seed.is_file() or not current.is_file():
            return False
        text = current.read_text(encoding="utf-8")
        seed_text = seed.read_text(encoding="utf-8")
        missing = [
            section
            for name in GUIDE_SECTIONS
            if markdown_section(text, name) is None
            and (section := markdown_section(seed_text, name)) is not None
        ]
        if not missing:
            return False
        self._write_file(
            PurePosixPath("AGENTS.md"), "\n\n".join([text.rstrip("\n"), *missing]) + "\n"
        )
        return True

    # ================================================================ reading

    def iter_documents(self) -> Iterator[tuple[PurePosixPath, Document]]:
        """All indexable documents (wiki/, raw/ and root-level guides)."""
        for path in sorted(self._iter_md_files()):
            rel = PurePosixPath(path.relative_to(self.root).as_posix())
            if rel.name in RESERVED_NAMES or rel.parts[0] == PROPOSALS_DIR:
                continue
            if len(rel.parts) > 1 and rel.parts[0] not in INDEXED_ROOTS:
                continue
            try:
                doc = parse_document(path.read_text(encoding="utf-8"))
            except (DocumentError, UnicodeDecodeError) as exc:
                log.warning("skipping unparsable document %s: %s", rel, exc)
                continue
            yield rel, doc

    def _iter_md_files(self) -> Iterator[Path]:
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                full = Path(dirpath) / name
                if name.endswith(".md") and not name.startswith(".") and not full.is_symlink():
                    yield full

    def all_markdown(self) -> Iterator[tuple[PurePosixPath, str]]:
        """Every markdown file in the bundle with its raw text (for lint)."""
        for path in sorted(self._iter_md_files()):
            rel = PurePosixPath(path.relative_to(self.root).as_posix())
            try:
                yield rel, path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                yield rel, ""

    def resolve(self, ref: str) -> tuple[PurePosixPath, Path]:
        try:
            rel = normalize_ref(ref)
            return rel, resolve_in_bundle(self.root, rel)
        except PathError as exc:
            raise WikiError(str(exc)) from exc

    def read(self, ref: str, actor: Actor | None = None) -> tuple[PurePosixPath, Document, str]:
        """A document as stored. With `actor`, a private one it may not see is not found."""
        rel, path = self.resolve(ref)
        if not path.is_file():
            raise _not_found(rel)
        text = path.read_text(encoding="utf-8")
        try:
            doc = parse_document(text)
        except DocumentError as exc:
            if actor is not None and not actor.sees_private:
                raise _not_found(rel) from exc  # the error would show its frontmatter
            raise WikiError(f"{rel} has malformed frontmatter: {exc}") from exc
        if (
            actor is not None
            and not actor.sees_private
            and (is_private(doc.frontmatter) or self._merge_preview_private(doc.frontmatter or {}))
        ):
            raise _not_found(rel)
        return rel, doc, text

    def read_visible(self, ref: str, actor: Actor) -> tuple[PurePosixPath, Document, str]:
        """A document as `actor` may see it (for display, never for writing back): without
        wiki:private, private pages are not found, and ids of private pages are left out of
        `related` and `sources`, and log.md loses the lines about them."""
        rel, doc, text = self.read(ref, actor)
        if actor.sees_private:
            return rel, doc, text
        if rel == PurePosixPath("log.md"):
            text = self.visible_log(text)
            return rel, Document(doc.frontmatter, self.visible_log(doc.body)), text
        fm = doc.frontmatter
        hidden = self.index.private_ids()
        if not fm or not hidden:
            return rel, doc, text
        shown = dict(fm)
        for key in ("related", "sources"):
            if isinstance(fm.get(key), list):
                shown[key] = [v for v in fm[key] if _ref_id(v) not in hidden]
        if isinstance(fm.get("merge_history"), list):
            shown["merge_history"] = [
                entry for entry in fm["merge_history"] if not self._names_private(str(entry), rel)
            ]
        if shown == fm:
            return rel, doc, text
        return rel, Document(shown, doc.body), serialize_document(shown, doc.body)

    def read_redirected(
        self, ref: str, actor: Actor
    ) -> tuple[PurePosixPath, Document, str, str | None]:
        """read_visible, following a duplicate's `canonical:` redirect (one hop, never a chain):
        the canonical page and the id of the duplicate it was reached from (None when `ref` is
        no duplicate). A duplicate whose canonical page the actor may not see is not found
        either; one whose canonical page is missing is answered itself."""
        rel, doc, text = self.read_visible(ref, actor)
        target = canonical_of(doc.frontmatter)
        if target is None:
            return rel, doc, text, None
        target_rel = PurePosixPath(target + ".md")
        if not self.exists(target_rel):
            return rel, doc, text, None
        if not self._visible_exists(target_rel, actor):
            raise _not_found(rel)
        c_rel, c_doc, c_text = self.read_visible(target, actor)
        return c_rel, c_doc, c_text, doc_id(rel)

    def visible_log(self, text: str) -> str:
        """log.md without the lines linking to a page that is private now (lines written while
        a page was private never name it: see _render_log)."""
        links = tuple(f"](/{did}.md)" for did in self.index.private_ids())
        if not links:
            return text
        return "\n".join(line for line in text.split("\n") if not any(k in line for k in links))

    def is_private_file(self, rel: PurePosixPath) -> bool:
        path = resolve_in_bundle(self.root, rel)
        if not path.is_file():
            return False
        fm, _ = _head(path)
        if fm is None:
            try:
                fm = parse_document(path.read_text(encoding="utf-8")).frontmatter
            except (DocumentError, UnicodeDecodeError):
                return False
        return is_private(fm)

    def private_by_default(self, rel: PurePosixPath, type_name: str | None) -> bool:
        """Whether a new page at `rel` is private when the write does not say."""
        ctype = self.registry.get(type_name or "")
        return (ctype is not None and ctype.private) or rel in PRIVATE_PAGES

    def private_paths(self) -> set[str]:
        """Bundle paths of every private document, pending proposals included."""
        paths = {r.path for r in self.index.all() if r.private}
        paths.update(f"{p['proposal_id']}.md" for p in self.list_proposals() if p.get("private"))
        return paths

    def _check_target(self, rel: PurePosixPath, concept: ConceptInput, actor: Actor) -> None:
        """Refuse a write `actor` may not make: to an existing private page it cannot see
        (without saying why, so the page's existence is not revealed), or `private: false` on
        a new page that would be private by default."""
        if actor.sees_private:
            return
        if self.is_private_file(rel):
            raise WikiError(f"{rel} cannot be written by this connection; choose another path")
        if (
            concept.private is False
            and not self.exists(rel)
            and self.private_by_default(rel, concept.type)
        ):
            raise WikiError(
                f"{doc_id(rel)} is private by default; making it non-private needs the "
                "'wiki:private' permission"
            )

    def _visible_exists(self, rel: PurePosixPath, actor: Actor | None) -> bool:
        if not self.exists(rel):
            return False
        return actor is None or actor.sees_private or not self.is_private_file(rel)

    def exists(self, rel: PurePosixPath) -> bool:
        return resolve_in_bundle(self.root, rel).is_file()

    def owner_names(self) -> set[str]:
        """The owner's names as name_key compares them: the generic OWNER_WORDS ("owner",
        "eigenaar", ...) and the owner profile's title and aliases."""
        builtin = {name_key(n) for n in OWNER_WORDS}
        fm, _ = _head(resolve_in_bundle(self.root, OWNER_PAGE))
        if fm is None:
            return builtin
        title = fm.get("title")
        names = [title] if isinstance(title, str) else []
        return builtin | {name_key(n) for n in [*names, *aliases_of(fm)] if n.strip()}

    def _check_not_owner(self, rel: PurePosixPath, type_name: str | None, title: str) -> None:
        """Refuse a new person page for the owner (exactly one of owner_names: the profile's
        title, one of its aliases or a generic word like "owner"): the owner is wiki/owner/profile,
        never a duplicate under wiki/people."""
        if type_name == "person" and not self.exists(rel) and name_key(title) in self.owner_names():
            raise WikiError(
                f"{title!r} is the owner: file what you know about them on "
                f"{doc_id(OWNER_PAGE)} (personal matters on wiki/owner/personal) instead of "
                f"creating {doc_id(rel)}"
            )

    def page_url(self, did: str) -> str:
        """Citation URL for a page (ChatGPT's search/fetch shape requires one)."""
        if self.public_base_url:
            return f"{self.public_base_url}/page/{did}"
        return f"wiki:{did}"

    def read_text_file(self, name: str) -> str:
        path = self.root / name
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def recent_changes(
        self, since: str | None = None, limit: int = 20, *, private: bool = True
    ) -> list[Commit]:
        """Newest commits first. Without `private`, commits that touched a private page (then
        or now) are left out whole: their subject, reason and files could all tell."""
        if since is not None and not _SINCE.match(since.strip()):
            raise WikiError("'since' must be an ISO date or datetime, e.g. 2026-09-01")
        try:
            commits = self.repo.log(
                since=since.strip() if since else None, limit=limit if private else limit * 5
            )
        except GitError as exc:
            raise WikiError(str(exc)) from exc
        if private:
            return commits
        hidden = self.private_paths()
        return [c for c in commits if not is_private_commit(c, hidden)][:limit]

    def unprocessed_sources(
        self,
        tag: str | None = CHAT_IMPORT_TAG,
        limit: int = 20,
        *,
        rerun: bool = False,
        everything: bool = False,
        private: bool = True,
    ) -> dict[str, Any]:
        """Raw sources (one entry per transcript, with its parts) that no page cites yet.
        With `rerun`, also those whose citing pages all predate the playbook (`filed_at`: the
        latest of those pages' updates), so they can be processed again under its rules. With
        `everything`, every one of them, cited or not (to process them all again)."""
        before = "\uffff" if everything else self.playbook_at if rerun else None
        groups = self.index.uncited_sources(tag=tag or None, cited_before=before, private=private)
        out = []
        for parts, filed_at in groups[: max(1, min(limit, MAX_LISTED_SOURCES))]:
            size = 0
            for row in parts:
                with contextlib.suppress(OSError):
                    size += (self.root / row.path).stat().st_size
            first = parts[0]
            out.append(
                {
                    "id": first.id,
                    "title": first.title,
                    "created": first.created,
                    "bytes": size,
                    "parts": [row.id for row in parts],
                    "private": any(row.private for row in parts),
                    **({"filed_at": filed_at} if filed_at else {}),
                }
            )
        result: dict[str, Any] = {"total": len(groups), "sources": out}
        if rerun:
            result["playbook_since"] = self.playbook_at
        return result

    # ================================================================ trust

    def source_trust(self, raw_id: str, actor: Actor | None = None) -> Trust:
        rel = normalize_ref(raw_id)
        path = resolve_in_bundle(self.root, rel)
        if not self._visible_exists(rel, actor):
            raise WikiError(f"source {raw_id} does not exist; ingest it first with ingest_source")
        fm = parse_document(path.read_text(encoding="utf-8")).frontmatter or {}
        try:
            return Trust(fm.get("trust", "external"))
        except ValueError:
            return Trust.EXTERNAL

    def effective_trust(self, actor: Actor, concept: ConceptInput) -> Trust:
        levels = [actor.trust] + [self.source_trust(s, actor) for s in concept.raw_source_ids()]
        return Trust.lowest(*levels)

    def needs_proposal(self, trust: Trust) -> bool:
        return self.proposal_threshold is not None and trust.rank <= self.proposal_threshold.rank

    # ================================================================ writes

    def _validate_concept(
        self, ref: str, frontmatter: Any, body: str, actor: Actor | None = None
    ) -> tuple[PurePosixPath, ConceptInput, str]:
        try:
            try:
                rel = normalize_ref(ref)
            except PathError:
                # Keep useful content-validation errors first for malformed requests.
                validate_concept_input(
                    frontmatter, known_types=[t.name for t in self.registry.all()]
                )
                raise
            existing = (
                self.read(str(rel))[1].frontmatter if self._visible_exists(rel, actor) else {}
            )
            concept = validate_concept_input(
                frontmatter,
                known_types=[t.name for t in self.registry.all()],
                consolidated=bool((existing or {}).get("merge_history")),
            )
            check_concept_path(rel, self.registry, concept.type)
            resolve_in_bundle(self.root, rel)
        except (DocumentError, PathError) as exc:
            raise WikiError(str(exc)) from exc
        body = _clean_body(body)
        for rid in concept.related:
            if not self._visible_exists(normalize_ref(rid), actor):
                raise WikiError(f"related page {rid} does not exist")
        for sid in concept.raw_source_ids():
            self.source_trust(sid, actor)  # raises if missing
        return rel, concept, body

    def _default_import_status(self, rel: PurePosixPath, concept: ConceptInput) -> None:
        """A page written without a status from imported chats alone is bulk processing that
        nobody reads along with: a new page becomes a draft tagged for review, an existing one
        keeps its status (never demoted) and, while still under review, its tag."""
        sids = concept.raw_source_ids()
        rows = [self.index.get(sid) for sid in sids]
        if not sids or not all(r is not None and CHAT_IMPORT_TAG in r.tags for r in rows):
            return
        path = resolve_in_bundle(self.root, rel)
        if path.is_file():
            existing = parse_document(path.read_text(encoding="utf-8")).frontmatter or {}
            status = existing.get("status", "stable")
            concept.status = status if status in STATUSES else "stable"
            tags = existing.get("tags")
            if not (isinstance(tags, list) and FROM_IMPORT_TAG in tags):
                return  # reviewed already, or never from imports: not put back on the list
        else:
            concept.status = "draft"
        if FROM_IMPORT_TAG not in concept.tags and len(concept.tags) < 20:
            concept.tags.append(FROM_IMPORT_TAG)

    def _build_concept(
        self,
        rel: PurePosixPath,
        concept: ConceptInput,
        body: str,
        actor: Actor,
        trust: Trust,
        *,
        verified_by: str | None = None,
    ) -> tuple[str, bool]:
        existing: dict[str, Any] = {}
        path = resolve_in_bundle(self.root, rel)
        created = not path.is_file()
        if not created:
            existing = parse_document(path.read_text(encoding="utf-8")).frontmatter or {}
            _check_not_merged(rel, existing)
        now = now_ts()
        fm: dict[str, Any] = {
            "type": concept.type,
            "id": doc_id(rel),
            "title": concept.title,
            "description": concept.description,
        }
        # Sticky like `private`: an accepted identity merge adds them, a rewrite keeps them.
        aliases = concept.aliases if concept.aliases is not None else aliases_of(existing)
        if aliases:
            fm["aliases"] = aliases
        if concept.resource:
            fm["resource"] = concept.resource
        fm["tags"] = concept.tags
        fm["status"] = concept.status
        private = concept.private
        if private is None:  # sticky: an existing page keeps its marker
            private = (
                is_private(existing) if not created else self.private_by_default(rel, concept.type)
            )
        if private:
            fm["private"] = True
        fm["generated"] = {"by": actor.generated_by, "at": now}
        if verified_by:
            fm["verified"] = [{"by": verified_by, "at": now}]
        if concept.stale_after:
            fm["stale_after"] = concept.stale_after
        if concept.sources:
            fm["sources"] = concept.sources
        if concept.related:
            fm["related"] = concept.related
        fm["trust"] = trust.value
        fm["created"] = existing.get("created", now)
        fm["updated"] = now
        fm.update(concept.extra)
        if existing.get("merge_history"):
            fm["merge_history"] = existing["merge_history"]
        text = serialize_document(fm, body)
        self._check_size(text, self.max_file_bytes)
        self._check_secrets(text)
        return text, created

    def _check_size(self, text: str, limit: int) -> None:
        size = len(text.encode("utf-8"))
        if size > limit:
            raise WikiError(
                f"document is {size} bytes; the limit is {limit}. Split it into several pages."
            )

    def _check_secrets(self, text: str) -> None:
        found = find_secrets(text)
        if found:
            raise WikiError(
                "refusing to store what looks like a credential ("
                + ", ".join(found)
                + "). Never put secrets in the wiki; describe where the secret is kept instead."
            )

    async def write_concept(
        self, ref: str, frontmatter: Any, body: str, reason: str, actor: Actor
    ) -> WriteResult:
        reason = _clean_reason(reason)
        rel, concept, body = self._validate_concept(ref, frontmatter, body, actor)
        self._check_target(rel, concept, actor)
        self._check_not_owner(rel, concept.type, concept.title)
        if self._visible_exists(rel, actor):
            _check_not_merged(rel, self.read(str(rel))[1].frontmatter)
        if isinstance(frontmatter, dict) and "status" not in frontmatter:
            self._default_import_status(rel, concept)
        trust = self.effective_trust(actor, concept)
        if self.needs_proposal(trust):
            return await self.create_proposal(
                "write",
                rel,
                {"frontmatter": frontmatter, "body": body},
                reason,
                actor,
                trust,
                title=concept.title,
            )
        async with self.lock:
            self._check_target(rel, concept, actor)  # again: it may have changed meanwhile
            text, created = self._build_concept(rel, concept, body, actor, trust)
            sha = await self._commit(
                [_Change(rel, text, "Creation" if created else "Update", concept.title)],
                f"{'Create' if created else 'Update'} {rel}",
                reason,
                actor,
                trust,
            )
        return WriteResult(doc_id(rel), str(rel), "committed", sha, created, trust.value)

    async def append_to_concept(
        self, ref: str, section: str, content: str, reason: str, actor: Actor
    ) -> WriteResult:
        reason = _clean_reason(reason)
        section = " ".join(str(section).split()).lstrip("#").strip()
        if not section or len(section) > 120:
            raise WikiError("'section' must be a heading text of 1-120 characters, e.g. 'Notes'")
        content = _clean_body(content)
        rel, doc, _ = self.read(ref, actor)
        ctype = self.registry.get(doc.type or "")
        if ctype is None or ctype.managed:
            raise WikiError(f"{rel} is not an editable concept page")
        fm = doc.frontmatter or {}
        _check_not_merged(rel, fm)
        concept = validate_concept_input(
            {k: v for k, v in fm.items()},
            known_types=[t.name for t in self.registry.all()],
            consolidated=bool(fm.get("merge_history")),
        )
        try:
            page_trust = Trust(fm.get("trust", "agent"))
        except ValueError:
            page_trust = Trust.EXTERNAL
        # The appended text comes from the actor; the page keeps the lowest trust it has seen.
        trust = Trust.lowest(actor.trust, page_trust)
        if self.needs_proposal(actor.trust):
            return await self.create_proposal(
                "append",
                rel,
                {"section": section, "content": content},
                reason,
                actor,
                actor.trust,
                title=doc.title,
            )
        async with self.lock:
            rel, doc, _ = self.read(ref, actor)  # re-read under the lock
            new_body = insert_into_section(doc.body, section, content)
            text, _ = self._build_concept(rel, concept, new_body, actor, trust)
            sha = await self._commit(
                [_Change(rel, text, "Update", doc.title)],
                f"Append to {rel} § {section}",
                reason,
                actor,
                trust,
            )
        return WriteResult(doc_id(rel), str(rel), "committed", sha, False, trust.value)

    async def link_concepts(
        self, src: str, dst: str, relation: str, reason: str, actor: Actor
    ) -> WriteResult:
        reason = _clean_reason(reason)
        relation = " ".join(str(relation).split())
        if not relation or len(relation) > 60:
            raise WikiError("'relation' must be 1-60 characters, e.g. 'works at' or 'depends on'")
        async with self.lock:
            rel, doc, _ = self.read(src, actor)
            dst_rel, dst_doc, _ = self.read(dst, actor)
            ctype = self.registry.get(doc.type or "")
            if ctype is None or ctype.managed:
                raise WikiError(f"{rel} is not an editable concept page")
            if rel == dst_rel:
                raise WikiError("cannot link a page to itself")
            _check_not_merged(rel, doc.frontmatter)
            if is_private(dst_doc.frontmatter) and not is_private(doc.frontmatter):
                raise WikiError(
                    "cannot link a non-private page to a private one: the link would show it"
                )
            fm = dict(doc.frontmatter or {})
            dst_id = doc_id(dst_rel)
            related = [str(r) for r in fm.get("related", []) if isinstance(r, str)]
            if dst_id not in related:
                related.append(dst_id)
            fm["related"] = related
            bullet = f"- {relation}: {markdown_link(dst_doc.title or dst_rel.stem, dst_rel)}"
            body = (
                doc.body if bullet in doc.body else insert_into_section(doc.body, "Related", bullet)
            )
            fm["updated"] = now_ts()
            fm["generated"] = {"by": actor.generated_by, "at": fm["updated"]}
            text = serialize_document(fm, body)
            self._check_size(text, self.max_file_bytes)
            sha = await self._commit(
                [_Change(rel, text, "Update", doc.title)],
                f"Link {rel} -> {dst_rel} ({relation})",
                reason,
                actor,
                actor.trust,
            )
        return WriteResult(doc_id(rel), str(rel), "committed", sha, False, actor.trust.value)

    async def ingest_source(
        self,
        title: str,
        content: str,
        source_url: str | None,
        trust: str | None,
        actor: Actor,
        *,
        private: bool = False,
        subfolder: str = "",
        extra: dict[str, Any] | None = None,
    ) -> WriteResult:
        """Store a raw source. `subfolder` files it under raw/<subfolder>/<date>/<slug>.md
        instead of raw/<date>-<slug>.md; `extra` adds frontmatter (provenance, tags)."""
        title = " ".join(str(title).split())
        if not title or len(title) > 200:
            raise WikiError("'title' must be 1-200 characters")
        try:
            level = Trust(trust) if trust else Trust.EXTERNAL
        except ValueError as exc:
            raise WikiError("'trust' must be external, agent or owner") from exc
        if level.rank > actor.trust.rank:
            raise WikiError(
                f"this client may record sources with trust up to '{actor.trust.value}'"
            )
        if source_url is not None:
            source_url = source_url.strip() or None
            if source_url and (
                not source_url.startswith(("http://", "https://")) or len(source_url) > 1000
            ):
                raise WikiError("'source_url' must be an http(s) URL")
        body = _clean_body(content)
        self._check_secrets(body)
        async with self.lock:
            rel, text = self._new_source(
                title,
                body,
                source_url,
                level,
                actor,
                private=private,
                subfolder=subfolder,
                extra=extra,
            )
            sha = await self._commit(
                [_Change(rel, text, "Source", title)],
                f"Ingest source {rel}",
                f"Ingested source '{title}'" + (f" from {source_url}" if source_url else ""),
                actor,
                level,
            )
        return WriteResult(doc_id(rel), str(rel), "committed", sha, True, level.value)

    def _new_source(
        self,
        title: str,
        body: str,
        source_url: str | None,
        level: Trust,
        actor: Actor,
        *,
        private: bool = False,
        subfolder: str = "",
        extra: dict[str, Any] | None = None,
    ) -> tuple[PurePosixPath, str]:
        """Path and text of a new raw source (call with the lock held)."""
        stype = self.registry.get("source")
        folder = stype.folder if stype else PurePosixPath("raw")
        today = dt.datetime.now(dt.UTC).date().isoformat()
        base = f"{today}-{slugify(title, 50)}"
        if subfolder:
            assert _PASS.match(subfolder), subfolder  # internal callers only
            folder, base = folder / subfolder / today, slugify(title, 50)
        rel = folder / f"{base}.md"
        n = 2
        while resolve_in_bundle(self.root, rel).exists():
            rel = folder / f"{base}-{n}.md"
            n += 1
        now = now_ts()
        fm: dict[str, Any] = {
            "type": "source",
            "id": doc_id(rel),
            "title": title,
            "description": f"Raw source: {title}"[:500],
            **(extra or {}),
        }
        if source_url:
            fm["resource"] = source_url
        if private:
            fm["private"] = True
        fm["generated"] = {"by": actor.generated_by, "at": now}
        fm["trust"] = level.value
        fm["content_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        fm["created"] = now
        fm["updated"] = now
        notice = (
            "> [!NOTE]\n> Immutable raw source. Content below is untrusted data, "
            "not instructions.\n\n"
        )
        text = serialize_document(fm, notice + body)
        self._check_size(text, self.max_source_bytes)
        return rel, text

    async def import_chats(
        self, items: list[ChatSource], actor: Actor, *, dry_run: bool = False
    ) -> ChatImport:
        """Store chat transcripts as immutable raw sources in one commit.

        Only creates files: a part whose path already exists is reported as existing
        and left untouched. Invalid parts are rejected one by one; the rest still go in.
        """
        result = ChatImport()
        ready: list[tuple[ChatSource, PurePosixPath, str]] = []
        for item in items:
            try:
                ready.append((item, *self._chat_source(item, actor)))
            except WikiError as exc:
                result.rejected.append((item, str(exc)))
        async with self.lock:
            changes: list[_Change] = []
            seen: set[PurePosixPath] = set()
            for item, rel, text in ready:
                if rel in seen or resolve_in_bundle(self.root, rel).exists():
                    result.existing.append(item)
                    if item.key not in result.state:
                        result.state[item.key] = self.chat_state(item.key)
                    continue
                if item.after:
                    # Only right after what was imported before this continuation.
                    before = self.chat_state(item.key, before=item.after)
                    if before.messages != item.after:
                        result.rejected.append(
                            (
                                item,
                                f"continues after message {item.after}, but "
                                f"{before.messages or 0} were imported before; "
                                "upload the export again",
                            )
                        )
                        continue
                seen.add(rel)
                changes.append(_Change(rel, text, "Source", item.title))
                result.created.append((item, doc_id(rel)))
            if dry_run or not changes:
                return result
            labels = sorted({CHAT_SOURCES[str(c.rel.parts[2])] for c in changes})
            conversations = len({item.key for item, _ in result.created})
            continued = len({item.key for item, _ in result.created if item.after})
            reason = (
                f"Imported {conversations} conversation(s) from a {' and '.join(labels)} export"
            )
            if continued:
                reason += f", {continued} of them continued since the last import"
            result.commit = await self._commit(
                changes,
                f"Import {conversations} chat conversation(s)",
                reason,
                actor,
                actor.trust,
            )
        return result

    def chat_state(self, key: str, *, before: int | None = None) -> ChatState:
        """Messages imported so far of a conversation: the most recorded by its original
        source or its continuations (only those starting before message `before`). Sources
        imported before counts were recorded fall back to their header's message count."""
        original = chat_source_path(key)
        folder = resolve_in_bundle(self.root, original).parent
        pattern = re.compile(re.escape(original.stem) + r"-after-(\d+)\.md$")
        found: list[tuple[int, Path]] = [(0, folder / original.name)]
        if folder.is_dir():
            for path in folder.glob(f"{original.stem}-after-*.md"):
                m = pattern.fullmatch(path.name)
                if m and (before is None or int(m.group(1)) < before):
                    found.append((int(m.group(1)), path))
        best = ChatState(None, None)
        for _after, path in sorted(found):
            fm, rest = _head(path)
            if fm is None:
                continue
            count = fm.get("messages")
            if not isinstance(count, int) or isinstance(count, bool):
                header = _HEADER_MESSAGES.search(rest)
                count = int(header.group(1)) if header else None
            if count is not None and (best.messages is None or count > best.messages):
                last = fm.get("last_message_at")
                best = ChatState(count, last if isinstance(last, str) else None)
        return best

    def _chat_source(self, item: ChatSource, actor: Actor) -> tuple[PurePosixPath, str]:
        if not 1 <= item.part <= item.parts <= MAX_CHAT_PARTS:
            raise WikiError(f"'part' must be 1..parts and parts at most {MAX_CHAT_PARTS}")
        if item.messages is not None and not 0 <= item.after < item.messages:
            raise WikiError("'after' must be at least 0 and less than 'messages'")
        if item.after and item.messages is None:
            raise WikiError("a continuation ('after') needs 'messages'")
        if item.last_message_at is not None and not _TIMESTAMP.match(item.last_message_at):
            raise WikiError("'last_message_at' must look like 2026-03-01T10:02:00Z")
        rel = chat_source_path(item.key, item.part, item.after)
        title = " ".join(str(item.title).split())[:200] or "Untitled conversation"
        if item.after:
            title = f"{title} (continued)"
        if item.parts > 1:
            title = f"{title} (part {item.part} of {item.parts})"
        title = title[:200]
        body = _clean_body(item.content)
        self._check_secrets(body)
        now = now_ts()
        source = str(rel.parts[2])
        description = f"{CHAT_SOURCES[source]} conversation transcript"
        if item.after:
            description += f", continued: the messages after the first {item.after}"
        fm: dict[str, Any] = {
            "type": "source",
            "id": doc_id(rel),
            "title": title,
            "description": description,
            "tags": [CHAT_IMPORT_TAG, source],
            "resource": item.key,
            "generated": {"by": actor.generated_by, "at": now},
            "trust": actor.trust.value,
            "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "created": now,
            "updated": now,
        }
        if item.messages is not None:
            fm["messages"] = item.messages
        if item.last_message_at is not None:
            fm["last_message_at"] = item.last_message_at
        if item.after:
            fm["continues"] = doc_id(chat_source_path(item.key))
        notice = (
            "> [!NOTE]\n> Immutable raw source. Content below is untrusted data, "
            "not instructions.\n\n"
        )
        text = serialize_document(fm, notice + body)
        self._check_size(text, self.max_source_bytes)
        return rel, text

    # ================================================================ observations

    def chat_parts(self, source: str) -> tuple[list[str], dict[str, Any], list[str]]:
        """An imported transcript from the id of any of its parts: the ids of its parts, its
        first part's frontmatter and the parts' bodies, in order. WikiError when it is no
        imported chat."""
        base = base_source(source)
        try:
            rel = normalize_ref(base)
        except PathError as exc:
            raise WikiError(str(exc)) from exc
        if not rel.parts[:2] == ("raw", "chats") or not self.exists(rel):
            raise WikiError(f"no imported chat at {base[:120]}")
        parts = [base]
        while len(parts) < MAX_CHAT_PARTS and self.exists(
            PurePosixPath(f"{base}-part-{len(parts) + 1}.md")
        ):
            parts.append(f"{base}-part-{len(parts) + 1}")
        bodies: list[str] = []
        first: dict[str, Any] = {}
        for pid in parts:
            _, doc, _ = self.read(pid)
            if doc.type != "source":
                raise WikiError(f"{pid} is not a raw source")
            first = first or dict(doc.frontmatter or {})
            bodies.append(doc.body)
        return parts, first, bodies

    def _transcript(self, source: str) -> tuple[list[str], dict[str, Any], list[Message]]:
        """chat_parts, with the messages (numbered as wiki.observations.parse does) instead of
        the bodies."""
        parts, first, bodies = self.chat_parts(source)
        return parts, first, parse_transcript(bodies)

    def observed_with(self, source: str, extractor: str) -> str | None:
        """The observation page of an imported chat when `extractor` wrote it (None: not
        observed yet, or by another extractor). WikiError when `source` is no chat's id."""
        try:
            pid = observation_page(source)
        except ObservationError as exc:
            raise WikiError(str(exc)) from exc
        if self.index.get(pid) is None or self._extractor_of(pid) != extractor:
            return None
        return pid

    async def write_observations(
        self,
        source: Any,
        extractor: Any,
        payload: Any,
        actor: Actor,
        *,
        observed_by: str | None = None,
    ) -> dict[str, Any]:
        """Write the observation page of an imported chat (wiki.observations): what its checks
        keep of `payload`, in one commit with a neutral message; the raw source is never
        touched. Nothing is written when the page exists from the same extractor (`exists`):
        another extractor's page is replaced whole. `observed_by`: the assistant whose model
        observed the chat (the observe tools), kept on the page; None for the portal's."""
        if not isinstance(extractor, str) or not _EXTRACTOR.match(extractor):
            raise WikiError("'extractor' must be a short version name, e.g. observe-1")
        if not isinstance(source, str):
            raise WikiError("'source' must be a raw source id")
        if observed_by is not None:
            check_dream_client(observed_by)
        try:
            pid = observation_page(source)
        except ObservationError as exc:
            raise WikiError(str(exc)) from exc
        rel = PurePosixPath(pid + ".md")
        async with self.lock:
            old: dict[str, Any] = {}
            if self.exists(rel):
                old = dict(self.read(pid)[1].frontmatter or {})
                if old.get("extractor") == extractor:
                    return {"status": "exists", "id": pid}
            parts, src, messages = self._transcript(source)
            try:
                checked = check_observations(payload, messages)
            except ObservationError as exc:
                raise WikiError(str(exc)) from exc
            now = now_ts()
            dates = [m.at for m in messages if m.at]
            label = CHAT_SOURCES.get(parts[0].split("/")[2], "Imported")
            fm: dict[str, Any] = {
                "type": "observation",
                "id": pid,
                "title": observations_title(label, dates[0] if dates else None),
                "description": "What the owner's own messages in one imported chat show: "
                "evidence for personal patterns, not facts.",
                "tags": ["observation"],
                "status": "draft",
                "private": True,
                "generated": {"by": actor.generated_by, "at": now},
                "trust": Trust.AGENT.value,
                "created": old.get("created", now),
                "updated": now,
                "source": parts[0],
                **({"parts": parts} if len(parts) > 1 else {}),
                "conversation": str(src.get("resource") or parts[0]),
                "extractor": extractor,
                "observed": now[:10],
                **({"observed_by": observed_by} if observed_by is not None else {}),
                **({"messages_from": min(dates), "messages_to": max(dates)} if dates else {}),
                "classification": checked.classification,
                "observations": checked.observations,
            }
            text = serialize_document(fm, render_observations(fm))
            self._check_size(text, self.max_file_bytes)
            self._check_secrets(text)
            sha = await self._commit(
                [_Change(rel, text, "Update" if old else "Creation", "observations")],
                "Record observations of an imported chat",
                f"Observations of the owner's own messages ({extractor})",
                actor,
                actor.trust,
            )
        return {
            "status": "committed",
            "id": pid,
            "commit": sha,
            "observations": len(checked.observations),
            "dropped": checked.dropped,
            "adjusted": checked.adjusted,
        }

    def observations_pending(self, limit: int = 100, outdated: str | None = None) -> dict[str, Any]:
        """Imported transcripts (oldest first, with their parts) that have no observation page
        yet; with `outdated` (an extractor version), also those whose page another extractor
        wrote. An observation page never counts as processing a chat (uncited_sources)."""
        out: list[dict[str, Any]] = []
        total = 0
        for parts, _ in self.index.uncited_sources(tag=CHAT_IMPORT_TAG, cited_before="￿"):
            first = parts[0]
            try:
                pid = observation_page(first.id)
            except ObservationError:
                continue
            observed = self.index.get(pid) is not None
            if observed and (outdated is None or self._extractor_of(pid) == outdated):
                continue
            total += 1
            if len(out) < limit:
                out.append(
                    {
                        "id": first.id,
                        "parts": [row.id for row in parts],
                        "created": first.created,
                        "observed": observed,
                    }
                )
        return {"total": total, "sources": out}

    def _extractor_of(self, pid: str) -> str | None:
        try:
            value = (self.read(pid)[1].frontmatter or {}).get("extractor")
        except (WikiError, DocumentError):
            return None
        return value if isinstance(value, str) else None

    def _pages_of(self, keep: Callable[[Any], bool]) -> list[tuple[str, dict[str, Any]]]:
        out = []
        for row in self.index.all():
            if not keep(row):
                continue
            try:
                out.append((row.id, dict(self.read(row.id)[1].frontmatter or {})))
            except (WikiError, DocumentError):
                continue
        return out

    def observation_pages(self) -> list[tuple[str, dict[str, Any]]]:
        """Every observation page: (id, frontmatter)."""
        return self._pages_of(lambda row: row.type == "observation")

    def pattern_pages(self) -> list[tuple[str, dict[str, Any]]]:
        """Every personal pattern page (wiki/inferred/patterns/): (id, frontmatter)."""
        return self._pages_of(lambda row: row.type == "inference" and is_pattern_id(row.id))

    def pattern_evidence(self) -> Evidence:
        """Every observation, clustered into events, and the coverage (wiki.patterns)."""
        return pattern_evidence(self.observation_pages())

    async def pattern_feedback(
        self, ref: str, verdict: str, note: str, actor: Actor
    ) -> WriteResult:
        """The owner's verdict on a personal pattern (agree, dispute, correct, unsure), with a
        note: added to its `feedback` and its Owner feedback section, in one commit with a
        neutral message. Never evidence for any pattern (wiki.patterns)."""
        if actor.trust is not Trust.OWNER:
            raise WikiError("only the owner gives feedback on patterns")
        if not isinstance(note, str) or not isinstance(verdict, str):
            raise WikiError("'verdict' and 'note' must be strings")
        async with self.lock:
            rel, doc, _ = self.read(ref)
            if doc.type != "inference" or not is_pattern_id(doc_id(rel)):
                raise WikiError(f"{doc_id(rel)} is not a personal pattern")
            fm = {k: v for k, v in (doc.frontmatter or {}).items()}
            try:
                fm, body = pattern_with_feedback(fm, verdict, note, now_ts(), _history(doc.body))
            except PatternError as exc:
                raise WikiError(str(exc)) from exc
            fm["updated"] = now_ts()
            text = serialize_document(fm, body)
            self._check_size(text, self.max_file_bytes)
            self._check_secrets(text)
            sha = await self._commit(
                [_Change(rel, text, "Update", "pattern")],
                "Record owner feedback on a pattern",
                "Owner feedback given in the portal",
                actor,
                Trust.OWNER,
            )
        return WriteResult(doc_id(rel), str(rel), "committed", sha, trust=Trust.OWNER.value)

    async def file_conversation(
        self,
        sources: list[str],
        pages: list[FilingPage],
        reason: str,
        actor: Actor,
        *,
        skip: str | None = None,
        replace: bool = False,
        private_sources: bool = False,
    ) -> Filing:
        """File one processed conversation in a single commit, so it counts as processed
        exactly when all its pages are written.

        New pages are drafts tagged for review; existing pages only get lines appended to
        their sections (lines already there are left out), the conversation added to their
        `sources`, their `related` extended and the review tag added, and keep their status.
        With `skip` (the reason), the conversation instead gets one line on the
        skipped-conversations page. Writes that need a proposal become proposals.

        With `replace` (processing a conversation again), what it contributed before is taken
        out first, in the same commit: the lines citing it (`[^<id>]`) on pages that are still
        waiting for review, and its line on the skipped-conversations page. Pages the owner has
        reviewed are never edited; they are flagged for review again instead.

        Pages with `private` are created private, or marked private when they exist (never
        un-marked here). With `private_sources`, the sources are marked private too (only that
        frontmatter key changes). The skip line of a private conversation shows neither its
        title nor the reason, only the link that marks it processed."""
        reason = _clean_reason(reason)
        if not 1 <= len(sources) <= MAX_CHAT_PARTS:
            raise WikiError(f"'sources' must list 1 to {MAX_CHAT_PARTS} raw source ids")
        cited: list[tuple[str, str]] = []  # (id, title)
        secret = private_sources
        for ref in sources:
            rel, doc, _ = self.read(ref, actor)
            if doc.type != "source":
                raise WikiError(f"{rel} is not a raw source")
            cited.append((doc_id(rel), doc.title or rel.stem))
            secret = secret or is_private(doc.frontmatter)
        if skip is not None:
            why = " ".join(str(skip).split())[:300] or "nothing to file"
            first_id, first_title = cited[0]
            target = PurePosixPath(first_id + ".md")
            line = (
                f"- {markdown_link('Private conversation', target)}"
                if secret
                else f"- {markdown_link(first_title, target)}: {why}"
            )
            pages = [
                FilingPage(
                    str(SKIPPED_PAGE),
                    (FilingSection("Skipped", (line,)),),
                    type="reference",
                    title="Skipped conversations",
                    description="Imported conversations with nothing worth filing.",
                )
            ]
        strip: dict[PurePosixPath, tuple[str, ...]] = {}
        if replace:
            marks = tuple(f"[^{sid.rsplit('/', 1)[-1]}]" for sid, _ in cited)
            links = tuple(f"(/{sid}.md)" for sid, _ in cited)
            listed: set[PurePosixPath] = set()
            for p in pages:
                with contextlib.suppress(PathError):
                    listed.add(normalize_ref(p.path))
            for sid, _ in cited:
                for row in self.index.backlinks(sid, private=actor.sees_private):
                    ctype = self.registry.get(row.type or "")
                    rel = PurePosixPath(row.path)
                    if ctype is None or ctype.managed or row.canonical:
                        continue  # a merged duplicate is never written again
                    strip[rel] = links if rel == SKIPPED_PAGE else marks
                    if rel not in listed:
                        listed.add(rel)
                        pages = [*pages, FilingPage(str(rel), ())]
        if not 1 <= len(pages) <= MAX_FILING_PAGES:
            raise WikiError(f"file 1 to {MAX_FILING_PAGES} pages per conversation")
        result = Filing()
        proposals: list[tuple[PurePosixPath, dict[str, Any], str, Trust, str]] = []
        async with self.lock:
            targets: list[tuple[FilingPage, PurePosixPath, bool]] = []
            for page in pages:
                try:
                    rel = normalize_ref(page.path)
                except PathError as exc:
                    raise WikiError(str(exc)) from exc
                if rel in [r for _, r, _ in targets]:
                    raise WikiError(f"{rel} is listed twice")
                if not actor.sees_private and self.is_private_file(rel):
                    raise WikiError(
                        f"{rel} cannot be written by this connection; choose another path"
                    )
                self._check_not_owner(rel, page.type, page.title or "")
                targets.append((page, rel, self.exists(rel)))
            new_ids = {doc_id(rel) for _, rel, exists in targets if not exists}
            changes: list[_Change] = []
            trusts: list[Trust] = []
            for page, rel, exists in targets:
                review = rel != SKIPPED_PAGE
                ids = [sid for sid, _ in cited] if review else []
                built = self._filed_page(
                    page, rel, exists, ids, new_ids, review, strip.get(rel, ()), actor
                )
                if built is None:
                    result.unchanged.append(doc_id(rel))
                    continue
                concept, fm_in, body = built
                trust = self.effective_trust(actor, concept)
                if self.needs_proposal(trust):
                    proposals.append((rel, fm_in, body, trust, concept.title))
                    continue
                text, created = self._build_concept(rel, concept, body, actor, trust)
                changes.append(
                    _Change(rel, text, "Creation" if created else "Update", concept.title)
                )
                (result.created if created else result.updated).append(doc_id(rel))
                trusts.append(trust)
            if private_sources:
                for sid, title in cited:
                    marked = self._with_private(PurePosixPath(sid + ".md"), True)
                    if marked is not None:
                        changes.append(_Change(PurePosixPath(sid + ".md"), marked, "Update", title))
                        trusts.append(actor.trust)
            if changes:
                result.commit = await self._commit(
                    changes,
                    f"File conversation {cited[0][0]}",
                    reason,
                    actor,
                    Trust.lowest(*trusts),
                )
        for rel, fm_in, body, trust, title in proposals:
            res = await self.create_proposal(
                "write",
                rel,
                {"frontmatter": fm_in, "body": body},
                reason,
                actor,
                trust,
                title=title,
            )
            result.proposed.append(str(res.proposal_id))
        return result

    def _filed_page(
        self,
        page: FilingPage,
        rel: PurePosixPath,
        exists: bool,
        sources: list[str],
        new_ids: set[str],
        review: bool,
        strip: tuple[str, ...] = (),
        actor: Actor | None = None,
    ) -> tuple[ConceptInput, dict[str, Any], str] | None:
        """The concept and body of one filed page, or None when nothing would change. Lines
        containing one of `strip` are taken out first, on a page still waiting for review (or
        the skipped-conversations page)."""
        lines = sum(len(s.lines) for s in page.sections)
        if lines > MAX_FILING_LINES:
            raise WikiError(f"{rel}: at most {MAX_FILING_LINES} lines per page")
        if exists:
            _, doc, _ = self.read(str(rel))
            ctype = self.registry.get(doc.type or "")
            if ctype is None or ctype.managed:
                raise WikiError(f"{rel} is not an editable concept page")
            fm = {k: v for k, v in (doc.frontmatter or {}).items() if k not in SERVER_KEYS}
            body = original = doc.body
            unreviewed = FROM_IMPORT_TAG in (fm.get("tags") or []) or not review
            if strip and unreviewed:
                kept = [line for line in body.split("\n") if not any(s in line for s in strip)]
                body = re.sub(r"\n{3,}", "\n\n", "\n".join(kept))
        else:
            if not (page.type and page.title and page.description):
                raise WikiError(f"{rel} is new: give its type, title and description")
            fm = {
                "type": page.type,
                "title": page.title,
                "description": page.description,
                "tags": list(page.tags),
                "status": "draft" if review else "stable",
            }
            body = f"# {' '.join(page.title.split())}\n\n{' '.join(page.description.split())}\n"
            original = body
        before = (
            original,
            fm.get("sources"),
            fm.get("related"),
            fm.get("tags"),
        )
        marked = page.private and not is_private(fm)
        if page.private:
            fm["private"] = True
        fm["sources"] = [*(fm.get("sources") or []), *sources]
        fm["related"] = [*(fm.get("related") or []), *page.related]
        if review:
            fm["tags"] = [*(fm.get("tags") or []), FROM_IMPORT_TAG]
        seen = {_line_key(line) for line in body.split("\n")}
        for section in page.sections:
            heading = " ".join(section.heading.split()).lstrip("#").strip()
            if not heading or len(heading) > 120:
                raise WikiError(f"{rel}: a section heading must be 1-120 characters")
            fresh = []
            for line in section.lines:
                line = _clean_body(str(line)).strip("\n")
                key = _line_key(line)
                if key and key not in seen and len(line) <= 4000:
                    seen.add(key)
                    fresh.append(line)
            if fresh:
                body = insert_into_section(body, heading, "\n".join(fresh))
        try:
            concept = validate_concept_input(
                fm,
                known_types=[t.name for t in self.registry.all()],
                consolidated=bool(fm.get("merge_history")),
            )
            check_concept_path(rel, self.registry, concept.type)
        except (DocumentError, PathError) as exc:
            raise WikiError(f"{rel}: {exc}") from exc
        for rid in concept.related:
            if rid not in new_ids and not self._visible_exists(normalize_ref(rid), actor):
                raise WikiError(f"{rel}: related page {rid} does not exist")
        concept.related = [r for r in concept.related if r != doc_id(rel)]
        after = (body, concept.sources, concept.related, concept.tags)
        if exists and not marked and after[0] == before[0] and _same(before[1:], after[1:], fm):
            return None
        fm_in = {**fm, "sources": concept.sources, "related": concept.related, "tags": concept.tags}
        return concept, fm_in, _clean_body(body)

    def _with_private(self, rel: PurePosixPath, private: bool) -> str | None:
        """The file at `rel` with `private` set (true) or removed (false), or None when that
        changes nothing. Only the frontmatter is rewritten; the body stays byte for byte."""
        text = resolve_in_bundle(self.root, rel).read_text(encoding="utf-8")
        fm = dict(parse_document(text).frontmatter or {})
        if is_private(fm) == private and (private or "private" not in fm):
            return None
        fm.pop("private", None)
        if private:
            fm["private"] = True
        return replace_frontmatter(text, fm)

    async def set_private(self, ref: str, private: bool, reason: str, actor: Actor) -> WriteResult:
        """Mark a concept page or raw source private, or un-mark it. Marking needs only write
        access, on an item the actor sees (not private yet: a private one is not found without
        wiki:private); un-marking needs wiki:private. Raw sources are otherwise immutable: only
        this key changes, so their content hash holds."""
        reason = _clean_reason(reason)
        if not isinstance(private, bool):
            raise WikiError("'private' must be true or false")
        if not private and not actor.sees_private:
            raise WikiError("this needs the 'wiki:private' permission")
        async with self.lock:
            rel, doc, _ = self.read(ref, actor)
            ctype = self.registry.get(doc.type or "")
            if ctype is None or (ctype.managed and doc.type != "source"):
                raise WikiError(f"{rel} is not a concept page or raw source")
            text = self._with_private(rel, private)
            if text is None:
                return WriteResult(doc_id(rel), str(rel), "unchanged")
            self._check_size(
                text, self.max_source_bytes if doc.type == "source" else self.max_file_bytes
            )
            sha = await self._commit(
                [_Change(rel, text, "Update", doc.title)],
                f"{'Mark' if private else 'Unmark'} {rel} private",
                reason,
                actor,
                actor.trust,
            )
        return WriteResult(doc_id(rel), str(rel), "committed", sha)

    async def write_managed(
        self,
        rel: PurePosixPath,
        frontmatter: dict[str, Any],
        body: str,
        reason: str,
        actor: Actor,
    ) -> WriteResult:
        """Server-generated pages (maintenance reports). No client input reaches here."""
        async with self.lock:
            created = not resolve_in_bundle(self.root, rel).exists()
            text = serialize_document(frontmatter, body)
            sha = await self._commit(
                [
                    _Change(
                        rel, text, "Creation" if created else "Update", frontmatter.get("title", "")
                    )
                ],
                f"{'Create' if created else 'Update'} {rel}",
                reason,
                actor,
                actor.trust,
            )
        return WriteResult(doc_id(rel), str(rel), "committed", sha, created, actor.trust.value)

    # ================================================================ review and the owner

    @staticmethod
    def is_reviewed(fm: dict[str, Any]) -> bool:
        """Checked by the owner: status stable, without the from-chat-import review tag."""
        tags = fm.get("tags")
        tagged = isinstance(tags, list) and FROM_IMPORT_TAG in tags
        return fm.get("status", "stable") == "stable" and not tagged

    async def review_page(self, ref: str, actor: Actor) -> WriteResult:
        """Mark a page reviewed: status stable, the from-chat-import tag removed, and the
        owner's verification recorded. The body is left as it is."""
        async with self.lock:
            rel, doc, _ = self.read(ref)
            ctype = self.registry.get(doc.type or "")
            if ctype is None or ctype.managed:
                raise WikiError(f"{rel} is not a concept page; only those are reviewed")
            fm = dict(doc.frontmatter or {})
            if self.is_reviewed(fm):
                return WriteResult(doc_id(rel), str(rel), "unchanged", message="already reviewed")
            now = now_ts()
            fm["status"] = "stable"
            fm["tags"] = [t for t in fm.get("tags") or [] if t != FROM_IMPORT_TAG]
            verified = [v for v in fm.get("verified") or [] if isinstance(v, dict)]
            fm["verified"] = [*verified, {"by": f"human:{actor.human or 'owner'}", "at": now}]
            fm["updated"] = now
            sha = await self._commit(
                [_Change(rel, serialize_document(fm, doc.body), "Review", doc.title)],
                f"Review {rel}",
                "Marked reviewed by the owner in the portal",
                actor,
                actor.trust,
            )
        return WriteResult(doc_id(rel), str(rel), "committed", sha, trust=actor.trust.value)

    def _owner_page(self) -> tuple[dict[str, Any], str]:
        """The owner profile's frontmatter and body; a new page when there is none yet."""
        path = resolve_in_bundle(self.root, OWNER_PAGE)
        if path.is_file():
            doc = parse_document(path.read_text(encoding="utf-8"))
            return dict(doc.frontmatter or {}), doc.body
        fm: dict[str, Any] = {
            "type": "owner",
            "id": doc_id(OWNER_PAGE),
            "title": "Owner profile",
            "description": "The owner: preferences, background and goals.",
            "status": "stable",
            "trust": Trust.OWNER.value,
            "created": now_ts(),
        }
        return fm, "# Owner profile\n"

    async def owner_goals(self, add: list[str], drop: list[str], actor: Actor) -> WriteResult:
        """The owner's goals on wiki/owner/profile § Goals. New ones are appended as
        `- <goal> (set YYYY-MM-DD)`; a dropped one is marked superseded, never deleted.
        The active goals are the lines of the section without that mark."""
        if actor.trust is not Trust.OWNER:
            raise WikiError("only the owner sets goals")
        today = dt.datetime.now(dt.UTC).date().isoformat()
        fresh = []
        for goal in add:
            text = " ".join(str(goal).split()).lstrip("-*+ ").strip()
            if not 3 <= len(text) <= 300:
                raise WikiError("a goal must be 3-300 characters")
            fresh.append(text)
        async with self.lock:
            fm, body = self._owner_page()
            before = body
            goals = bullet_lines(markdown_section(body, "Goals") or "")
            for line in drop:
                if line not in goals or superseded(line) is not None:
                    raise WikiError(f"not an active goal: {line[:100]}")
                body = replace_line(body, line, mark_superseded(line, today, "dropped by owner"))
            known = {_goal_key(g) for g in goals if superseded(g) is None}
            lines = [f"- {g} (set {today})" for g in fresh if _goal_key(f"- {g}") not in known]
            if lines:
                body = insert_into_section(body, "Goals", "\n".join(lines))
            if body == before:
                return WriteResult(doc_id(OWNER_PAGE), str(OWNER_PAGE), "unchanged")
            problems = guard_bullets(before, body)
            if problems:
                raise WikiError(problems[0])
            text = self._owner_text(fm, body, actor)
            sha = await self._commit(
                [_Change(OWNER_PAGE, text, "Update", str(fm.get("title") or "Owner profile"))],
                f"Set goals on {OWNER_PAGE}",
                f"Goals set by the owner in the portal: {len(lines)} added, {len(drop)} dropped",
                actor,
                Trust.OWNER,
            )
        return WriteResult(doc_id(OWNER_PAGE), str(OWNER_PAGE), "committed", sha, trust="owner")

    async def owner_statement(self, text: str, section: str, actor: Actor) -> WriteResult:
        """Something the owner states about himself: stored as a raw source with trust owner,
        and added as a dated line citing it to a section of wiki/owner/profile, in one commit."""
        if actor.trust is not Trust.OWNER:
            raise WikiError("only the owner makes owner statements")
        text = " ".join(str(text).split())
        section = " ".join(str(section).split()).lstrip("#").strip() or "Facts"
        if not 3 <= len(text) <= 1000:
            raise WikiError("a statement must be 3-1000 characters")
        if len(section) > 120:
            raise WikiError("'section' must be a heading text of 1-120 characters")
        self._check_secrets(text)
        today = dt.datetime.now(dt.UTC).date().isoformat()
        async with self.lock:
            short = text if len(text) <= 60 else text[:57].rstrip() + "..."
            title = f"Owner statement {today}: {short}"
            src_rel, src_text = self._new_source(title, text + "\n", None, Trust.OWNER, actor)
            fm, body = self._owner_page()
            sid = src_rel.stem
            body = insert_into_section(body, section, f"- {today}: {text} [^{sid}]")
            sources = [s for s in fm.get("sources") or [] if s]
            fm["sources"] = [*sources, {"id": sid, "resource": "/" + str(src_rel)}]
            page = self._owner_text(fm, body, actor)
            sha = await self._commit(
                [
                    _Change(src_rel, src_text, "Source", title),
                    _Change(OWNER_PAGE, page, "Update", str(fm.get("title") or "Owner profile")),
                ],
                f"Owner statement on {OWNER_PAGE} § {section}",
                "Stated by the owner in the portal",
                actor,
                Trust.OWNER,
            )
        return WriteResult(doc_id(OWNER_PAGE), str(OWNER_PAGE), "committed", sha, trust="owner")

    def _owner_text(self, fm: dict[str, Any], body: str, actor: Actor) -> str:
        now = now_ts()
        fm = {**fm, "updated": now, "generated": {"by": actor.generated_by, "at": now}}
        text = serialize_document(fm, _clean_body(body))
        self._check_size(text, self.max_file_bytes)
        self._check_secrets(text)
        return text

    def briefing(self, *, private: bool = False) -> dict[str, Any]:
        """The hot-memory briefing a dream wrote (wiki/briefing/hot-memory), with when; when
        there is none yet, a message pointing to the owner profile instead. With `private`
        (wiki:private), the variant built from private pages too, when there is one."""
        page = BRIEFING_PAGE
        path = resolve_in_bundle(self.root, normalize_ref(BRIEFING_PAGE))
        variant = resolve_in_bundle(self.root, normalize_ref(BRIEFING_PRIVATE_PAGE))
        if private and variant.is_file():
            page, path = BRIEFING_PRIVATE_PAGE, variant
        if not path.is_file():
            return {
                "markdown": None,
                "message": "No briefing has been generated yet. Read wiki/owner/profile for "
                "who the user is (read_concept), and search the wiki for the rest.",
            }
        doc = parse_document(path.read_text(encoding="utf-8"))
        fm = doc.frontmatter or {}
        return {
            "markdown": doc.body,
            "generated": fm.get("updated"),
            "dream": fm.get("dream"),
            "page": page,
            "message": "Stated facts only, generated by the wiki's last dream; (unreviewed) "
            "marks facts from pages the user has not reviewed. Check details in the wiki.",
        }

    def open_loops(self) -> list[dict[str, Any]]:
        """Every unchecked `- [ ]` line on stable pages and on the owner profile (whatever its
        status), without the superseded ones; `reviewed` says whether the page is."""
        out: list[dict[str, Any]] = []
        for row in self.index.all():
            ctype = self.registry.get(row.type or "")
            if not row.path.startswith("wiki/") or ctype is None or ctype.managed or row.canonical:
                continue
            if (row.status or "stable") != "stable" and row.path != str(OWNER_PAGE):
                continue
            try:
                doc = parse_document((self.root / row.path).read_text(encoding="utf-8"))
            except (OSError, DocumentError):
                continue
            reviewed = self.is_reviewed(doc.frontmatter or {})
            for line in bullet_lines(doc.body):
                if _OPEN_LOOP.match(line) and superseded(line) is None:
                    as_of = _AS_OF.search(line)
                    out.append(
                        {
                            "page": row.id,
                            "title": row.title,
                            "status": row.status or "stable",
                            "reviewed": reviewed,
                            "private": row.private,
                            "line": line.strip(),
                            "date": line_date(line) or (as_of.group(1) if as_of else None),
                        }
                    )
        return out

    # ================================================================ identities

    def _identity_pair(
        self, duplicate: str, canonical: str, actor: Actor, *, migration: bool = False
    ) -> tuple[PurePosixPath, Document, str, PurePosixPath, Document, str]:
        """The two pages of an identity merge, as `actor` may see them; WikiError when they may
        not be merged: the same page, not concept pages, the duplicate merged already (or the
        owner profile, always canonical), the canonical page itself merged or
        other pages merged into the duplicate (no chains), or one private and the other not."""
        d_rel, d_doc, d_text = self.read(duplicate, actor)
        c_rel, c_doc, c_text = self.read(canonical, actor)
        if d_rel == c_rel:
            raise WikiError("cannot merge a page into itself")
        for rel, doc in ((d_rel, d_doc), (c_rel, c_doc)):
            ctype = self.registry.get(doc.type or "")
            if ctype is None or ctype.managed:
                raise WikiError(f"{rel} is not a concept page; only those are merged")
        d_fm, c_fm = d_doc.frontmatter or {}, c_doc.frontmatter or {}
        if migration and canonical_of(d_fm) is None:
            raise WikiError("content migration requires a previously accepted redirect-only merge")
        if d_rel == OWNER_PAGE:
            raise WikiError(f"{doc_id(OWNER_PAGE)} is always the canonical page of the owner")
        if (merged := canonical_of(d_fm)) is not None and not (
            migration and merged == doc_id(c_rel) and not d_fm.get("merge_consolidated")
        ):
            raise WikiError(f"{d_rel} is merged into {merged} already")
        # A deprecated duplicate without `canonical` (a hand-made redirect page) may be merged.
        if (hop := canonical_of(c_fm)) is not None:
            raise WikiError(
                f"{c_rel} is itself merged into {hop}: merge into {hop} instead (no chains)"
            )
        if self.index.merged_into(doc_id(d_rel), private=actor.sees_private):
            raise WikiError(
                f"other pages are merged into {d_rel}, so it stays canonical (no chains)"
            )
        if is_private(d_fm) != is_private(c_fm):
            raise WikiError(
                "cannot merge a private page with a non-private one: the private one would "
                "show through the other, or the other would vanish for most apps"
            )
        return d_rel, d_doc, d_text, c_rel, c_doc, c_text

    def _identity_payload(
        self, duplicate: str, canonical: str, actor: Actor, *, migration: bool = False
    ) -> dict[str, Any]:
        d_rel, d_doc, d_text, c_rel, c_doc, c_text = self._identity_pair(
            duplicate, canonical, actor, migration=migration
        )
        base = {doc_id(d_rel): fingerprint(d_text), doc_id(c_rel): fingerprint(c_text)}
        provenance = {
            "base": base,
            "commits": {
                doc_id(d_rel): self._page_sha(doc_id(d_rel)),
                doc_id(c_rel): self._page_sha(doc_id(c_rel)),
            },
        }
        preview = consolidate(d_doc, c_doc, doc_id(d_rel), doc_id(c_rel), provenance)
        self._check_size(preview, self.max_file_bytes)
        self._check_secrets(preview)
        return {
            "duplicate": doc_id(d_rel),
            "canonical": doc_id(c_rel),
            "duplicate_title": d_doc.title or d_rel.stem,
            "canonical_title": c_doc.title or c_rel.stem,
            "migration": migration,
            "base": base,
            "base_commits": provenance["commits"],
            "preview": preview,
            "review_token": fingerprint(preview),
            "private_preview": is_private(d_doc.frontmatter) or self._names_private(preview, c_rel),
        }

    async def propose_identity_merge(
        self,
        duplicate: str,
        canonical: str,
        reason: str,
        actor: Actor,
        *,
        consolidate_merged: bool = False,
    ) -> WriteResult:
        """Preview a complete consolidation; only the owner approves in the portal.
        consolidate_merged explicitly reviews a previously accepted redirect-only merge."""
        reason = _clean_reason(reason)
        payload = self._identity_payload(duplicate, canonical, actor, migration=consolidate_merged)
        d_rel = normalize_ref(payload["duplicate"])
        for pending in self.list_proposals():
            if pending.get("action") == IDENTITY and pending.get("target") == str(d_rel):
                raise WikiError(
                    f"a merge of {d_rel} is proposed already ({pending.get('proposal_id')})"
                )
        result = await self.create_proposal(
            IDENTITY,
            d_rel,
            payload,
            reason,
            actor,
            actor.trust,
            title=f"{payload['duplicate_title']} is {payload['canonical_title']}",
            private=payload["private_preview"],
        )
        result.message = "Stored as a consolidation preview: the owner reviews it in the portal (Wiki > Proposals)."
        return result

    async def refresh_identity_preview(self, proposal_id: str, actor: Actor) -> WriteResult:
        """Explicit renewed review, including old proposals that have no content preview."""
        if actor.trust is not Trust.OWNER or not actor.human:
            raise WikiError(IDENTITY_OWNER_ONLY)
        async with self.lock:
            rel, fm = self._load_proposal(proposal_id, actor)
            if (fm.get("proposal") or {}).get("action") != IDENTITY:
                raise WikiError("only identity merges have a consolidation preview")
            old = fm.get("payload") or {}
            payload = self._identity_payload(
                old.get("duplicate", ""),
                old.get("canonical", ""),
                actor,
                migration=old.get("migration") is True,
            )
            fm["payload"], fm["updated"] = payload, now_ts()
            fm["private"] = payload["private_preview"]
            sha = await self._commit(
                [
                    _Change(
                        rel,
                        serialize_document(
                            fm, _identity_summary(payload, str(fm.get("description") or ""), actor)
                        ),
                        "Proposal",
                        str(fm.get("title") or "Merge preview"),
                    )
                ],
                f"Refresh merge preview {proposal_id}",
                "Renewed owner review",
                actor,
                Trust.OWNER,
            )
        return WriteResult(doc_id(rel), str(rel), "proposed", sha, proposal_id=doc_id(rel))

    def _identity_changes(
        self, payload: dict[str, Any], actor: Actor, review_token: str | None = None
    ) -> list[_Change]:
        """Apply exactly the reviewed snapshot, with both pages checked under the write lock."""
        duplicate, canonical = (
            str(payload.get("duplicate") or ""),
            str(payload.get("canonical") or ""),
        )
        d_rel, d_doc, d_text, c_rel, c_doc, c_text = self._identity_pair(
            duplicate, canonical, actor, migration=payload.get("migration") is True
        )
        if not payload.get("preview") or not payload.get("base"):
            raise WikiError("merge needs a consolidation preview; refresh it and review again")
        if payload["base"] != {
            doc_id(d_rel): fingerprint(d_text),
            doc_id(c_rel): fingerprint(c_text),
        }:
            raise WikiError("page changed after the preview; refresh it and review again")
        if payload.get("base_commits") != {
            doc_id(d_rel): self._page_sha(doc_id(d_rel)),
            doc_id(c_rel): self._page_sha(doc_id(c_rel)),
        }:
            raise WikiError("page changed after the preview; refresh it and review again")
        preview = str(payload["preview"])
        if payload.get("review_token") != fingerprint(preview) or (
            review_token is not None and review_token != payload["review_token"]
        ):
            raise WikiError("preview changed; review the renewed preview again")
        for page, text in ((d_rel, d_text), (c_rel, c_text)):
            if self.repo.committed_text(page) != text:
                raise WikiError(f"{page}: commit page edits before refreshing the merge preview")
        now = now_ts()
        d_fm = {
            k: v
            for k, v in (d_doc.frontmatter or {}).items()
            if k in ("id", "type", "title", "private", "created")
        }
        d_fm.update(
            status="deprecated", canonical=doc_id(c_rel), updated=now, merge_consolidated=True
        )
        redirect = serialize_document(d_fm, f"Merged into [canonical page](/{doc_id(c_rel)}.md).\n")
        c_preview = parse_document(preview)
        c_fm = dict(c_preview.frontmatter or {})
        c_fm["updated"] = now
        text = serialize_document(c_fm, c_preview.body)
        changes = [
            _Change(d_rel, redirect, "Update", d_doc.title),
            _Change(c_rel, text, "Update", c_doc.title),
        ]
        for change in changes:
            self._check_size(str(change.text), self.max_file_bytes)
        return changes

    def proposal_action(self, proposal_id: str, actor: Actor) -> str:
        """The action of a pending proposal `actor` may see (write, append, dream, identity)."""
        _, fm = self._load_proposal(proposal_id, actor)
        return str((fm.get("proposal") or {}).get("action") or "")

    # ================================================================ dreams

    def _page_sha(self, pid: str) -> str | None:
        """The last commit of a page (None when it does not exist): a dream proposal's base."""
        try:
            rel = normalize_ref(pid)
        except PathError as exc:
            raise WikiError(str(exc)) from exc
        return self.repo.last_commit(rel) if self.exists(rel) else None

    def _dream_changes(
        self,
        ops: list[Op],
        allowed: frozenset[str],
        dream: int,
        actor: Actor,
        *,
        verified_by: str | None = None,
    ) -> list[_Change]:
        """The pages the operations change, computed in memory (call with the lock held).
        WikiError when any operation may not be applied."""
        by_page: dict[str, list[Op]] = {}
        for op in ops:
            if op.op not in allowed:
                raise WikiError(f"operation {op.op} is not allowed here")
            by_page.setdefault(op.page, []).append(op)
        changes: list[_Change] = []
        evidence: Evidence | None = None  # read once, for the first pattern
        for pid, page_ops in by_page.items():
            try:
                rel = normalize_ref(pid)
                resolve_in_bundle(self.root, rel)
            except PathError as exc:
                raise WikiError(str(exc)) from exc
            whole = [
                o for o in page_ops if o.op in ("write_managed", "write_inference", "write_pattern")
            ]
            if whole and len(page_ops) > 1:
                raise WikiError(f"{pid}: a page written whole takes no other operation")
            change: _Change | None
            if whole and whole[0].op == "write_managed":
                change = self._managed_change(rel, whole[0], dream, actor)
            elif whole and whole[0].op == "write_pattern":
                evidence = evidence or self.pattern_evidence()
                change = self._pattern_change(rel, whole[0], dream, actor, evidence)
            elif whole:
                change = self._inference_change(rel, whole[0], dream, actor)
            else:
                hygiene = allowed == DIRECT_OPS
                change = self._edited_change(rel, page_ops, actor, verified_by, hygiene)
            if change is not None:
                changes.append(change)
        if allowed == PROPOSAL_OPS:
            private = {self._private_after(c) for c in changes}
            if len(private) > 1:
                raise WikiError(
                    "a dream proposal cannot change private and non-private pages together; "
                    "what comes from a private page stays on private pages"
                )
        return changes

    def _private_after(self, change: _Change) -> bool:
        return self.is_private_file(change.rel) or (
            change.text is not None and _text_private(change.text)
        )

    def _private_ids(self) -> set[str]:
        """Ids of every private page and pending private proposal."""
        return {p.removesuffix(".md") for p in self.private_paths()}

    def _names_private(self, body: str, rel: PurePosixPath) -> bool:
        """Whether a body links to, or names the id of, a private page or proposal."""
        hidden = self._private_ids()
        if not hidden:
            return False
        if any(t in hidden for t in extract_links(body, rel)):
            return True
        return any(re.search(rf"(?<![\w.-]){re.escape(h)}(?![\w/-])", body) for h in hidden)

    def _managed_change(self, rel: PurePosixPath, op: Op, dream: int, actor: Actor) -> _Change:
        """A briefing or a dream log, written whole (regenerated each dream)."""
        kind = "briefing" if doc_id(rel) in BRIEFING_PAGES else "maintenance-report"
        ctype = self.registry.get(kind)
        if ctype is None or self.registry.for_path(rel) != ctype:
            raise WikiError(f"{rel}: not a {kind} page")
        body = _clean_body(op.body)
        if kind == "briefing" and any(is_inference_id(t) for t in extract_links(body, rel)):
            raise WikiError(f"{rel}: the briefing never links to inferences")
        path = resolve_in_bundle(self.root, rel)
        old = parse_document(path.read_text(encoding="utf-8")).frontmatter if path.is_file() else {}
        # Every client reads the public briefing: it may not name a private page. The private
        # variant always is private; a dream log is when it names one, or the portal says so.
        if doc_id(rel) == BRIEFING_PAGE:
            private = False
            if self._names_private(body, rel):
                raise WikiError(f"{rel}: the briefing for every client names a private page")
        elif doc_id(rel) == BRIEFING_PRIVATE_PAGE:
            private = True
        else:
            private = (
                op.frontmatter.get("private") is True
                or is_private(old)
                or self._names_private(body, rel)
            )
        now = now_ts()
        title = _fm_text(op.frontmatter, "title", 200) or rel.stem
        fm: dict[str, Any] = {
            "type": kind,
            "id": doc_id(rel),
            "title": title,
            "description": _fm_text(op.frontmatter, "description", 500) or title,
            "tags": ["dream"],
            "status": "stable",
            **({"private": True} if private else {}),
            "generated": {"by": actor.generated_by, "at": now},
            "trust": actor.trust.value,
            "created": (old or {}).get("created", now),
            "updated": now,
            "dream": dream,
        }
        text = serialize_document(fm, body)
        self._check_size(text, self.max_file_bytes)
        self._check_secrets(text)
        return _Change(rel, text, "Update" if old else "Creation", title)

    def _inference_change(
        self, rel: PurePosixPath, op: Op, dream: int, actor: Actor
    ) -> _Change | None:
        """An inference page, created or updated whole. It must rest on at least two stated
        lines of existing pages that are not inferences, claim no more confidence than they
        allow, and say what it is in its first line."""
        if is_pattern_id(doc_id(rel)):
            raise WikiError(f"{rel}: personal patterns change only through write_pattern")
        fm_in = op.frontmatter
        title, desc = _fm_text(fm_in, "title", 200), _fm_text(fm_in, "description", 500)
        kind, confidence = fm_in.get("kind"), fm_in.get("confidence")
        speculative, status = fm_in.get("speculative", False), fm_in.get("status", "stable")
        supports, counter = fm_in.get("supports"), fm_in.get("counterexamples") or []
        about = fm_in.get("about") or []
        if not (title and desc) or kind not in ("induction", "deduction"):
            raise WikiError(f"{rel}: an inference needs title, description and kind")
        if confidence not in ("low", "medium", "high") or not isinstance(speculative, bool):
            raise WikiError(f"{rel}: confidence must be low, medium or high; speculative a bool")
        if status not in ("stable", "deprecated"):
            raise WikiError(f"{rel}: status must be stable or deprecated")
        if not isinstance(about, list) or not all(isinstance(a, str) for a in about):
            raise WikiError(f"{rel}: 'about' must be a list of page ids")
        if not isinstance(supports, list) or len(supports) < 2 or not isinstance(counter, list):
            raise WikiError(f"{rel}: an inference needs at least 2 supports")
        lines = [
            *(self._evidence(rel, s) for s in supports),
            *(self._evidence(rel, c) for c in counter),
        ]
        if above(confidence, rubric(supports, counter)):
            raise WikiError(f"{rel}: confidence {confidence} is above what the supports allow")
        body = _clean_body(op.body)
        first = opening(body)
        if speculative and (not first.startswith(SPECULATIVE_BANNER) or confidence != "low"):
            raise WikiError(f"{rel}: a speculative inference is marked so and has confidence low")
        if not speculative and not first.startswith(INFERENCE_BANNER):
            raise WikiError(f"{rel}: an inference opens with {INFERENCE_BANNER}")
        path = resolve_in_bundle(self.root, rel)
        old_fm: dict[str, Any] = {}
        if path.is_file():
            old = parse_document(path.read_text(encoding="utf-8"))
            if old.type != "inference":
                raise WikiError(f"{rel} exists and is not an inference")
            problems = guard_bullets(old.body, body)
            if problems:
                raise WikiError(f"{rel}: {problems[0]}")
            old_fm = old.frontmatter or {}
        # What rests on (or is about) a private page is private itself; never un-marked here.
        hidden = self._private_ids()
        cited = [e["page"] for e in lines] + [a.strip("/").removesuffix(".md") for a in about]
        private = (
            fm_in.get("private") is True
            or is_private(old_fm)
            or any(p in hidden for p in cited)
            or self._names_private(body, rel)
        )
        now = now_ts()
        fm: dict[str, Any] = {
            "type": "inference",
            "id": doc_id(rel),
            "title": title,
            "description": desc,
            "tags": ["inferred"],
            "status": status,
            **({"private": True} if private else {}),
            "generated": {"by": actor.generated_by, "at": now},
            "trust": Trust.AGENT.value,
            "created": old_fm.get("created", now),
            "updated": now,
            "kind": kind,
            "confidence": confidence,
            "speculative": speculative,
            "about": [a.strip("/").removesuffix(".md") for a in about],
            "supports": lines[: len(supports)],
            "counterexamples": lines[len(supports) :],
            "dream": dream,
        }
        stale = fm_in.get("stale_after")
        if isinstance(stale, str) and stale:
            fm["stale_after"] = stale
        text = serialize_document(fm, body)
        self._check_size(text, self.max_file_bytes)
        self._check_secrets(text)
        return _Change(rel, text, "Update" if old_fm else "Creation", title)

    def _pattern_change(
        self, rel: PurePosixPath, op: Op, dream: int, actor: Actor, evidence: Evidence
    ) -> _Change:
        """A personal pattern page, created, updated or retired whole (wiki.patterns.build):
        its events, span, confidence and body computed from the observation pages, and the
        owner's feedback kept as it was."""
        path = resolve_in_bundle(self.root, rel)
        old: dict[str, Any] | None = None
        history: list[str] = []
        if path.is_file():
            doc = parse_document(path.read_text(encoding="utf-8"))
            if doc.type != "inference":
                raise WikiError(f"{rel} exists and is not an inference")
            old, history = dict(doc.frontmatter or {}), _history(doc.body)
        today = dt.datetime.now(dt.UTC).date()
        try:
            fm, body = build_pattern(
                doc_id(rel), op.frontmatter, old, history, evidence, dream, today
            )
        except PatternError as exc:
            raise WikiError(str(exc)) from exc
        now = now_ts()
        fm = {
            **fm,
            "generated": {"by": actor.generated_by, "at": now},
            "trust": Trust.AGENT.value,
            "created": (old or {}).get("created", now),
            "updated": now,
        }
        text = serialize_document(fm, body)
        self._check_size(text, self.max_file_bytes)
        self._check_secrets(text)
        return _Change(rel, text, "Update" if old else "Creation", "pattern")

    def _evidence(self, rel: PurePosixPath, item: Any) -> dict[str, str]:
        """A support or counterexample: a stated line, quoted exactly, on an existing page that
        is not an inference, citing a raw source."""
        if not isinstance(item, dict):
            raise WikiError(f"{rel}: each support is {{page, quote, source}}")
        page = str(item.get("page") or "").strip("/").removesuffix(".md")
        quote, source = str(item.get("quote") or "").strip(), str(item.get("source") or "")
        if is_inference_id(page):
            raise WikiError(f"{rel}: an inference never rests on another inference ({page})")
        try:
            _, doc, _ = self.read(page)
        except WikiError as exc:
            raise WikiError(f"{rel}: support page {page or '?'} does not exist") from exc
        stated = [line.strip() for line in bullet_lines(doc.body)]
        if not quote or quote not in stated or "[^" not in quote:
            raise WikiError(f"{rel}: not a stated line of {page}: {quote[:100]}")
        return {"page": page, "quote": quote, "source": source.strip("/").removesuffix(".md")}

    def _edited_change(
        self,
        rel: PurePosixPath,
        ops: list[Op],
        actor: Actor,
        verified_by: str | None,
        hygiene: bool = False,
    ) -> _Change | None:
        """A reviewed page with the operations applied; its bullet lines all stay (a direct
        hygiene commit may also make the rewrites of _hygiene_rewrite)."""
        path = resolve_in_bundle(self.root, rel)
        if not path.is_file():
            raise WikiError(f"no page at {rel}")
        doc = parse_document(path.read_text(encoding="utf-8"))
        ctype = self.registry.get(doc.type or "")
        retire = ctype is not None and ctype.name == "inference"
        if is_pattern_id(doc_id(rel)):
            raise WikiError(f"{rel}: personal patterns change only through write_pattern")
        if retire and any(o.op != "deprecate" for o in ops):
            raise WikiError(f"{rel}: inference pages change only through write_inference")
        if ctype is None or (ctype.managed and not retire):
            raise WikiError(f"{rel} is not an editable concept page")
        fm = dict(doc.frontmatter or {})
        if not retire and not self.is_reviewed(fm):
            raise WikiError(
                f"{rel} is not reviewed (status stable without the {FROM_IMPORT_TAG} tag); "
                "dreams leave it alone"
            )
        body = doc.body
        for op in ops:
            body = self._apply_op(rel, op, fm, body)
        allow = (lambda old, new: self._hygiene_rewrite(rel, old, new)) if hygiene else None
        problems = guard_bullets(doc.body, body, allow)
        if problems:
            raise WikiError(f"{rel}: {problems[0]}")
        if not is_private(fm):
            added = set(extract_links(body, rel)) - set(extract_links(doc.body, rel))
            if added and added & self._private_ids():
                raise WikiError(f"{rel}: a non-private page cannot link to a private one")
        if body == doc.body and fm == (doc.frontmatter or {}):
            return None
        now = now_ts()
        fm["updated"] = now
        fm["generated"] = {"by": actor.generated_by, "at": now}
        if verified_by:
            verified = [v for v in fm.get("verified") or [] if isinstance(v, dict)]
            fm["verified"] = [*verified, {"by": verified_by, "at": now}]
        text = serialize_document(fm, _clean_body(body))
        self._check_size(text, self.max_file_bytes)
        self._check_secrets(text)
        return _Change(rel, text, "Update", doc.title)

    def _hygiene_rewrite(self, rel: PurePosixPath, old: str, new: str) -> bool:
        """Whether `new` is `old` fixed in one of the ways hygiene may (wiki.facts): a doubled
        checkbox collapsed; on the owner profile, the owner's pronouns; or a broken link sent to
        the only page git history shows its target was renamed to."""
        if old != new and collapse_checkboxes(old) == new:
            return True
        if rel == OWNER_PAGE and pronouns_only(old, new):
            return True
        swap = swapped_link(old, new)
        if swap is None:
            return False
        was, now = resolve_link(swap[0], rel), resolve_link(swap[1], rel)
        if was is None or now is None or self.exists(PurePosixPath(was + ".md")):
            return False
        if not self.exists(PurePosixPath(now + ".md")):
            return False
        return self.repo.renamed_to(PurePosixPath(was + ".md")) == {now + ".md"}

    def _apply_op(self, rel: PurePosixPath, op: Op, fm: dict[str, Any], body: str) -> str:
        """One operation on a page's frontmatter (in place) and body (returned)."""
        if op.op == "append":
            return insert_into_section(body, op.section, op.line)
        if op.op == "supersede":
            if op.line.rstrip() not in [line.rstrip() for line in bullet_lines(body)]:
                raise WikiError(f"{rel}: no such line: {op.line[:120]}")
            try:
                marked = mark_superseded(op.line.rstrip(), op.date, op.reason)
            except ValueError as exc:
                raise WikiError(f"{rel}: {exc}") from exc
            return replace_line(body, op.line.rstrip(), marked)
        if op.op == "replace":
            if body.count(op.old) != 1:
                raise WikiError(f"{rel}: the text to replace must occur exactly once")
            return body.replace(op.old, op.new, 1)
        if op.op == "add_sources":
            sources = [s for s in fm.get("sources") or [] if s]
            have = {s.get("resource") for s in sources if isinstance(s, dict)}
            for sid in op.ids:
                srel, sdoc, _ = self.read(sid)
                if srel.parts[0] != "raw" or sdoc.type != "source":
                    raise WikiError(f"{rel}: {sid} is not a raw source")
                if "/" + str(srel) not in have:
                    sources.append({"id": srel.stem, "resource": "/" + str(srel)})
                    have.add("/" + str(srel))
            fm["sources"] = sources
            return body
        if op.op == "add_related":
            related = [str(r) for r in fm.get("related") or []]
            for rid in op.ids:
                other, odoc, _ = self.read(rid)
                if is_private(odoc.frontmatter) and not is_private(fm):
                    raise WikiError(f"{rel}: a non-private page cannot relate to a private one")
                if doc_id(other) not in related and other != rel:
                    related.append(doc_id(other))
            fm["related"] = related
            return body
        if op.op == "deprecate":
            fm["status"] = "deprecated"
            return prepend_note(body, op.note)
        raise WikiError(f"operation {op.op} cannot be applied to {rel}")

    async def dream_commit(
        self,
        dream: int,
        pass_: str,
        reason: str,
        raw_ops: Any,
        *,
        client: str | None = None,
    ) -> tuple[str | None, list[str]]:
        """A dream's direct changes of one pass, as one commit with a `Dream: <id>` trailer:
        the commit (None when nothing changed) and the pages changed. `client`: the assistant
        an assistant dream runs in (a `Dream-client` trailer; None for the portal's)."""
        reason = _clean_reason(reason)
        _check_dream(dream, pass_)
        if client is not None:
            check_dream_client(client)
        try:
            ops = parse_ops(raw_ops, DIRECT_OPS)
        except OpError as exc:
            raise WikiError(str(exc)) from exc
        async with self.lock:
            changes = self._dream_changes(ops, DIRECT_OPS, dream, DREAM)
            if not changes:
                return None, []
            sha = await self._commit(
                changes,
                f"Dream {dream}: {pass_}",
                reason,
                DREAM,
                Trust.AGENT,
                extra_trailers={
                    "Dream": str(dream),
                    "Dream-pass": pass_,
                    **({"Dream-client": client} if client is not None else {}),
                },
            )
        return sha, [doc_id(c.rel) for c in changes]

    async def dream_proposal(
        self,
        dream: int,
        pass_: str,
        summary: str,
        reason: str,
        raw_ops: Any,
        *,
        speculative: bool = False,
        client: str | None = None,
    ) -> WriteResult:
        """A dream's change for the owner to accept or reject: checked now (it must apply to
        the pages as they are), with each page's last commit as its base. `client`: as for
        dream_commit, recorded in the proposal."""
        reason = _clean_reason(reason)
        summary = " ".join(str(summary).split())[:300] or reason
        _check_dream(dream, pass_)
        if client is not None:
            check_dream_client(client)
        if (
            isinstance(raw_ops, list)
            and len(raw_ops) == 1
            and isinstance(raw_ops[0], dict)
            and raw_ops[0].get("op") == "merge_identity"
        ):
            op = raw_ops[0]
            merger = Actor(DREAM.client, DREAM.trust, DREAM.scopes | {"wiki:private"})
            payload = self._identity_payload(
                str(op.get("page") or ""), str(op.get("canonical") or ""), merger
            )
            for page in (payload["duplicate"], payload["canonical"]):
                if not self.is_reviewed(self.read(page)[1].frontmatter or {}):
                    raise WikiError(f"{page} is not reviewed; dreams leave it alone")
            for pending in self.list_proposals():
                if (
                    pending.get("action") == IDENTITY
                    and pending.get("duplicate") == payload["duplicate"]
                ):
                    raise WikiError("an identity merge is proposed already")
            payload.update(dream=dream, **{"pass": pass_, "summary": summary})
            return await self.create_proposal(
                IDENTITY,
                normalize_ref(payload["duplicate"]),
                payload,
                reason,
                DREAM,
                Trust.AGENT,
                title=summary,
                private=payload["private_preview"],
            )
        try:
            ops = parse_ops(raw_ops, PROPOSAL_OPS)
        except OpError as exc:
            raise WikiError(str(exc)) from exc
        async with self.lock:
            changes = self._dream_changes(ops, PROPOSAL_OPS, dream, DREAM)
            private = any(self._private_after(c) for c in changes)
            base = {op.page: self._page_sha(op.page) for op in ops}
        payload = {
            "dream": dream,
            "pass": pass_,
            "summary": summary,
            "speculative": bool(speculative),
            "operations": [op.as_dict() for op in ops],
            "base": base,
            **({"client": client} if client is not None else {}),
        }
        target = normalize_ref(ops[0].page)
        return await self.create_proposal(
            "dream", target, payload, reason, DREAM, Trust.AGENT, title=summary, private=private
        )

    async def revert_dream(self, dream: int, actor: Actor) -> dict[str, Any]:
        """Undo a dream: `git revert` each of its direct commits, newest first, one revert
        commit each, then reject its pending proposals. A revert that conflicts on a page is
        aborted cleanly and the rest is left as it is; it is reported, never forced."""
        _check_dream(dream, "revert")
        index_rel, log_rel = PurePosixPath("index.md"), PurePosixPath("log.md")
        generated = {str(index_rel), str(log_rel)}
        reverted: list[dict[str, str]] = []
        conflict: dict[str, Any] | None = None
        async with self.lock:
            for sha in self.repo.commits_with_trailer(f"Dream: {dream}"):
                if self.repo.commits_with_trailer(f"Reverts: {sha}"):
                    continue  # reverted before
                subject = self.repo.subject(sha)
                conflicts = await asyncio.to_thread(self.repo.revert_start, sha)
                pages = [p for p in conflicts if p not in generated]
                if pages:
                    await asyncio.to_thread(self.repo.revert_abort)
                    conflict = {"commit": sha, "subject": subject, "pages": pages}
                    break
                try:
                    staged = await asyncio.to_thread(self.repo.revert_finish, [index_rel, log_rel])
                    changes = []
                    for p in staged:
                        rel = PurePosixPath(p)
                        path = resolve_in_bundle(self.root, rel)
                        text = path.read_text(encoding="utf-8") if path.is_file() else None
                        changes.append(_Change(rel, text, "Revert", rel.stem))
                    if not changes:
                        continue
                    new = await self._commit(
                        changes,
                        f"Revert dream {dream}: {subject}",
                        f"Dream {dream} reverted by the owner in the portal",
                        actor,
                        actor.trust,
                        extra_trailers={"Reverts": sha},
                    )
                except Exception:
                    await asyncio.to_thread(self.repo.revert_abort)
                    raise
                reverted.append({"commit": sha, "revert": new})
        rejected: list[str] = []
        if conflict is None:
            for proposal in self.list_proposals():
                if proposal.get("dream") == dream and proposal.get("proposal_id"):
                    pid = str(proposal["proposal_id"])
                    await self.reject_proposal(pid, f"Dream {dream} was reverted", actor)
                    rejected.append(pid)
        return {"reverted": reverted, "rejected": rejected, "conflict": conflict}

    # ================================================================ proposals

    async def create_proposal(
        self,
        action: str,
        target: PurePosixPath,
        payload: dict[str, Any],
        reason: str,
        actor: Actor,
        trust: Trust,
        *,
        title: str,
        private: bool = False,
    ) -> WriteResult:
        async with self.lock:
            stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
            base = f"{stamp}-{slugify(target.stem, 40)}"
            rel = PurePosixPath(PROPOSALS_DIR) / f"{base}.md"
            n = 2
            while resolve_in_bundle(self.root, rel).exists():
                rel = PurePosixPath(PROPOSALS_DIR) / f"{base}-{n}.md"
                n += 1
            now = now_ts()
            # A proposal for a private page is private itself: it holds the page's content.
            proposed = payload.get("frontmatter")
            proposed = proposed if isinstance(proposed, dict) else {}
            private = (
                private
                or self.is_private_file(target)
                or proposed.get("private") is True
                or (
                    not self.exists(target)
                    and proposed.get("private") is not False
                    and self.private_by_default(target, str(proposed.get("type") or ""))
                )
            )
            fm: dict[str, Any] = {
                "type": "proposal",
                "id": doc_id(rel),
                "title": f"Proposal: {action} {target}",
                "description": reason,
                "status": "draft",
                **({"private": True} if private else {}),
                "generated": {"by": actor.generated_by, "at": now},
                "trust": trust.value,
                "created": now,
                "updated": now,
                "proposal": {"action": action, "target": str(target), "proposed_by": actor.client},
                "payload": payload,
            }
            summary = (
                f"# Proposed {action} of `{target}`\n\n"
                f"Proposed by **{actor.client}** because: {reason}\n\n"
                "This change was derived from content with trust level "
                f"`{trust.value}` and awaits owner review (accept_proposal / reject_proposal).\n"
            )
            if action == "dream":
                summary = _dream_summary(payload, reason)
            elif action == IDENTITY:
                summary = _identity_summary(payload, reason, actor)
            elif action == "write":
                summary += "\n## Proposed content\n\n" + str(payload.get("body", ""))
            elif action == "append":
                summary += (
                    f"\n## Proposed addition to section '{payload.get('section')}'\n\n"
                    + str(payload.get("content", ""))
                )
            text = serialize_document(fm, summary)
            self._check_size(text, self.max_file_bytes * 2)
            self._check_secrets(text)
            sha = await self._commit(
                [_Change(rel, text, "Proposal", title)],
                f"Propose {action} of {target}",
                reason,
                actor,
                trust,
            )
        return WriteResult(
            doc_id(target),
            str(target),
            "proposed",
            sha,
            False,
            trust.value,
            proposal_id=doc_id(rel),
            message=(
                "Stored as a proposal for owner review because it derives from lower-trust content."
            ),
        )

    def _merge_preview_private(self, fm: dict[str, Any]) -> bool:
        """A saved preview becomes private if one of its referenced pages is hidden later."""
        meta = fm.get("proposal")
        if not isinstance(meta, dict) or meta.get("action") != IDENTITY:
            return False
        payload = fm.get("payload") or {}
        text = str(payload.get("preview") or "")
        return any(
            re.search(rf"(?<![\w.-]){re.escape(pid)}(?![\w/-])", text)
            for pid in self.index.private_ids()
        )

    def list_proposals(self, *, private: bool = True) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        folder = self.root / PROPOSALS_DIR
        if not folder.is_dir():
            return out
        for path in sorted(folder.glob("*.md")):
            if path.name in RESERVED_NAMES or path.is_symlink():
                continue
            try:
                fm = parse_document(path.read_text(encoding="utf-8")).frontmatter or {}
            except DocumentError:
                continue
            hidden = is_private(fm) or self._merge_preview_private(fm)
            if not private and hidden:
                continue
            meta = fm.get("proposal") or {}
            entry = {
                "proposal_id": fm.get("id"),
                "title": fm.get("title"),
                "action": meta.get("action"),
                "target": meta.get("target"),
                "proposed_by": meta.get("proposed_by"),
                "reason": fm.get("description"),
                "trust": fm.get("trust"),
                "created": fm.get("created"),
                "private": hidden,
            }
            payload = fm.get("payload") or {}
            if meta.get("action") in ("dream", IDENTITY) and isinstance(payload, dict):
                entry["dream"] = payload.get("dream")
                entry["pass"] = payload.get("pass")
                entry["summary"] = payload.get("summary")
                entry["speculative"] = payload.get("speculative") is True
            if meta.get("action") == IDENTITY and isinstance(payload, dict):
                for key in (
                    "duplicate",
                    "canonical",
                    "duplicate_title",
                    "canonical_title",
                    "preview",
                    "review_token",
                    "migration",
                ):
                    entry[key] = payload.get(key)
            out.append(entry)
        return out

    def _load_proposal(
        self, proposal_id: str, actor: Actor | None = None
    ) -> tuple[PurePosixPath, dict[str, Any]]:
        rel, doc, _ = self.read(proposal_id, actor)
        if rel.parts[0] != PROPOSALS_DIR or doc.type != "proposal":
            raise WikiError(f"{proposal_id} is not a proposal; use review_proposals to list them")
        return rel, doc.frontmatter or {}

    async def accept_proposal(
        self,
        proposal_id: str,
        reason: str,
        actor: Actor,
        *,
        review_token: str | None = None,
        require_review_token: bool = False,
    ) -> WriteResult:
        reason = _clean_reason(reason)
        async with self.lock:
            proposal_rel = normalize_ref(proposal_id)
            receipt = PurePosixPath("merge-receipts") / proposal_rel.name
            if proposal_rel.parts[0] == PROPOSALS_DIR and self.exists(receipt):
                if actor.trust is not Trust.OWNER or not actor.human:
                    raise WikiError(IDENTITY_OWNER_ONLY)
                if require_review_token and review_token is None:
                    raise WikiError("review_token is required for merge approval")
                _, receipt_doc, _ = self.read(str(receipt), actor)
                receipt_fm = receipt_doc.frontmatter or {}
                if review_token is not None and review_token != receipt_fm.get("review_token"):
                    raise WikiError("preview changed; review the accepted preview")
                duplicate = str(receipt_fm["duplicate"])
                return WriteResult(
                    duplicate,
                    str(normalize_ref(duplicate)),
                    "unchanged",
                    self._page_sha(str(receipt)),
                    trust="owner",
                )
            rel, fm = self._load_proposal(proposal_id, actor)
            meta = fm.get("proposal") or {}
            payload = fm.get("payload") or {}
            target = str(meta.get("target", ""))
            proposer = Actor(str(meta.get("proposed_by", "unknown")), Trust.AGENT)
            verified_by = f"human:{actor.human or 'owner'}"
            trailers = {"Proposed-by": proposer.client}
            changes: list[_Change] = []
            if meta.get("action") == "write":
                t_rel, concept, body = self._validate_concept(
                    target, payload.get("frontmatter"), str(payload.get("body", ""))
                )
                self._check_target(t_rel, concept, actor)
                text, created = self._build_concept(
                    t_rel, concept, body, proposer, Trust.AGENT, verified_by=verified_by
                )
                changes.append(
                    _Change(t_rel, text, "Creation" if created else "Update", concept.title)
                )
            elif meta.get("action") == "append":
                t_rel, doc, _ = self.read(target, actor)
                fm_t = doc.frontmatter or {}
                concept = validate_concept_input(
                    fm_t,
                    known_types=[t.name for t in self.registry.all()],
                    consolidated=bool(fm_t.get("merge_history")),
                )
                new_body = insert_into_section(
                    doc.body,
                    str(payload.get("section")),
                    _clean_body(str(payload.get("content", ""))),
                )
                text, _ = self._build_concept(
                    t_rel, concept, new_body, proposer, Trust.AGENT, verified_by=verified_by
                )
                changes.append(_Change(t_rel, text, "Update", doc.title))
            elif meta.get("action") == "dream":
                try:
                    ops = parse_ops(payload.get("operations"), PROPOSAL_OPS)
                except OpError as exc:
                    raise WikiError(f"proposal is malformed: {exc}") from exc
                base = payload.get("base") or {}
                for pid in {op.page for op in ops}:
                    if self._page_sha(pid) != base.get(pid):
                        raise WikiError(f"{pid}: page changed since the dream; run it again")
                dream = int(payload.get("dream") or 0)
                trailers["From-dream"] = str(dream)
                changes += self._dream_changes(
                    ops, PROPOSAL_OPS, dream, proposer, verified_by=verified_by
                )
            elif meta.get("action") == IDENTITY:
                # The owner's own decision, never an assistant's (the MCP tool refuses too).
                if actor.trust is not Trust.OWNER or not actor.human:
                    raise WikiError(IDENTITY_OWNER_ONLY)
                if require_review_token and review_token is None:
                    raise WikiError("review_token is required for merge approval")
                changes += self._identity_changes(payload, actor, review_token)
                receipt_fm = {
                    "duplicate": payload["duplicate"],
                    "canonical": payload["canonical"],
                    "review_token": payload["review_token"],
                    "private": is_private(fm),
                }
                changes.append(
                    _Change(receipt, serialize_document(receipt_fm, "Accepted merge.\n"))
                )
            else:
                raise WikiError(f"proposal has unknown action {meta.get('action')!r}")
            changes.append(_Change(rel, None))
            sha = await self._commit(
                changes,
                f"Accept proposal {doc_id(rel)} ({meta.get('action')} {target})",
                reason,
                actor,
                Trust.OWNER,
                extra_trailers=trailers,
            )
        return WriteResult(
            doc_id(changes[0].rel), str(changes[0].rel), "committed", sha, trust="agent"
        )

    async def reject_proposal(self, proposal_id: str, reason: str, actor: Actor) -> WriteResult:
        reason = _clean_reason(reason)
        async with self.lock:
            rel, fm = self._load_proposal(proposal_id, actor)
            target = str((fm.get("proposal") or {}).get("target", ""))
            sha = await self._commit(
                [_Change(rel, None, "Rejected", f"proposal for {target}")],
                f"Reject proposal {doc_id(rel)}",
                reason,
                actor,
                Trust.OWNER,
            )
        return WriteResult(doc_id(rel), str(rel), "rejected", sha)

    # ================================================================ commit machinery

    def _write_file(self, rel: PurePosixPath, text: str | None) -> None:
        path = resolve_in_bundle(self.root, rel)
        if text is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    async def _commit(
        self,
        changes: list[_Change],
        subject: str,
        reason: str,
        actor: Actor,
        trust: Trust,
        *,
        extra_trailers: dict[str, str] | None = None,
    ) -> str:
        assert self.lock.locked(), "writes must hold the store lock"
        index_rel = PurePosixPath("index.md")
        log_rel = PurePosixPath("log.md")
        marked = self._newly_private(changes)
        if marked:
            changes = changes + self._inferences_on_newly_private(changes, marked)
            changes = changes + self._briefing_on_newly_private(changes, marked)
        touched = [c.rel for c in changes]
        for change in changes:
            change.private = self._private_after(change)
        try:
            for change in changes:
                self._write_file(change.rel, change.text)
            self._write_file(index_rel, self._render_index())
            self._write_file(log_rel, self._render_log(changes, reason, actor))
            trailers = {"Client": actor.client, "Trust": trust.value, **(extra_trailers or {})}
            message = f"{subject}\n\n{reason}\n\n" + "\n".join(
                f"{k}: {v}" for k, v in trailers.items()
            )
            if any(c.private for c in changes):
                message += "\n" + PRIVATE_TRAILER
            name, email = actor.git_author
            sha = await asyncio.to_thread(
                self.repo.commit,
                [*touched, index_rel, log_rel],
                message,
                author_name=name,
                author_email=email,
            )
        except Exception:
            await asyncio.to_thread(self.repo.restore, [*touched, index_rel, log_rel])
            raise
        finally:
            for change in changes:
                self.reindex(change.rel)
        if self.on_commit is not None:
            await self.on_commit(sha)
        return sha

    def _newly_private(self, changes: list[_Change]) -> set[str]:
        """Ids of the pages these changes make private (or create private)."""
        return {
            doc_id(c.rel)
            for c in changes
            if c.text is not None and _text_private(c.text) and not self.is_private_file(c.rel)
        }

    def _briefing_on_newly_private(self, changes: list[_Change], marked: set[str]) -> list[_Change]:
        """The briefing for every client without its lines about a page these changes make
        private (each line links to the page it comes from); the next dream rewrites it."""
        rel = normalize_ref(BRIEFING_PAGE)
        path = resolve_in_bundle(self.root, rel)
        if rel in {c.rel for c in changes} or not path.is_file():
            return []
        doc = parse_document(path.read_text(encoding="utf-8"))
        links = tuple(f"](/{did}.md)" for did in marked)
        lines = doc.body.split("\n")
        kept = [line for line in lines if not any(k in line for k in links)]
        if len(kept) == len(lines):
            return []
        fm = dict(doc.frontmatter or {})
        fm["updated"] = now_ts()
        text = serialize_document(fm, "\n".join(kept))
        return [_Change(rel, text, "Update", doc.title or rel.stem)]

    def _inferences_on_newly_private(
        self, changes: list[_Change], marked: set[str]
    ) -> list[_Change]:
        """Inference pages that rest on or are about a page these changes make private, marked
        private too: they quote its lines."""
        changed = {c.rel for c in changes}
        out: list[_Change] = []
        for row in self.index.list_docs(type="inference", limit=100_000):
            rel = PurePosixPath(row.path)
            if row.private or rel in changed:
                continue
            text = resolve_in_bundle(self.root, rel).read_text("utf-8")
            fm: dict[str, Any] = parse_document(text).frontmatter or {}
            refs: list[Any] = [*_as_list(fm.get("about"))]
            for key in ("supports", "counterexamples"):
                refs += [e.get("page") for e in _as_list(fm.get(key)) if isinstance(e, dict)]
            cited = {str(r or "").strip("/").removesuffix(".md") for r in refs}
            if cited & marked:
                marked_text = self._with_private(rel, True)
                if marked_text is not None:
                    out.append(_Change(rel, marked_text, "Update", row.title or rel.stem))
        return out

    def reindex(self, rel: PurePosixPath) -> None:
        did = doc_id(rel)
        indexable = rel.parts[0] in INDEXED_ROOTS or len(rel.parts) == 1
        path = resolve_in_bundle(self.root, rel)
        if not indexable or rel.name in RESERVED_NAMES or not path.is_file():
            self.index.remove(did)
            return
        try:
            self.index.upsert(rel, parse_document(path.read_text(encoding="utf-8")))
        except DocumentError:
            self.index.remove(did)

    # ================================================================ index.md / log.md

    def _render_index(self) -> str:
        rows_by_type: dict[str, list[tuple[str, str, str]]] = {}
        for rel, doc in self.iter_documents():
            if len(rel.parts) == 1:
                continue
            fm = doc.frontmatter or {}
            if is_private(fm) or canonical_of(fm):
                continue  # index.md is read by every client; the portal lists private pages
            raw_desc = fm.get("description")
            desc = raw_desc if isinstance(raw_desc, str) else ""
            rows_by_type.setdefault(doc.type or "untyped", []).append(
                (doc.title or rel.stem, "/" + str(rel), desc)
            )
        lines = [
            "---",
            f'okf_version: "{OKF_VERSION}"',
            "---",
            "",
            f"# {self.wiki_title}",
            "",
            "Start with [AGENTS.md](/AGENTS.md) (house rules) and [README.md](/README.md). "
            "This index is regenerated automatically on every change.",
            "",
        ]
        ordered = [t.name for t in self.registry.all()]
        for tname in sorted(
            rows_by_type, key=lambda n: (ordered.index(n) if n in ordered else 99, n)
        ):
            ctype = self.registry.get(tname)
            lines += [f"# {ctype.heading if ctype else tname.capitalize()}", ""]
            for title, link, desc in sorted(rows_by_type[tname], key=lambda r: r[0].lower()):
                safe_title = title.replace("[", "(").replace("]", ")")
                lines.append(f"* [{safe_title}]({link})" + (f" - {desc}" if desc else ""))
            lines.append("")
        return "\n".join(lines).rstrip("\n") + "\n"

    def _render_log(self, changes: list[_Change], reason: str, actor: Actor) -> str:
        existing = self.read_text_file("log.md")
        today = dt.datetime.now(dt.UTC).date().isoformat()
        entries = []
        # log.md is readable by every client, and a line cannot be hidden later without
        # knowing what it was about. So a change to a private page is logged without its
        # path, title or reason, and so is the reason of a commit that also touched one: the
        # git commit (hidden from clients without wiki:private) keeps all of it.
        private = any(c.private for c in changes)
        for c in changes:
            if not c.log_kind:
                continue
            if c.private:
                entries.append(f"* **{c.log_kind}**: a private page _(by {actor.client})_")
                continue
            title = (c.log_title or c.rel.stem).replace("[", "(").replace("]", ")")
            linkable = c.text is not None and c.rel.parts[0] != PROPOSALS_DIR
            target = f"[{title}](/{c.rel})" if linkable else f"`{c.rel}`"
            why = "reason not shown (a private page changed too)" if private else reason
            entries.append(f"* **{c.log_kind}**: {target} — {why} _(by {actor.client})_")
        if not entries:
            return existing or f"{LOG_HEADER}\n"
        body_lines = existing.splitlines()
        if not body_lines or not body_lines[0].startswith("# "):
            body_lines = [LOG_HEADER, "", *body_lines]
        heading = f"## {today}"
        if heading in body_lines:
            pos = body_lines.index(heading) + 1
            body_lines[pos:pos] = entries
        else:
            pos = 1
            while pos < len(body_lines) and not body_lines[pos].startswith("## "):
                pos += 1
            body_lines[pos:pos] = [heading, *entries, ""]
        text = "\n".join(body_lines)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.rstrip("\n") + "\n"


# ==================================================================== helpers


def insert_into_section(body: str, section: str, content: str) -> str:
    """Append `content` at the end of the section whose heading text is `section`
    (case-insensitive), creating a level-2 section at the end if there is none."""
    lines = body.rstrip("\n").split("\n") if body.strip() else []
    target = section.strip().lower()
    start = level = None
    in_fence = False
    for i, line in enumerate(lines):
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m and m.group(2).strip().lower() == target:
            start, level = i, len(m.group(1))
            break
    content = content.strip("\n")
    if start is None:
        return "\n".join([*lines, "", f"## {section}", "", content]).lstrip("\n") + "\n"
    assert level is not None
    end = len(lines)
    in_fence = False
    for j in range(start + 1, len(lines)):
        if lines[j].startswith(("```", "~~~")):
            in_fence = not in_fence
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})\s", lines[j])
        if m and len(m.group(1)) <= level:
            end = j
            break
    head, tail = lines[:end], lines[end:]
    while head and not head[-1].strip() and len(head) > start + 1:
        head.pop()
    new = [*head, "", content]
    if tail:
        new += ["", *tail]
    return "\n".join(new) + "\n"


def text_window(text: str, offset: int, limit: int) -> tuple[str, int | None]:
    """At most `limit` characters of `text` from `offset`, ending after a line break when one
    falls in the second half of the window; also the offset to continue from (None at the end)."""
    offset = max(0, min(offset, len(text)))
    end = offset + limit
    if end >= len(text):
        return text[offset:], None
    cut = text.rfind("\n", offset + limit // 2, end)
    if cut != -1:
        end = cut + 1
    return text[offset:end], end


def _head(path: Path, size: int = 16384) -> tuple[dict[str, Any] | None, str]:
    """Frontmatter and the start of the body of a (possibly huge) file, reading only its
    beginning. (None, "") when it is missing or has no readable frontmatter."""
    try:
        with path.open("rb") as f:
            text = f.read(size).decode("utf-8", errors="ignore")
    except OSError:
        return None, ""
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if m is None:
        return None, ""
    try:
        fm = safe_yaml_load(m.group(1))
    except yaml.YAMLError:
        return None, ""
    return (fm if isinstance(fm, dict) else None), text[m.end() :]


def markdown_section(text: str, heading: str) -> str | None:
    """The level-2 section `## heading` of `text` (heading line included, up to the next
    level-1 or level-2 heading), or None when there is none."""
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip() == f"## {heading}"), None)
    if start is None:
        return None
    end = next(
        (j for j in range(start + 1, len(lines)) if re.match(r"^#{1,2}\s", lines[j])),
        len(lines),
    )
    return "\n".join(lines[start:end]).strip("\n")


def load_yaml_text(text: str) -> Any:
    return safe_yaml_load(text)


def replace_line(body: str, old: str, new: str) -> str:
    """`body` with the first line equal to `old` (trailing spaces aside) replaced by `new`."""
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if line.rstrip() == old.rstrip():
            lines[i] = new
            return "\n".join(lines)
    raise WikiError(f"no such line: {old[:120]}")


def prepend_note(body: str, note: str) -> str:
    """`body` with a note paragraph right under its title (or at the top)."""
    note = " ".join(note.split())
    lines = body.split("\n")
    if lines and lines[0].startswith("# "):
        return "\n".join([lines[0], "", note, *lines[1:]])
    return f"{note}\n\n{body}"


def _history(body: str) -> list[str]:
    """The lines of a page's History section (a pattern page keeps them when rewritten)."""
    section = markdown_section(body, "History")
    return [line.strip() for line in bullet_lines(section)] if section else []


def _goal_key(line: str) -> str:
    """A goal compared for duplicates: without its `(set YYYY-MM-DD)` date."""
    return _line_key(_SET.sub("", line))


def _fm_text(fm: dict[str, Any], key: str, limit: int) -> str:
    value = fm.get(key)
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _check_dream(dream: Any, pass_: str) -> None:
    if not isinstance(dream, int) or isinstance(dream, bool) or dream < 1:
        raise WikiError("'dream' must be the dream's id, a positive integer")
    if not _PASS.match(pass_):
        raise WikiError("'pass' must be a short lowercase name, e.g. hygiene")


def check_dream_client(client: Any) -> str:
    """The assistant an assistant dream runs in (claude.ai, chatgpt): a short printable name,
    as it goes into a commit trailer."""
    if (
        not isinstance(client, str)
        or not 1 <= len(client) <= 40
        or not client.isprintable()
        or client.strip() != client
    ):
        raise WikiError("'client' must be a short name on one line, e.g. claude.ai")
    return client


def _identity_summary(payload: dict[str, Any], reason: str, actor: Actor) -> str:
    """The body of an identity-merge proposal: the two pages and what accepting changes."""
    dup, canon = payload.get("duplicate"), payload.get("canonical")
    d_title, c_title = payload.get("duplicate_title"), payload.get("canonical_title")
    alias = alias_from_title(str(d_title or ""))
    summary = (
        f"# Proposed identity merge of `{dup}`\n\n"
        f"[{d_title}](/{dup}.md) is the same as [{c_title}](/{canon}.md).\n\n"
        f"Proposed by **{actor.client}** because: {reason}\n\n"
        "Only the owner accepts or rejects it, in the portal (Wiki > Proposals). Accepting "
        "consolidates content, citations, aliases, relationships and provenance:\n\n"
        f"- `{dup}` gets `canonical: {canon}` and `status: deprecated`; a minimal redirect stays, and "
        "reading it shows the canonical page.\n"
    )
    if alias:
        summary += (
            f'- `{canon}` gets "{alias}" in `aliases` (unless it has that name already), so '
            "searching that name finds it.\n"
        )
    summary += (
        "\n## Consolidated page preview\n\n```markdown\n"
        + str(payload.get("preview") or "Refresh preview before approval.")
        + "\n```\n"
    )
    return summary


def _dream_summary(payload: dict[str, Any], reason: str) -> str:
    """The body of a dream proposal: what it would change, for the owner to judge."""
    lines = [f"# Dream {payload.get('dream')}: {payload.get('summary')}", ""]
    if payload.get("speculative"):
        lines += [
            f"{SPECULATIVE_BANNER} This proposal rests on a speculative inference: it goes "
            "beyond what the supporting lines state.",
            "",
        ]
    lines += [
        f"Proposed by the {payload.get('pass')} pass of dream {payload.get('dream')} because: "
        f"{reason}",
        "",
        "Nothing changes until you accept it. If a page it touches changes first, accepting "
        "is refused: run the dream again.",
        "",
        "## Proposed changes",
        "",
    ]
    for op in parse_ops(payload.get("operations"), PROPOSAL_OPS):
        lines.append(op.describe())
        if op.op == "write_inference":
            lines += ["", "```markdown", op.body.strip(), "```", ""]
    return "\n".join(lines) + "\n"
