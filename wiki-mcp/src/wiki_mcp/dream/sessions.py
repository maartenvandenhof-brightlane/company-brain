"""Assistant dreams' sessions, in dreams.sqlite (wiki-mcp state, next to oauth.sqlite).

An assistant dream is a dream the owner runs by hand in Claude.ai or ChatGPT: he starts it on
the portal's Dreams page, which opens its session here (dream/assistant.py); the assistant's
model then does the passes through MCP tools, and closing the session writes the dream log.
A patterns session is the same for the Patterns page's "Reassess in Claude.ai / ChatGPT": a
session of kind "patterns" with the patterns pass only (dream/reassess.py), and no hygiene at
the opening; its dream log says trigger "patterns", as the portal's patterns runs do.
One row per dream id holds what the passes need between tool calls:

  dream, client      the dreams row's id in the portal; the assistant it runs in (claude.ai)
  kind               dream (the passes of PASSES) or patterns (the patterns pass): KINDS
  started, number    the dream's start (ISO, from the portal) and which dream of that day it
                     is: the log page's name, wiki/maintenance/dream-<day>-<number>
  base, since_base   the wiki commit it started from; the previous dream's (deduction reads
                     what changed since; null: every reviewed page)
  opened, expires    SESSION_TTL_S after opening the session expires; nothing is written to
                     it any more, and the next read closes it (the log says "expired")
  finished, ended    when and how it closed: finished, expired, closed by the owner
  passes             the passes submitted so far, each with its parts (D3's windows)
  commits, proposals what it wrote, in the shape of the portal's dreams row
  log                the DreamLog the passes fill (dream/log.py), rendered at the close
  work               what the MCP tools keep between calls (dream/passes.py): the private
                     access the material was read with, the apps that called, each pass's
                     number of parts, a fingerprint of each part's material, and what a
                     pass carries from one part to the next

States: open (not finished, not expired), expired (not finished, past `expires`), finished.
Expiry is lazy, like the OAuth state's pending authorizations: nothing runs when a session
expires; reads see it. At most one session is open at a time, and a dream id is opened once.

The observe tools (dream/observing.py) need no session, but a long chat's windows are answered
one at a time: the table `observing` keeps each window's answer until the chat's last one is
in (then the page is written and its rows go), and drops what is older than OBSERVE_TTL_S.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wiki_mcp.dream.log import Commit, DreamLog
from wiki_mcp.wiki.store import WikiError, check_dream_client

SESSION_TTL_S = 7200
OBSERVE_TTL_S = 86400  # a long chat's window answers wait this long for the others
# The passes an assistant does, in order, after the hygiene fixes made when the session opens
# (D4's duplicate merges, D2, D3, D5); D6, the log, is the close.
PASSES = ("duplicates", "deduction", "induction", "hot-memory")
# A session's kind: its passes, and the trigger its dream log (and the portal's row) names.
KINDS = {"dream": PASSES, "patterns": ("patterns",)}
TRIGGERS = {"dream": "assistant", "patterns": "patterns"}
ENDED = ("finished", "expired", "closed by the owner")

_STARTED = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+(Z|[+-]\d{2}:\d{2})?$")
_SHA = re.compile(r"^[0-9a-f]{4,40}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    dream INTEGER PRIMARY KEY,
    client TEXT NOT NULL,
    started TEXT NOT NULL,
    number INTEGER NOT NULL,
    base TEXT NOT NULL DEFAULT '',
    since_base TEXT,
    opened REAL NOT NULL,
    expires REAL NOT NULL,
    finished REAL,
    ended TEXT NOT NULL DEFAULT '',
    passes TEXT NOT NULL DEFAULT '{}',
    commits TEXT NOT NULL DEFAULT '[]',
    proposals TEXT NOT NULL DEFAULT '[]',
    log TEXT NOT NULL DEFAULT '{}',
    log_page TEXT NOT NULL DEFAULT '',
    work TEXT NOT NULL DEFAULT '{}',
    kind TEXT NOT NULL DEFAULT 'dream'
);
CREATE INDEX IF NOT EXISTS sessions_open ON sessions(finished, expires);
CREATE TABLE IF NOT EXISTS observing (
    source TEXT NOT NULL,
    part INTEGER NOT NULL,
    parts INTEGER NOT NULL,
    answer TEXT NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (source, part)
);
"""


