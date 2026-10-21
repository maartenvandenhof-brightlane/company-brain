"""Ideas and their state, in assistant.sqlite (portal state, not the wiki).

An idea is a suggestion to the owner, not knowledge, so it never goes into the wiki. Its
state is the owner's feedback: new, useful, not useful, dismissed, done, snoozed (for 7 or
30 days, then new again) or already doing. The next generation run is shown the useful
ideas as examples to follow, the not useful, dismissed and already-doing ones as what not to
suggest again, and the scorer sees every idea of the last 180 days with its state. Each
generation run is recorded, so the next one knows from when to read the wiki.

  ideas        kind, text, counter; the goal or loop it serves {type, ref, text}, why now,
               the next step, its scores {relevance, novelty, actionability, why_now, total}
  idea_taste   one row: the note on the owner's taste (taste.py), when it was written, and
               how many reactions came since (it is rewritten after every TASTE_EVERY)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from portal.assistant import connect

KINDS = ("follow-up", "continue", "connect", "challenge")
STATES = ("new", "useful", "not_useful", "dismissed", "done", "snoozed", "already_doing")
OPEN_STATES = ("new", "useful")  # still shown in the feed
FEEDBACK_STATES = ("not_useful", "dismissed", "already_doing")  # told to the next run
SEEN_STATES = ("new", "useful", "snoozed")  # "already seen": still on the page, or coming back
SNOOZE_DAYS = (7, 30)
DEDUPE_DAYS = 90
TASTE_EVERY = 10  # reactions between two rewrites of the taste note

_RUNS = """
CREATE TABLE IF NOT EXISTS idea_runs (
    id INTEGER PRIMARY KEY,
    started TEXT NOT NULL,
    finished TEXT,
    since TEXT,
    model TEXT NOT NULL,
    pages INTEGER NOT NULL DEFAULT 0,
    proposed INTEGER NOT NULL DEFAULT 0,
    kept INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'recent'
);
"""
_IDEAS = f"""
CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN {KINDS!r}),
    text TEXT NOT NULL,
    counter TEXT NOT NULL DEFAULT '',
    cites TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'new' CHECK (state IN {STATES!r}),
    run_id INTEGER REFERENCES idea_runs(id),
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    serves TEXT NOT NULL DEFAULT '{{}}',
    why_now TEXT NOT NULL DEFAULT '',
    next_step TEXT NOT NULL DEFAULT '',
    snoozed_until TEXT,
    score TEXT NOT NULL DEFAULT '{{}}'
);
"""
_TABLES = f"""
{_IDEAS}
CREATE INDEX IF NOT EXISTS ideas_state ON ideas(state, updated);
CREATE TABLE IF NOT EXISTS idea_taste (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    note TEXT NOT NULL DEFAULT '',
    updated TEXT,
    reactions INTEGER NOT NULL DEFAULT 0
);
"""
# The columns a store from before the scored ideas has (copied when the table is rebuilt).
_OLD_COLUMNS = "id, kind, text, counter, cites, fingerprint, state, run_id, created, updated"
_WORDS = re.compile(r"\w+")


def now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fingerprint(kind: str, text: str) -> str:
    """The same idea worded with other case or punctuation counts as the same."""
    words = " ".join(_WORDS.findall(text.lower()))
    return hashlib.sha256(f"{kind}\n{words}".encode()).hexdigest()[:32]


def ago(days: int) -> str:
    then = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    return then.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Idea:
    kind: str
    text: str
    cites: list[str]
    counter: str = ""  # challenge: the strongest counterargument
    id: int = 0
    state: str = "new"
    created: str = ""
    updated: str = ""
    serves: dict[str, str] = field(default_factory=dict)  # {type, ref, text}
    why_now: str = ""
    next_step: str = ""
    snoozed_until: str | None = None
    score: dict[str, int] = field(default_factory=dict)  # the four scores and their total
    duplicate_of: int | None = None  # not stored: the past idea the scorer says it repeats

    @property
    def total(self) -> int:
        return int(self.score.get("total", 0))


@dataclass
class Taste:
    note: str = ""
    updated: str | None = None
    reactions: int = 0  # since the note was last written


@dataclass
class Run:
    id: int
    started: str
    finished: str | None
    since: str | None
    model: str
    pages: int = 0
    proposed: int = 0
    kept: int = 0
    error: str = ""
    scope: str = "recent"  # recent | wiki (see ideas.progress.SCOPES)
    dropped: list[str] = field(default_factory=list)  # not stored: why ideas were left out


class IdeaStore:
    def __init__(self, state_dir: Path) -> None:
        self.db = connect(state_dir)
        self.db.executescript(_RUNS)
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(idea_runs)")}
        if "scope" not in columns:  # stores from before the Ideate button
            self.db.execute("ALTER TABLE idea_runs ADD COLUMN scope TEXT NOT NULL DEFAULT 'recent'")
        self._rebuild_ideas()
        self.db.executescript(_TABLES)
        self.db.execute("INSERT OR IGNORE INTO idea_taste (id, reactions) VALUES (1, 0)")
        self._lock = threading.Lock()

    def _rebuild_ideas(self) -> None:
        """Stores from before the scored ideas: their CHECK lists fewer states, which SQLite
        cannot alter, so the table is rebuilt in one transaction, keeping every row and id."""
        row = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'ideas'"
        ).fetchone()
        if row is None or "'already_doing'" in str(row["sql"]):
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("ALTER TABLE ideas RENAME TO ideas_before")
            self.db.execute(_IDEAS)
            self.db.execute(
                f"INSERT INTO ideas ({_OLD_COLUMNS}) SELECT {_OLD_COLUMNS} FROM ideas_before"  # noqa: S608 - fixed text
            )
            self.db.execute("DROP TABLE ideas_before")
            self.db.execute("COMMIT")
        except sqlite3.Error:
            self.db.execute("ROLLBACK")
            raise

    # --- runs ------------------------------------------------------------------

    def start_run(self, since: str | None, model: str, scope: str = "recent") -> Run:
        started = now()
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO idea_runs (started, since, model, scope) VALUES (?, ?, ?, ?)",
                (started, since, model, scope),
            )
        return Run(int(cur.lastrowid or 0), started, None, since, model, scope=scope)

    def finish_run(self, run: Run) -> None:
        run.finished = now()
        with self._lock:
            self.db.execute(
                "UPDATE idea_runs SET finished = ?, model = ?, pages = ?, proposed = ?, kept = ?, "
                "error = ? WHERE id = ?",
                (run.finished, run.model, run.pages, run.proposed, run.kept, run.error[:500],
                 run.id),
            )  # fmt: skip

    def last_run(self) -> Run | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM idea_runs ORDER BY id DESC LIMIT 1").fetchone()
        return None if row is None else _run(row)

    def runs(self, limit: int = 30) -> list[Run]:
        """The latest runs, newest first (listed on the Ideas tab; their windows are left out
        of the dream trigger's idle check)."""
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM idea_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_run(r) for r in rows]

    def last_finished(self) -> Run | None:
        """The latest run that has ended, well or not (what the home page reports)."""
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM idea_runs WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else _run(row)

    def last_success(self) -> Run | None:
        """The latest "recent" run that finished without an error: the next one reads from
        its start. A whole-wiki run reads only part of the wiki, so it does not count."""
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM idea_runs WHERE finished IS NOT NULL AND error = '' "
                "AND scope = 'recent' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else _run(row)

    # --- ideas -----------------------------------------------------------------

    def add(self, ideas: list[Idea], run_id: int | None = None) -> list[Idea]:
        """Store new ideas; one like an idea of the last DEDUPE_DAYS (any state) is skipped."""
        added: list[Idea] = []
        stamp = now()
        cutoff = ago(DEDUPE_DAYS)
        with self._lock:
            for idea in ideas:
                fp = fingerprint(idea.kind, idea.text)
                old = self.db.execute(
                    "SELECT id, created FROM ideas WHERE fingerprint = ?", (fp,)
                ).fetchone()
                if old is not None and old["created"] >= cutoff:
                    continue
                if old is not None:  # older than the window: may be suggested again
                    self.db.execute("DELETE FROM ideas WHERE id = ?", (old["id"],))
                cur = self.db.execute(
                    "INSERT INTO ideas (kind, text, counter, cites, fingerprint, run_id, created, "
                    "updated, serves, why_now, next_step, score) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (idea.kind, idea.text, idea.counter, json.dumps(idea.cites), fp, run_id,
                     stamp, stamp, json.dumps(idea.serves), idea.why_now, idea.next_step,
                     json.dumps(idea.score)),
                )  # fmt: skip
                idea.id, idea.created, idea.updated = int(cur.lastrowid or 0), stamp, stamp
                added.append(idea)
        return added

    def set_state(self, idea_id: int, state: str, *, days: int | None = None) -> bool:
        """The owner's answer. Snoozed needs `days` (one of SNOOZE_DAYS): the idea is new
        again once they have passed."""
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}")
        until = None
        if state == "snoozed":
            if days not in SNOOZE_DAYS:
                raise ValueError(f"snooze for one of {SNOOZE_DAYS} days, not {days!r}")
            until = (dt.datetime.now(dt.UTC) + dt.timedelta(days=days)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        with self._lock:
            cur = self.db.execute(
                "UPDATE ideas SET state = ?, snoozed_until = ?, updated = ? WHERE id = ?",
                (state, until, now(), idea_id),
            )
        return cur.rowcount == 1

    def wake(self) -> int:
        """Snoozed ideas whose date has passed are new again; how many. Called whenever
        ideas are loaded, and before a run."""
        stamp = now()
        with self._lock:
            cur = self.db.execute(
                "UPDATE ideas SET state = 'new', snoozed_until = NULL, updated = ? "
                "WHERE state = 'snoozed' AND snoozed_until <= ?",
                (stamp, stamp),
            )
        return cur.rowcount

    def get(self, idea_id: int) -> Idea | None:
        self.wake()
        with self._lock:
            row = self.db.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
        return None if row is None else _idea(row)

    def ideas(
        self,
        states: tuple[str, ...] = OPEN_STATES,
        limit: int = 50,
        *,
        by: str = "created",
        since: str = "",
    ) -> list[Idea]:
        """Ideas in these states created since `since`: newest first (by creation, or by the
        last state change), or by score (the highest total first, then the newest)."""
        self.wake()
        order = {
            "updated": "updated DESC",
            "score": "CAST(COALESCE(json_extract(score, '$.total'), 0) AS INTEGER) DESC, "
            "created DESC",
        }.get(by, "created DESC")
        marks = ",".join("?" * len(states))
        with self._lock:
            rows = self.db.execute(
                f"SELECT * FROM ideas WHERE state IN ({marks}) AND created >= ? "  # noqa: S608 - fixed names and placeholders only
                f"ORDER BY {order}, id DESC LIMIT ?",
                (*states, since, limit),
            ).fetchall()
        return [_idea(r) for r in rows]

    def useful(self, limit: int = 10) -> list[Idea]:
        """The latest ideas the owner marked useful: examples to follow."""
        return self.ideas(("useful",), limit, by="updated")

    def feedback(self, limit: int = 20) -> list[Idea]:
        """The latest ideas the owner found not useful, dismissed or is already doing."""
        return self.ideas(FEEDBACK_STATES, limit, by="updated")

    def seen(self, limit: int = 30) -> list[Idea]:
        """The ideas still on the page or snoozed: "already seen"."""
        return self.ideas(SEEN_STATES, limit)

    def past(self, days: int, limit: int = 200) -> list[Idea]:
        """Every idea of the last `days`, whatever its state (scoring, near-duplicates)."""
        return self.ideas(STATES, limit, since=ago(days))

    def reactions(self, limit: int = 30) -> list[Idea]:
        """The ideas the owner answered most recently (any state but new)."""
        return self.ideas(tuple(s for s in STATES if s != "new"), limit, by="updated")

    def counts(self) -> dict[str, int]:
        self.wake()
        with self._lock:
            rows = self.db.execute("SELECT state, COUNT(*) AS n FROM ideas GROUP BY state")
            found = {str(r["state"]): int(r["n"]) for r in rows}
        return {s: found.get(s, 0) for s in STATES}

    # --- the taste note ----------------------------------------------------------

    def taste(self) -> Taste:
        with self._lock:
            row = self.db.execute("SELECT * FROM idea_taste WHERE id = 1").fetchone()
        return Taste(str(row["note"]), row["updated"], int(row["reactions"]))

    def react(self) -> int:
        """Count one reaction of the owner; how many there are since the note was written."""
        with self._lock:
            self.db.execute("UPDATE idea_taste SET reactions = reactions + 1 WHERE id = 1")
            row = self.db.execute("SELECT reactions FROM idea_taste WHERE id = 1").fetchone()
        return int(row["reactions"])

    def set_taste(self, note: str) -> Taste:
        """A new note (rewritten, or edited by the owner): the reaction count starts over."""
        stamp = now()
        with self._lock:
            self.db.execute(
                "UPDATE idea_taste SET note = ?, updated = ?, reactions = 0 WHERE id = 1",
                (note, stamp),
            )
        return Taste(note, stamp, 0)


def _json_dict(value: str | None) -> dict[str, object]:
    try:
        data = json.loads(value) if value else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _idea(row: sqlite3.Row) -> Idea:
    return Idea(
        kind=row["kind"],
        text=row["text"],
        cites=[str(c) for c in json.loads(row["cites"])],
        counter=row["counter"],
        id=row["id"],
        state=row["state"],
        created=row["created"],
        updated=row["updated"],
        serves={str(k): str(v) for k, v in _json_dict(row["serves"]).items() if v is not None},
        why_now=row["why_now"],
        next_step=row["next_step"],
        snoozed_until=row["snoozed_until"],
        score={str(k): v for k, v in _json_dict(row["score"]).items() if isinstance(v, int)},
    )


def _run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        started=row["started"],
        finished=row["finished"],
        since=row["since"],
        model=row["model"],
        pages=row["pages"],
        proposed=row["proposed"],
        kept=row["kept"],
        error=row["error"],
        scope=row["scope"],
    )
