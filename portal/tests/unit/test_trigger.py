"""When a dream is due (dreams/trigger.py), the scheduler, and the nightly ideas fallback."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from portal.dreams.store import DreamStore
from portal.dreams.trigger import Due, Rule, Scheduler, Window, dreamt_within, evaluate
from portal.ideas.job import IdeasJob
from portal.ideas.store import IdeaStore, Run
from portal.processing.llm import LLM
from portal.wikiclient import WikiClient, WikiUnavailable
from tests.conftest import _find, make_budget, priced_models

NOW = dt.datetime(2026, 9, 29, 12, 0, tzinfo=dt.UTC)
RULE = Rule(cooldown_hours=24, min_changes=5, max_days=7, idle_minutes=60)
PAGE = "wiki/projects/shed"
DAY_AND_A_QUARTER = dt.timedelta(hours=30)
FIVE_HOURS = dt.timedelta(hours=5)


def ts(delta: dt.timedelta) -> str:
    return (NOW - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def h(hours: float) -> dt.timedelta:
    return dt.timedelta(hours=hours)


def m(minutes: float) -> dt.timedelta:
    return dt.timedelta(minutes=minutes)


def commit(
    ago: dt.timedelta, *paths: str, author: str = "claude.ai", summary: str = "Write"
) -> dict[str, Any]:
    # git's %aI, with an offset rather than Z
    date = (NOW - ago).astimezone(dt.timezone(dt.timedelta(hours=2))).isoformat()
    files = [{"status": "M", "path": p + ".md"} for p in paths]
    return {"commit": "a" * 12, "author": author, "date": date, "summary": summary, "files": files}


def chat(ago: dt.timedelta, n: int) -> dict[str, Any]:
    return {"id": f"raw/chats/claude/c{n}", "created": ts(ago)}


def pages(n: int) -> set[str]:
    return {f"wiki/p{i}" for i in range(n)}


def run(
    *,
    last: dt.timedelta | None = DAY_AND_A_QUARTER,
    commits: list[dict[str, Any]] | None = None,
    chats: list[dict[str, Any]] | None = None,
    reviewed: set[str] | None = None,
    spend: list[dict[str, Any]] | None = None,
    windows: list[Window] | None = None,
    budget: bool = True,
    rule: Rule = RULE,
) -> Due:
    return evaluate(
        NOW,
        None if last is None else NOW - last,
        commits or [],
        chats or [],
        reviewed if reviewed is not None else pages(10) | {PAGE},
        spend or [],
        windows or [],
        budget,
        rule,
        "over BUDGET_AUTOMATION_EUR",
    )


def five_changes(ago: dt.timedelta = FIVE_HOURS) -> list[dict[str, Any]]:
    return [commit(ago, f"wiki/p{i}") for i in range(5)]


def reason(due: Due, prefix: str) -> str:
    return next(r for r in due.reasons if r.startswith(prefix))


def test_due_when_all_four_hold() -> None:
    due = run(commits=five_changes())
    assert due.due
    assert [r.split(":")[0] for r in due.reasons] == ["cooldown", "changes", "idle", "budget"]
    assert reason(due, "cooldown") == "cooldown: last dream 30 h ago (needs 24 h)"
    assert reason(due, "changes").startswith("changes: 5 since the last dream")
    assert reason(due, "idle") == "idle: no activity in the last 60 min"
    assert reason(due, "budget") == "budget: allows the cheap passes"


def test_cooldown_blocks() -> None:
    due = run(last=h(3), commits=five_changes(h(2)))
    assert not due.due
    assert reason(due, "cooldown") == "cooldown: last dream 3 h ago, needs 24 h"


def test_min_changes_and_the_max_days_rule() -> None:
    # 0 changes: never due, however long ago the last dream was
    assert not run(last=h(24 * 30)).due
    # 1 change: only after max days
    one = [commit(h(5), "wiki/p1")]
    due = run(last=h(30), commits=one)
    assert not due.due
    assert reason(due, "changes") == (
        "changes: 1 since the last dream (0 new chat(s), 1 reviewed page(s); "
        "needs 5, or 1 after 7 days)"
    )
    assert run(last=h(24 * 8), commits=one).due
    # 4 changes within max days: not enough; 5: enough
    assert not run(commits=five_changes()[:4]).due
    assert run(commits=five_changes()).due


def test_what_counts_as_a_change() -> None:
    changes = [
        commit(h(5), "wiki/p1"),
        commit(h(4), "wiki/p1", "wiki/p2"),  # p1 again: distinct pages count once
        commit(h(4), "wiki/drafty"),  # not a reviewed page
        commit(h(40), "wiki/p3"),  # before the last dream
        commit(h(3), "wiki/p4", author="process:dream"),  # a dream's own change
        commit(h(3), "wiki/fresh", author="portal", summary="Review wiki/fresh.md"),
    ]
    chats = [chat(h(2), 1), chat(h(2), 2), chat(h(50), 3)]  # the third came before the dream
    due = run(commits=changes, chats=chats)
    # p1, p2, the page marked reviewed, and two chats
    assert reason(due, "changes").startswith(
        "changes: 5 since the last dream (2 new chat(s), 3 reviewed page(s)"
    )
    assert due.due


def test_dream_commits_do_not_count() -> None:
    dreamt = [commit(m(5), f"wiki/p{i}", author="process:dream") for i in range(6)]
    due = run(commits=dreamt)
    assert not due.due
    assert reason(due, "changes").startswith("changes: 0")
    assert reason(due, "idle") == "idle: no activity in the last 60 min"  # not activity either


def test_a_review_commit_counts() -> None:
    review = commit(h(2), "wiki/new-page", author="portal", summary="Review wiki/new-page.md")
    due = run(last=h(24 * 8), commits=[review], reviewed=set())
    assert due.due and reason(due, "changes").startswith("changes: 1 ")


def test_idle_broken_by_a_recent_commit() -> None:
    due = run(commits=[*five_changes(), commit(m(12), "wiki/other")])
    assert not due.due
    assert reason(due, "idle") == "idle: a wiki commit by claude.ai 12 min ago (needs 60 min quiet)"
    # older than the idle window: fine
    assert run(commits=[*five_changes(), commit(m(61), "wiki/other")]).due


def test_idle_broken_by_a_recent_spend_line() -> None:
    spend = [{"ts": ts(m(5)), "model": "glm-5-3"}, {"ts": ts(m(90)), "model": "glm-5-3"}]
    due = run(commits=five_changes(), spend=spend)
    assert not due.due
    assert reason(due, "idle") == "idle: a model call (glm-5-3) 5 min ago (needs 60 min quiet)"


def test_the_portals_own_runs_are_not_activity() -> None:
    spend = [{"ts": ts(m(20)), "model": "mistral-large"}, {"ts": ts(m(4)), "model": "glm-5-3"}]
    commits = [*five_changes(), commit(m(20), "wiki/p8", author="portal")]
    dream: Window = (NOW - m(25), NOW - m(15))  # a dry run, say: covers the commit and a call
    ideas: Window = (NOW - m(5), None)  # an ideas run still going: counts up to now
    due = run(commits=commits, spend=spend, windows=[dream, ideas])
    assert due.due, due.reasons
    # the same activity outside those windows breaks idleness
    assert not run(commits=commits, spend=spend, windows=[dream]).due
    assert not run(commits=commits, spend=spend, windows=[ideas]).due


def test_budget_refusal_blocks() -> None:
    due = run(commits=five_changes(), budget=False)
    assert not due.due
    assert reason(due, "budget") == (
        "budget: refuses even the cheap passes (over BUDGET_AUTOMATION_EUR)"
    )


def test_no_previous_dream() -> None:
    due = run(last=None, chats=[chat(h(24 * 100), 1)])
    assert due.due
    assert reason(due, "cooldown") == "cooldown: no dream yet"
    assert reason(due, "changes") == (
        "changes: 1 so far, no dream yet (1 new chat(s), 0 reviewed page(s); needs 1)"
    )
    assert not run(last=None).due  # nothing to dream about


def test_dreamt_within(tmp_path: Path) -> None:
    store = DreamStore(tmp_path)
    assert not dreamt_within(store)
    store.start("dry run", dry_run=True)
    assert not dreamt_within(store)  # a dry run makes no ideas
    store.start("manual")
    now = dt.datetime.now(dt.UTC)
    assert dreamt_within(store, 24, clock=lambda: now)
    assert not dreamt_within(store, 24, clock=lambda: now + h(25))


# --- the scheduler -------------------------------------------------------------------


class FakeJob:
    def __init__(self) -> None:
        self.running = False
        self.triggered: list[str] = []

    def trigger(self, trigger: str, *, dry_run: bool = False) -> bool:
        self.triggered.append(trigger)
        return True


class Stop(Exception):
    pass


def sleeper(ticks: int) -> tuple[list[float], Any]:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        if len(slept) == ticks:
            raise Stop
        slept.append(seconds)

    return slept, sleep


def due_check(result: bool) -> Any:
    async def check() -> Due:
        return Due(result, ["cooldown: no dream yet"])

    return check


async def test_review_mode_never_triggers() -> None:
    job = FakeJob()
    slept, sleep = sleeper(3)
    scheduler = Scheduler(due_check(True), job, mode="review", every_minutes=60, sleep=sleep)
    scheduler.start()
    assert scheduler._task is None
    assert not await scheduler.tick()
    assert job.triggered == [] and slept == []


async def test_auto_mode_triggers_when_due() -> None:
    job = FakeJob()
    slept, sleep = sleeper(3)
    scheduler = Scheduler(due_check(True), job, mode="auto", every_minutes=60, sleep=sleep)
    with pytest.raises(Stop):
        await scheduler._loop()
    assert slept == [3600, 3600, 3600]
    assert job.triggered == ["auto", "auto", "auto"]


async def test_auto_mode_waits_when_not_due_busy_or_unreachable() -> None:
    job = FakeJob()
    _, sleep = sleeper(1)
    scheduler = Scheduler(due_check(False), job, mode="auto", every_minutes=5, sleep=sleep)
    assert not await scheduler.tick()
    job.running = True
    scheduler.check = due_check(True)
    assert not await scheduler.tick()  # a run is going: not even checked

    async def unreachable() -> Due:
        raise WikiUnavailable("down")

    job.running = False
    scheduler.check = unreachable
    assert not await scheduler.tick()
    assert job.triggered == []


# --- the nightly ideas fallback ----------------------------------------------------------


def _ideas(tmp_path: Path, skip: Any) -> IdeasJob:
    state, models = tmp_path / "state", priced_models(tmp_path / "models")

    def llm() -> LLM:
        return LLM("http://x", "k")

    return IdeasJob(
        IdeaStore(state),
        WikiClient("http://127.0.0.1:9", "t" * 40),
        llm,
        _find("config"),
        make_budget(state, models),
        skip_nightly=skip,
    )


@pytest.mark.parametrize(("skip", "runs"), [(None, 1), (lambda: False, 1), (lambda: True, 0)])
async def test_nightly_ideas_skip_after_a_dream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skip: Any, runs: int
) -> None:
    job = _ideas(tmp_path, skip)
    reasons: list[str] = []

    async def fake_run(reason: str = "manual", *_: Any) -> Run | None:
        reasons.append(reason)
        return None

    monkeypatch.setattr(job, "run", fake_run)
    await job.nightly()
    assert reasons == ["nightly"] * runs