class SessionError(WikiError):
    """A session refused: the message says why."""


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Session:
    dream: int
    client: str
    started: str
    number: int
    opened: float
    expires: float
    base: str = ""
    since_base: str | None = None
    kind: str = "dream"  # KINDS
    finished: float | None = None
    ended: str = ""
    passes: dict[str, list[str]] = field(default_factory=dict)  # pass -> parts submitted
    commits: list[dict[str, Any]] = field(default_factory=list)  # {sha, pass, pages}
    proposals: list[dict[str, Any]] = field(default_factory=list)  # {id, pass, summary, ...}
    log: DreamLog = field(default_factory=DreamLog)
    log_page: str = ""
    # What the MCP tools keep between calls (dream/passes.py)
    access: str = ""  # "private" or "public": how the material is read, fixed at the first read
    run_in: list[str] = field(default_factory=list)  # the MCP clients that called the tools
    parts: dict[str, int] = field(default_factory=dict)  # pass -> its number of parts
    seen: dict[str, str] = field(default_factory=dict)  # "pass/part" -> material fingerprint
    carry: dict[str, Any] = field(default_factory=dict)  # pass -> what its parts carry over

    # What the dream log says about the dream (wiki_mcp.dream.log.Run)
    @property
    def id(self) -> int:
        return self.dream

    @property
    def trigger(self) -> str:
        return TRIGGERS[self.kind]

    @property
    def dry_run(self) -> bool:
        return False

    @property
    def base_sha(self) -> str:
        return self.base

    def status(self, now: float) -> str:
        if self.finished is not None:
            return "finished"
        return "expired" if now > self.expires else "open"

    @property
    def all_passes(self) -> tuple[str, ...]:
        """The passes of its kind, in order."""
        return KINDS[self.kind]

    @property
    def to_do(self) -> list[str]:
        """The passes not submitted yet."""
        return [p for p in self.all_passes if p not in self.passes]

    def record_commit(self, sha: str, pass_: str, pages: list[str], lines: list[str]) -> None:
        self.commits.append({"sha": sha, "pass": pass_, "pages": pages})
        self.log.commits.append(Commit(sha, pass_, lines))

    def record_proposal(
        self, pid: str, pass_: str, summary: str, *, speculative: bool = False
    ) -> None:
        self.proposals.append(
            {"id": pid, "pass": pass_, "summary": summary, "speculative": speculative}
        )
        self.log.proposed(pid, pass_, summary, speculative)

    def work(self) -> dict[str, Any]:
        return {
            "access": self.access,
            "run_in": self.run_in,
            "parts": self.parts,
            "seen": self.seen,
            "carry": self.carry,
        }

    def state(self, now: float) -> dict[str, Any]:
        """What the portal shows (GET /dream/session)."""
        return {
            "dream": self.dream,
            "kind": self.kind,
            "status": self.status(now),
            "client": self.client,
            "started": self.started,
            "number": self.number,
            "base": self.base,
            "since_base": self.since_base,
            "opened": iso(self.opened),
            "expires": iso(self.expires),
            "finished": iso(self.finished) if self.finished is not None else None,
            "ended": self.ended or None,
            "passes_done": self.passes,
            "passes_to_do": self.to_do,
            "commits": self.commits,
            "proposals": self.proposals,
            "flagged": self.log.flagged,
            "skipped": self.log.skipped,
            "log_page": self.log_page or None,
        }


