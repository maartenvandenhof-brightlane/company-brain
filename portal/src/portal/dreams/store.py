"""Dreams and what each one did, in assistant.sqlite (portal state, next to the ideas).

A dream's changes live in the wiki (commits with a `Dream: <id>` trailer, proposals, its log
page); this table is the portal's record of them: when it ran and why, the wiki commit it
started from, the commits and proposals it made, what it cost, and for a dry run what it
would have done. The id is the one the wiki's trailers and proposals carry.

An assistant dream (trigger "assistant", dreams/assistant.py) runs in Claude.ai or ChatGPT
(`client`); its session is wiki-mcp's, and the row is running until the portal reads the
session finished and copies what it did here.

A patterns run (trigger "patterns", the Patterns page's "Reassess patterns") is a dream with
only the patterns pass and the log. It is no real dream for what comes after: last() leaves it
out, so deduction still reads what changed since the last real dream. One reassessed in
Claude.ai or ChatGPT (the Patterns page's "Reassess in Claude.ai / ChatGPT") is a patterns run
with a `client`, whose session is wiki-mcp's as an assistant dream's is: both are "in an
assistant" (Dream.in_assistant, running_assistant()).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from portal.assistant import connect
from portal.ideas.store import now

TRIGGERS = ("auto", "manual", "dry run", "assistant", "patterns")
PATTERNS = "patterns"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dreams (
    id INTEGER PRIMARY KEY,
    started TEXT NOT NULL,
    finished TEXT,
    trigger TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 0,
    base_sha TEXT NOT NULL DEFAULT '',
    commits TEXT NOT NULL DEFAULT '[]',
    proposals TEXT NOT NULL DEFAULT '[]',
    cost_eur REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    log_page TEXT NOT NULL DEFAULT '',
    dry_run_result TEXT NOT NULL DEFAULT '{}',
    reverted TEXT NOT NULL DEFAULT '',
    client TEXT
);
"""


@dataclass
class Dream:
    id: int
    started: str
    trigger: str
    dry_run: bool = False
    finished: str | None = None
    base_sha: str = ""
    commits: list[dict[str, Any]] = field(default_factory=list)  # {sha, pass, pages}
    proposals: list[dict[str, Any]] = field(default_factory=list)  # {id, pass, summary, ...}
    cost_eur: float = 0.0
    error: str = ""
    log_page: str = ""
    dry_run_result: dict[str, Any] = field(default_factory=dict)
    reverted: str = ""  # when and how it was reverted
    client: str | None = None  # an assistant dream's: claude.ai, chatgpt

    @property
    def running(self) -> bool:
        return self.finished is None

    @property
    def in_assistant(self) -> bool:
        """Run in Claude.ai or ChatGPT, with a session in wiki-mcp: an assistant dream, or
        patterns reassessed there."""
        return self.client is not None and self.trigger in ("assistant", PATTERNS)


class DreamStore:
    def __init__(self, state_dir: Path) -> None:
        self.db = connect(state_dir)
        self.db.executescript(_SCHEMA)
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(dreams)")}
        if "client" not in columns:  # stores from before assistant dreams
            self.db.execute("ALTER TABLE dreams ADD COLUMN client TEXT")
        self._lock = threading.Lock()

    def start(
        self,
        trigger: str,
        *,
        dry_run: bool = False,
        base_sha: str = "",
        client: str | None = None,
    ) -> Dream:
        if trigger not in TRIGGERS:
            raise ValueError(f"unknown trigger {trigger!r}")
        started = now()
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO dreams (started, trigger, dry_run, base_sha, client) "
                "VALUES (?, ?, ?, ?, ?)",
                (started, trigger, int(dry_run), base_sha, client),
            )
        return Dream(
            int(cur.lastrowid or 0), started, trigger, dry_run, base_sha=base_sha, client=client
        )

    def save(self, dream: Dream) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE dreams SET finished = ?, base_sha = ?, commits = ?, proposals = ?, "
                "cost_eur = ?, error = ?, log_page = ?, dry_run_result = ?, reverted = ? "
                "WHERE id = ?",
                (
                    dream.finished,
                    dream.base_sha,
                    json.dumps(dream.commits),
                    json.dumps(dream.proposals),
                    dream.cost_eur,
                    dream.error[:1000],
                    dream.log_page,
                    json.dumps(dream.dry_run_result),
                    dream.reverted,
                    dream.id,
                ),
            )

    def finish(self, dream: Dream) -> None:
        dream.finished = now()
        self.save(dream)

    def get(self, dream_id: int) -> Dream | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM dreams WHERE id = ?", (dream_id,)).fetchone()
        return None if row is None else _dream(row)

    def recent(self, limit: int = 30) -> list[Dream]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM dreams ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_dream(r) for r in rows]

    def last(self, *, real: bool = True) -> Dream | None:
        """The latest dream (only those that could write, and no patterns run, unless `real`
        is False)."""
        where = "WHERE dry_run = 0 AND trigger != 'patterns' " if real else ""
        with self._lock:
            row = self.db.execute(
                f"SELECT * FROM dreams {where}ORDER BY id DESC LIMIT 1"  # noqa: S608 - fixed text
            ).fetchone()
        return None if row is None else _dream(row)

    def running_assistant(self, since: str = "") -> list[Dream]:
        """Dreams in an assistant (assistant dreams and patterns sessions) still running (their
        session not read finished yet), newest first; only those started at or after `since`
        (an ISO time) when given."""
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM dreams WHERE trigger IN ('assistant', 'patterns') "
                "AND client IS NOT NULL AND finished IS NULL AND started >= ? ORDER BY id DESC",
                (since,),
            ).fetchall()
        return [_dream(r) for r in rows]

    def last_in_assistant(self, trigger: str) -> Dream | None:
        """The latest dream in an assistant with this trigger (the Patterns page's session)."""
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM dreams WHERE trigger = ? AND client IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                (trigger,),
            ).fetchone()
        return None if row is None else _dream(row)

    def number_on(self, day: str, dream_id: int) -> int:
        """Which dream of the day (1, 2, ...) this one is: the N of its log page's name."""
        with self._lock:
            row = self.db.execute(
                "SELECT COUNT(*) AS n FROM dreams WHERE started LIKE ? AND id <= ?",
                (day + "%", dream_id),
            ).fetchone()
        return int(row["n"])


def _dream(row: Any) -> Dream:
    return Dream(
        id=row["id"],
        started=row["started"],
        trigger=row["trigger"],
        dry_run=bool(row["dry_run"]),
        finished=row["finished"],
        base_sha=row["base_sha"],
        commits=json.loads(row["commits"]),
        proposals=json.loads(row["proposals"]),
        cost_eur=row["cost_eur"],
        error=row["error"],
        log_page=row["log_page"],
        dry_run_result=json.loads(row["dry_run_result"]),
        reverted=row["reverted"],
        client=row["client"],
    )
