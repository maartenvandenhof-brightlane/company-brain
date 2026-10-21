"""The inbox (gdrive and both modes): what a chat app saves lands in raw/inbox/ for review.

Told by its instructions (docs/CHAT_INSTRUCTIONS.md), Gemini creates a Google Task, or a Google
Keep note, whose title starts with the prefix (default "[brain]"). The poller reads the inbox
every few minutes and stores each new item once as a raw source, raw/inbox/YYYY-MM-DD/<slug>.md,
with trust external and its provenance: it shows up among the unprocessed sources (tag `inbox`),
and anything derived from it goes through the proposals review. Item ids are remembered in
SQLite, so nothing is ingested twice, also not after a restart. An item is data, never
instructions: it is stored verbatim after the secret scan every ingest gets; one the scan refuses
is remembered as refused and reported, not retried.

Tasks (the default) works with the connected user's own OAuth grant. Keep has no user-OAuth API:
a service account with domain-wide delegation, approved by a Workspace admin, reads the notes of
one Workspace user (keep.readonly, or keep when notes are deleted after ingest).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

from wiki_mcp.brain import BrainConfig, DriveSettings, Mode
from wiki_mcp.config import Trust
from wiki_mcp.google import KEEP_READONLY_SCOPE, KEEP_SCOPE, TASKS_SCOPE, Google, GoogleError
from wiki_mcp.runtime import Runtime
from wiki_mcp.supervisor import NotReady
from wiki_mcp.wiki.actor import Actor
from wiki_mcp.wiki.store import WikiError

log = logging.getLogger(__name__)

TASKS = "https://tasks.googleapis.com/tasks/v1"
KEEP = "https://keep.googleapis.com/v1"
MAX_PAGES = 50  # 100 items each

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    raw_id TEXT NOT NULL,  -- empty: refused (the reason is in the report of that run)
    at TEXT NOT NULL,
    PRIMARY KEY (source, item_id)
);
"""


@dataclass(frozen=True)
class InboxItem:
    id: str
    title: str
    content: str
    created: str
    author: str


class InboxAdapter(Protocol):
    """One inbox service: Google Tasks or Google Keep."""

    source: str  # provenance: google-tasks | google-keep

    async def check(self) -> None: ...  # raises GoogleError when the inbox cannot be read

    async def items(self) -> list[InboxItem]: ...

    async def done(self, item: InboxItem) -> None: ...  # after the item was ingested


class TasksInbox:
    source = "google-tasks"

    def __init__(self, google: Google, s: DriveSettings) -> None:
        self.google = google
        self.tasklist = s.tasklist

    async def list_id(self) -> str:
        """The task list: @default (where Gemini adds tasks), else a list id or title."""
        if self.tasklist == "@default":
            return self.tasklist
        r = await self.google.request("GET", f"{TASKS}/users/@me/lists", params={"maxResults": 100})
        for entry in r.json().get("items") or []:
            if self.tasklist in (entry.get("id"), entry.get("title")):
                return str(entry["id"])
        raise GoogleError(f'no task list "{self.tasklist}"')

    async def url(self) -> str:
        return f"{TASKS}/lists/{quote(await self.list_id(), safe='@')}/tasks"

    async def check(self) -> None:
        await self.google.request("GET", await self.url(), params={"maxResults": 1})

    async def items(self) -> list[InboxItem]:
        url = await self.url()
        author = (self.google.account() or {}).get("email", "")
        params: dict[str, Any] = {"maxResults": 100, "showCompleted": "true", "showHidden": "true"}
        out = []
        for _ in range(MAX_PAGES):
            data = (await self.google.request("GET", url, params=params)).json()
            out += [
                InboxItem(
                    str(t["id"]),
                    str(t.get("title") or ""),
                    str(t.get("notes") or ""),
                    str(t.get("updated") or ""),  # tasks have no creation time
                    author,
                )
                for t in data.get("items") or []
            ]
            if not data.get("nextPageToken"):
                break
            params["pageToken"] = data["nextPageToken"]
        return out

    async def done(self, item: InboxItem) -> None:
        return None  # the task stays: the user completes or deletes it


def _list_text(entries: Any, depth: int = 0) -> list[str]:
    lines = []
    for entry in entries or []:
        mark = "x" if entry.get("checked") else " "
        text = (entry.get("text") or {}).get("text", "")
        lines.append(f"{'  ' * depth}- [{mark}] {text}")
        lines += _list_text(entry.get("childListItems"), depth + 1)
    return lines