class DreamSessions:
    def __init__(
        self,
        db_path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        ttl: int = SESSION_TTL_S,
    ) -> None:
        self.db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(sessions)")}
        if "work" not in columns:  # a state from before the MCP tools
            self.db.execute("ALTER TABLE sessions ADD COLUMN work TEXT NOT NULL DEFAULT '{}'")
        if "kind" not in columns:  # a state from before patterns sessions
            self.db.execute("ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'dream'")
        self._lock = threading.Lock()
        # Held while a session is opened, written to or closed: one of these at a time, so a
        # close cannot race a pass, and a second close finds the first one's result.
        self.lock = asyncio.Lock()
        self.clock = clock
        self.ttl = ttl

    def close(self) -> None:
        self.db.close()

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.db.execute(sql, params)

    def now(self) -> float:
        return self.clock()

    def create(
        self,
        dream: int,
        client: str,
        started: str,
        number: int,
        since_base: str | None,
        base: str = "",
        kind: str = "dream",
    ) -> Session:
        """A new open session; SessionError when the dream has one already, when another is
        open, or for a value that is not what the portal sends."""
        if not isinstance(dream, int) or isinstance(dream, bool) or dream < 1:
            raise SessionError("'dream' must be the dream's id, a positive integer")
        if kind not in KINDS:
            raise SessionError(f"'kind' must be one of {', '.join(KINDS)}")
        check_dream_client(client)
        if not isinstance(started, str) or not _STARTED.match(started):
            raise SessionError("'started' must be the dream's start, an ISO date and time")
        if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 999:
            raise SessionError("'number' must be which dream of the day this is (1, 2, ...)")
        if since_base is not None and not (isinstance(since_base, str) and _SHA.match(since_base)):
            raise SessionError("'since_base' must be a commit sha or null")
        now = self.now()
        with self._lock:
            if self.db.execute("SELECT 1 FROM sessions WHERE dream = ?", (dream,)).fetchone():
                raise SessionError(f"dream {dream} has had its session already")
            row = self.db.execute(
                "SELECT dream FROM sessions WHERE finished IS NULL AND expires >= ?", (now,)
            ).fetchone()
            if row is not None:
                raise SessionError(f"the session of dream {row['dream']} is still open")
            session = Session(
                dream, client, started, number, now, now + self.ttl, base, since_base, kind
            )
            self.db.execute(
                "INSERT INTO sessions (dream, client, started, number, base, since_base, opened, "
                "expires, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (dream, client, started, number, base, since_base, now, session.expires, kind),
            )
        return session

    def get(self, dream: int) -> Session | None:
        row = self._exec("SELECT * FROM sessions WHERE dream = ?", (dream,)).fetchone()
        return None if row is None else _session(row)

    def current(self) -> Session | None:
        """The open session, if any."""
        row = self._exec(
            "SELECT * FROM sessions WHERE finished IS NULL AND expires >= ? "
            "ORDER BY dream DESC LIMIT 1",
            (self.now(),),
        ).fetchone()
        return None if row is None else _session(row)

    def save(self, session: Session) -> None:
        """What the passes did so far (not whether it is finished: see finish); a finished
        session is never changed."""
        self._exec(
            "UPDATE sessions SET passes = ?, commits = ?, proposals = ?, log = ?, work = ? "
            "WHERE dream = ? AND finished IS NULL",
            (
                json.dumps(session.passes),
                json.dumps(session.commits),
                json.dumps(session.proposals),
                json.dumps(session.log.as_dict()),
                json.dumps(session.work()),
                session.dream,
            ),
        )

    def finish(self, session: Session, ended: str, log_page: str, at: float) -> None:
        """Close it for good, with what it did; SessionError when it was closed before."""
        if ended not in ENDED:
            raise ValueError(f"unknown end {ended!r}")
        self.save(session)
        cur = self._exec(
            "UPDATE sessions SET finished = ?, ended = ?, log_page = ? "
            "WHERE dream = ? AND finished IS NULL",
            (at, ended, log_page, session.dream),
        )
        if cur.rowcount != 1:
            raise SessionError(f"the session of dream {session.dream} is closed already")
        session.finished, session.ended, session.log_page = at, ended, log_page

    def state(self, session: Session) -> dict[str, Any]:
        return session.state(self.now())

    # --- a long chat's window answers (dream/observing.py) ----------------------------------

    def keep_window(
        self, source: str, window: int, windows: int, answer: dict[str, Any]
    ) -> list[int]:
        """Keep one window's answer (again: replaced); the windows of `source` held now.
        Answers older than OBSERVE_TTL_S, and those of the chat cut into another number of
        windows, go first."""
        with self._lock:
            self.db.execute("DELETE FROM observing WHERE at < ?", (self.now() - OBSERVE_TTL_S,))
            self.db.execute(
                "DELETE FROM observing WHERE source = ? AND parts != ?", (source, windows)
            )
            self.db.execute(
                "INSERT OR REPLACE INTO observing (source, part, parts, answer, at) "
                "VALUES (?, ?, ?, ?, ?)",
                (source, window, windows, json.dumps(answer), self.now()),
            )
            rows = self.db.execute(
                "SELECT part FROM observing WHERE source = ? ORDER BY part", (source,)
            ).fetchall()
        return [int(r["part"]) for r in rows]

    def windows_of(self, source: str) -> list[dict[str, Any]]:
        """The answers kept for `source`, in window order."""
        rows = self._exec(
            "SELECT answer FROM observing WHERE source = ? ORDER BY part", (source,)
        ).fetchall()
        return [dict(json.loads(r["answer"])) for r in rows]

    def drop_windows(self, source: str) -> None:
        self._exec("DELETE FROM observing WHERE source = ?", (source,))


def _session(row: sqlite3.Row) -> Session:
    work = json.loads(row["work"] or "{}")
    return Session(
        dream=row["dream"],
        client=row["client"],
        started=row["started"],
        number=row["number"],
        opened=row["opened"],
        expires=row["expires"],
        base=row["base"],
        since_base=row["since_base"],
        kind=row["kind"],
        finished=row["finished"],
        ended=row["ended"],
        passes=json.loads(row["passes"]),
        commits=json.loads(row["commits"]),
        proposals=json.loads(row["proposals"]),
        log=DreamLog.of(json.loads(row["log"])),
        log_page=row["log_page"],
        access=str(work.get("access") or ""),
        run_in=[str(c) for c in work.get("run_in") or []],
        parts={str(k): int(v) for k, v in (work.get("parts") or {}).items()},
        seen={str(k): str(v) for k, v in (work.get("seen") or {}).items()},
        carry=dict(work.get("carry") or {}),
    )
