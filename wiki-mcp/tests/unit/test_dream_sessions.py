"""Assistant dreams' sessions (dream/sessions.py) and the dream log they end with."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from wiki_mcp.dream import log as dream_log
from wiki_mcp.dream.log import Commit, DreamLog
from wiki_mcp.dream.sessions import OBSERVE_TTL_S, SESSION_TTL_S, DreamSessions, SessionError

STARTED = "2026-09-30T08:00:00Z"
T0 = 1_790_000_000.0


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sessions(tmp_path: Path, clock: Clock) -> DreamSessions:
    return DreamSessions(tmp_path / "dreams.sqlite", clock=clock)


def test_open_read_and_state(sessions: DreamSessions) -> None:
    session = sessions.create(3, "claude.ai", STARTED, 2, "abc123def456", "0123456789ab")
    got = sessions.get(3)
    assert got is not None and got == session
    assert sessions.current() == session
    assert sessions.state(session) == {
        "dream": 3,
        "kind": "dream",
        "status": "open",
        "client": "claude.ai",
        "started": STARTED,
        "number": 2,
        "base": "0123456789ab",
        "since_base": "abc123def456",
        "opened": "2026-09-21T14:13:20Z",
        "expires": "2026-09-21T16:13:20Z",
        "finished": None,
        "ended": None,
        "passes_done": {},
        "passes_to_do": ["duplicates", "deduction", "induction", "hot-memory"],
        "commits": [],
        "proposals": [],
        "flagged": [],
        "skipped": [],
        "log_page": None,
    }
    assert sessions.get(4) is None


def test_what_the_passes_did_is_kept(sessions: DreamSessions) -> None:
    session = sessions.create(3, "chatgpt", STARTED, 1, None)
    session.passes["induction"] = ["1"]
    session.record_commit("f" * 40, "hygiene", ["wiki/projects/shed"], ["wiki/projects/shed: x"])
    session.record_proposal("wiki/proposals/p", "deduction", "A change", speculative=True)
    session.log.flag("hygiene", "wiki/projects/shed: orphan")
    session.log.models["induction"] = "chatgpt"
    sessions.save(session)
    again = sessions.get(3)
    assert again is not None
    assert again.passes == {"induction": ["1"]}
    assert again.commits == [{"sha": "f" * 40, "pass": "hygiene", "pages": ["wiki/projects/shed"]}]
    assert again.proposals == [
        {"id": "wiki/proposals/p", "pass": "deduction", "summary": "A change", "speculative": True}
    ]
    assert again.log == session.log
    assert again.log.proposals == ["[wiki/proposals/p](/wiki/proposals/p.md) deduction: "
                                   "A change (speculative)"]  # fmt: skip
    assert again.to_do == ["duplicates", "deduction", "hot-memory"]


def test_one_open_session_and_one_session_per_dream(sessions: DreamSessions, clock: Clock) -> None:
    sessions.create(3, "claude.ai", STARTED, 1, None)
    with pytest.raises(SessionError, match="dream 3 is still open"):
        sessions.create(4, "claude.ai", STARTED, 2, None)
    with pytest.raises(SessionError, match="had its session already"):
        sessions.create(3, "claude.ai", STARTED, 1, None)
    clock.now += SESSION_TTL_S + 1  # expired: another may open, this one never again
    expired = sessions.get(3)
    assert expired is not None and sessions.state(expired)["status"] == "expired"
    assert sessions.current() is None
    with pytest.raises(SessionError, match="had its session already"):
        sessions.create(3, "claude.ai", STARTED, 1, None)
    assert sessions.create(4, "claude.ai", STARTED, 2, None).expires == clock.now + SESSION_TTL_S


def test_finished_once(sessions: DreamSessions, clock: Clock) -> None:
    session = sessions.create(3, "claude.ai", STARTED, 1, None)
    sessions.finish(session, "closed by the owner", "wiki/maintenance/dream-2026-09-30-1", T0 + 5)
    stored = sessions.get(3)
    assert stored is not None
    state = sessions.state(stored)
    assert state["status"] == "finished" and state["ended"] == "closed by the owner"
    assert state["finished"] == "2026-09-21T14:13:25Z"
    assert state["log_page"] == "wiki/maintenance/dream-2026-09-30-1"
    with pytest.raises(SessionError, match="closed already"):
        sessions.finish(stored, "finished", "x", T0 + 6)
    stored.passes["deduction"] = []
    sessions.save(stored)  # a finished session is never changed
    assert sessions.get(3).passes == {}  # type: ignore[union-attr]
    clock.now += SESSION_TTL_S + 1
    assert sessions.state(stored)["status"] == "finished"  # not expired


def test_a_patterns_session(sessions: DreamSessions) -> None:
    session = sessions.create(3, "chatgpt", STARTED, 1, None, kind="patterns")
    assert session.trigger == "patterns" and session.to_do == ["patterns"]
    got = sessions.get(3)
    assert got is not None and got.kind == "patterns" and got.trigger == "patterns"
    state = sessions.state(got)
    assert state["kind"] == "patterns" and state["passes_to_do"] == ["patterns"]
    with pytest.raises(SessionError, match="dream 3 is still open"):  # one open session of any kind
        sessions.create(4, "chatgpt", STARTED, 2, None)
    with pytest.raises(SessionError, match="'kind'"):
        sessions.create(5, "chatgpt", STARTED, 3, None, kind="ideas")


def test_a_state_from_before_patterns_sessions(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE sessions (dream INTEGER PRIMARY KEY, client TEXT NOT NULL, started TEXT "
        "NOT NULL, number INTEGER NOT NULL, base TEXT NOT NULL DEFAULT '', since_base TEXT, "
        "opened REAL NOT NULL, expires REAL NOT NULL, finished REAL, ended TEXT NOT NULL "
        "DEFAULT '', passes TEXT NOT NULL DEFAULT '{}', commits TEXT NOT NULL DEFAULT '[]', "
        "proposals TEXT NOT NULL DEFAULT '[]', log TEXT NOT NULL DEFAULT '{}', log_page TEXT "
        "NOT NULL DEFAULT '', work TEXT NOT NULL DEFAULT '{}')"
    )
    db.execute(
        "INSERT INTO sessions (dream, client, started, number, opened, expires) "
        "VALUES (2, 'claude.ai', ?, 1, 1, 2)",
        (STARTED,),
    )
    db.commit()
    db.close()
    old = DreamSessions(path).get(2)
    assert old is not None and old.kind == "dream" and old.trigger == "assistant"


def test_a_long_chats_window_answers(sessions: DreamSessions, clock: Clock) -> None:
    chat = "raw/chats/claude/long"
    assert sessions.keep_window(chat, 1, 3, {"observations": [1]}) == [1]
    assert sessions.keep_window(chat, 0, 3, {"observations": [0]}) == [0, 1]
    assert sessions.keep_window(chat, 1, 3, {"observations": ["again"]}) == [0, 1]  # replaced
    assert sessions.windows_of(chat) == [{"observations": [0]}, {"observations": ["again"]}]
    # cut into another number of windows: the answers kept go
    assert sessions.keep_window(chat, 0, 2, {"observations": []}) == [0]
    clock.now += OBSERVE_TTL_S + 1  # too old: gone at the next keep
    assert sessions.keep_window("raw/chats/claude/other", 0, 2, {}) == [0]
    assert sessions.windows_of(chat) == []
    sessions.drop_windows("raw/chats/claude/other")
    assert sessions.windows_of("raw/chats/claude/other") == []


@pytest.mark.parametrize(
    ("args", "why"),
    [
        ((0, "claude.ai", STARTED, 1, None), "positive integer"),
        ((3, "", STARTED, 1, None), "'client'"),
        ((3, "claude.ai\nDream: 9", STARTED, 1, None), "'client'"),
        ((3, "claude.ai", "yesterday", 1, None), "'started'"),
        ((3, "claude.ai", STARTED, 0, None), "'number'"),
        ((3, "claude.ai", STARTED, 1, "HEAD~1"), "'since_base'"),
    ],
)
def test_refused_values(sessions: DreamSessions, args: tuple[object, ...], why: str) -> None:
    with pytest.raises(ValueError, match=why):
        sessions.create(*args)  # type: ignore[arg-type]
    assert sessions.current() is None


# ------------------------------------------------------------------ the log


@dataclass
class Run:
    id: int = 7
    started: str = "2026-09-30T08:00:00Z"
    trigger: str = "manual"
    dry_run: bool = False
    base_sha: str = "0123456789ab"


def _log() -> DreamLog:
    out = DreamLog(ingestion=["2 of 3 chats filed"])
    out.commits.append(Commit("a" * 40, "hygiene", ["wiki/projects/shed: collapsed a checkbox"]))
    out.proposed("wiki/proposals/p", "deduction", "The plan changed")
    out.flag("hygiene", "wiki/projects/shed: orphan")
    out.skip("induction", "skipped: budget")
    out.models["deduction"] = "glm-5-3"
    return out


def test_a_portal_dream_log_is_unchanged() -> None:
    text = dream_log.render(Run(), _log(), 2, "2026-09-30T08:10:00Z", 0.01234)
    assert text == (
        "# Dream 2026-09-30 #2\n\n"
        "Dream 7. Trigger: manual. Started 2026-09-30T08:00:00Z, finished 2026-09-30T08:10:00Z. "
        "Models: deduction: glm-5-3. Cost: EUR 0.0123. Base commit: `0123456789ab`.\n\n"
        "## Ingestion\n\n- 2 of 3 chats filed\n\n"
        "## Direct changes\n\n"
        f"### `{'a' * 12}` hygiene\n\n- wiki/projects/shed: collapsed a checkbox\n\n"
        "## Proposals\n\n- [wiki/proposals/p](/wiki/proposals/p.md) deduction: The plan changed\n\n"
        "## Flagged\n\n- hygiene: wiki/projects/shed: orphan\n\n"
        "## Skipped\n\n- induction: skipped: budget\n"
    )
    assert dream_log.page_id(Run(), 2) == "wiki/maintenance/dream-2026-09-30-2"


def test_an_assistant_dream_log_names_the_client() -> None:
    text = dream_log.render(
        Run(trigger="assistant"),
        DreamLog(models={"deduction": "claude.ai"}),
        1,
        "2026-09-30T09:00:00Z",
        0.0,
        client="claude.ai",
        ended="expired",
    )
    header = text.split("\n")[2]
    assert header == (
        "Dream 7. Trigger: assistant. Started 2026-09-30T08:00:00Z, finished "
        "2026-09-30T09:00:00Z (ended: expired). Models: deduction: claude.ai. "
        "Client: claude.ai. Base commit: `0123456789ab`."
    )
    assert "Cost" not in text and "EUR" not in text


def test_a_dream_log_round_trips() -> None:
    log = _log()
    assert DreamLog.of(log.as_dict()) == log
    assert DreamLog.of({}) == DreamLog()