class KeepInbox:
    source = "google-keep"

    def __init__(self, google: Google, s: DriveSettings) -> None:
        self.google = google
        self.delete = s.keep_delete
        self.keep = (s.keep_user, KEEP_SCOPE if s.keep_delete else KEEP_READONLY_SCOPE)

    async def check(self) -> None:
        await self.google.request("GET", f"{KEEP}/notes", params={"pageSize": 1}, keep=self.keep)

    async def items(self) -> list[InboxItem]:
        params: dict[str, Any] = {"pageSize": 100}  # trashed notes are left out by default
        out = []
        for _ in range(MAX_PAGES):
            r = await self.google.request("GET", f"{KEEP}/notes", params=params, keep=self.keep)
            data = r.json()
            for note in data.get("notes") or []:
                body = note.get("body") or {}
                text = (body.get("text") or {}).get("text")
                content = (
                    text
                    if text is not None
                    else "\n".join(_list_text((body.get("list") or {}).get("listItems")))
                )
                owners = [
                    p.get("email")
                    for p in note.get("permissions") or []
                    if p.get("role") == "OWNER"
                ]
                out.append(
                    InboxItem(
                        str(note["name"]),
                        str(note.get("title") or ""),
                        str(content),
                        str(note.get("createTime") or ""),
                        str(owners[0] if owners and owners[0] else self.keep[0]),
                    )
                )
            if not data.get("nextPageToken"):
                break
            params["pageToken"] = data["nextPageToken"]
        return out

    async def done(self, item: InboxItem) -> None:
        if self.delete and item.id.startswith("notes/"):
            await self.google.request("DELETE", f"{KEEP}/{quote(item.id)}", keep=self.keep)


def inbox_for(google: Google, s: DriveSettings) -> InboxAdapter:
    return KeepInbox(google, s) if s.inbox == "keep" else TasksInbox(google, s)


class InboxPoller:
    name = "inbox"
    modes = frozenset({Mode.GDRIVE, Mode.BOTH})
    section = "gdrive"

    def __init__(self, rt: Runtime, google: Google) -> None:
        self.store = rt.store
        self.google = google
        self.db_path = rt.settings.state_dir / "inbox.sqlite"
        self.db: sqlite3.Connection | None = None
        self.task: asyncio.Task[None] | None = None
        self.report: dict[str, Any] = {}

    async def start(self, config: BrainConfig) -> None:
        s = config.gdrive
        if s.inbox == "keep" and (self.google.keep_account() is None or not s.keep_user):
            raise NotReady("Keep needs the service account key and the user to read (setup page)")
        if s.inbox == "tasks" and not self.google.has_scope(TASKS_SCOPE):
            raise NotReady("Google is not connected with Tasks access: use Connect Google")
        self.db = sqlite3.connect(self.db_path)
        self.db.executescript(_SCHEMA)
        self.report = {"last_poll": None, "errors": []}
        self.task = asyncio.create_task(
            self._run(inbox_for(self.google, s), s.prefix, s.poll_minutes * 60)
        )

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.db is not None:
            self.db.close()
        self.task = self.db = None

    async def _run(self, inbox: InboxAdapter, prefix: str, every_s: float) -> None:
        while True:
            try:
                await self.poll(inbox, prefix)
            except Exception:  # a bug must not end the loop silently
                log.exception("inbox poll failed")
            await asyncio.sleep(every_s)

    async def poll(self, inbox: InboxAdapter, prefix: str) -> int:
        """Ingest the new items whose title starts with `prefix` ("": all of them); returns
        how many were stored."""
        assert self.db is not None
        errors: list[str] = []
        stored = 0
        try:
            items = await inbox.items()
        except (GoogleError, OSError) as exc:
            items, errors = [], [f"{inbox.source}: {exc}"]
        actor = Actor(f"process:{inbox.source}", Trust.EXTERNAL)
        for item in items:
            title = item.title.strip()
            if not title.casefold().startswith(prefix.casefold()):
                continue
            if self.db.execute(
                "SELECT 1 FROM seen WHERE source = ? AND item_id = ?", (inbox.source, item.id)
            ).fetchone():
                continue
            title = " ".join(title[len(prefix) :].split())[:200] or "Inbox note"
            provenance = {
                "source": inbox.source,
                "item_id": item.id,
                "created": item.created,
                "author": item.author,
            }
            try:
                result = await self.store.ingest_source(
                    title,
                    item.content if item.content.strip() else title,
                    None,
                    Trust.EXTERNAL.value,
                    actor,
                    subfolder="inbox",
                    extra={"provenance": provenance, "tags": ["inbox"]},
                )
                raw_id = result.id
            except WikiError as exc:  # e.g. it looks like a credential: never stored
                raw_id = ""
                errors.append(f"{inbox.source} item refused: {exc}")
            now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
            self.db.execute(
                "INSERT INTO seen VALUES (?, ?, ?, ?)", (inbox.source, item.id, raw_id, now)
            )
            self.db.commit()
            if raw_id:
                stored += 1
                try:
                    await inbox.done(item)
                except (GoogleError, OSError) as exc:
                    errors.append(f"{inbox.source}: could not finish {item.id}: {exc}")
        for error in errors[:5]:
            log.warning("inbox: %s", error)
        self.report = {
            "last_poll": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "ingested": stored,
            "errors": errors[:20],
        }
        return stored
