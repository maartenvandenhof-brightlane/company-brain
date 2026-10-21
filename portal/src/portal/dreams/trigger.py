"""When a dream is due, and the scheduler that starts one in DREAM_MODE=auto.

A dream is due when all four conditions hold (docs/dreaming.md, "When a dream is due"):

  cooldown  no real (non-dry-run) dream started in the last DREAM_COOLDOWN_HOURS
  changes   enough changed since the last real dream started: new unprocessed imported chats,
            reviewed pages changed by a commit that is not a dream's, pages marked reviewed
            (distinct pages and chats); at least DREAM_MIN_CHANGES, or at least one when the
            last dream is more than DREAM_MAX_DAYS old (no dream yet: infinitely old)
  idle      no wiki commit by anyone but a dream and no model call in LiteLLM's spend log in
            the last DREAM_IDLE_MINUTES, leaving out what falls inside the portal's own runs
            (every dream, dry runs too, and every ideas run, from start to finish, or to now
            while one is going)
  budget    the budget guard allows at least the cheap passes

`evaluate` decides over plain inputs; `DreamTrigger.check` gathers them from the wiki, the
stores, the spend log and the budget. The Dreams page shows the result in both modes; the
`Scheduler` acts on it only in auto mode, every DREAM_CHECK_MINUTES. A wait for idleness
restarts by itself when activity appears, since each check looks again. "Dream now" skips
the first three conditions (dreams/routes.py).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from wiki_mcp.dream.pages import MANAGED

from portal.config import Settings
from portal.dreams.budget import Budget
from portal.dreams.store import DreamStore
from portal.ideas.store import IdeaStore
from portal.usage import UsageLog
from portal.wikiclient import WikiClient, WikiUnavailable

log = logging.getLogger(__name__)

DREAM_AUTHOR = "process:dream"
REVIEW_PREFIX = "Review "  # the subject of the commit that marks a page reviewed
CHEAP_CHARS, CHEAP_TOKENS = 40_000, 2000  # a cheap pass the budget must allow
MAX_COMMITS = 200  # the most GET /recent returns
# A run's window reaches this far past its end: LiteLLM writes its spend line after the
# answer, which can land just after the run recorded that it finished.
WINDOW_SLACK = dt.timedelta(seconds=60)

Window = tuple[dt.datetime, dt.datetime | None]  # a run's start and end (None: still going)


@dataclass(frozen=True)
class Rule:
    cooldown_hours: int = 24  # DREAM_COOLDOWN_HOURS
    min_changes: int = 5  # DREAM_MIN_CHANGES
    max_days: int = 7  # DREAM_MAX_DAYS
    idle_minutes: int = 60  # DREAM_IDLE_MINUTES

    @classmethod
    def of(cls, settings: Settings) -> Rule:
        return cls(
            settings.dream_cooldown_hours,
            settings.dream_min_changes,
            settings.dream_max_days,
            settings.dream_idle_minutes,
        )


@dataclass
class Due:
    due: bool
    reasons: list[str] = field(default_factory=list)  # one per condition, met or not


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def parse_ts(value: str | None) -> dt.datetime | None:
    """An ISO timestamp (`Z` or an offset; none means UTC) as an aware UTC datetime."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def format_ts(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(delta: dt.timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} h" if hours < 48 else f"{hours // 24} days"


def page_of(path: str) -> str:
    return path.removesuffix(".md")


def _inside(when: dt.datetime, windows: Iterable[Window], now: dt.datetime) -> bool:
    return any(start <= when <= (end or now) + WINDOW_SLACK for start, end in windows)


def evaluate(
    now: dt.datetime,
    last_dream: dt.datetime | None,
    commits: list[dict[str, Any]],
    chats: list[dict[str, Any]],
    reviewed: set[str],
    spend: list[dict[str, Any]],
    windows: list[Window],
    budget_allows: bool,
    rule: Rule,
    budget_note: str = "",
) -> Due:
    """Whether a dream is due, and why (or why not), from the inputs `check` gathers.

    `commits` are GET /recent's (newest first or not), `chats` the unprocessed imported
    chats ({id, created}), `reviewed` the ids of the reviewed pages, `spend` LiteLLM's spend
    log lines ({ts, model, ...}), `windows` the portal's own runs."""
    reasons: list[str] = []

    # 1. Cooldown
    if last_dream is None:
        cooled = True
        reasons.append("cooldown: no dream yet")
    else:
        since = now - last_dream
        cooled = since >= dt.timedelta(hours=rule.cooldown_hours)
        reasons.append(
            f"cooldown: last dream {ago(since)} ago"
            + (
                f" (needs {rule.cooldown_hours} h)"
                if cooled
                else f", needs {rule.cooldown_hours} h"
            )
        )

    # 2. Changes since the last real dream started
    chat_ids: set[str] = set()
    pages: set[str] = set()
    for chat in chats:
        created = parse_ts(chat.get("created"))
        if last_dream is None or (created is not None and created > last_dream):
            chat_ids.add(str(chat.get("id")))
    for commit in commits:
        if commit.get("author") == DREAM_AUTHOR:
            continue
        when = parse_ts(commit.get("date"))
        if last_dream is not None and (when is None or when <= last_dream):
            continue
        summary = str(commit.get("summary") or "")
        if summary.startswith(REVIEW_PREFIX + "wiki/"):
            pages.add(page_of(summary[len(REVIEW_PREFIX) :].strip()))
        for f in commit.get("files") or []:
            page = page_of(str(f.get("path") or ""))
            if page in reviewed:
                pages.add(page)
    changes = len(chat_ids) + len(pages)
    old = last_dream is None or now - last_dream > dt.timedelta(days=rule.max_days)
    enough = changes >= rule.min_changes or (changes >= 1 and old)
    what = f"{len(chat_ids)} new chat(s), {len(pages)} reviewed page(s)"
    needs = f"needs {rule.min_changes}, or 1 after {rule.max_days} days"
    if last_dream is None:
        reasons.append(f"changes: {changes} so far, no dream yet ({what}; needs 1)")
    else:
        reasons.append(f"changes: {changes} since the last dream ({what}; {needs})")

    # 3. Idle: the latest activity that is not the portal's own
    idle_from = now - dt.timedelta(minutes=rule.idle_minutes)
    latest: tuple[dt.datetime, str] | None = None
    for commit in commits:
        when = parse_ts(commit.get("date"))
        if commit.get("author") == DREAM_AUTHOR or when is None or when <= idle_from:
            continue
        if _inside(when, windows, now):
            continue
        if latest is None or when > latest[0]:
            latest = (when, f"a wiki commit by {commit.get('author') or 'someone'}")
    for line in spend:
        when = parse_ts(line.get("ts"))
        if when is None or when <= idle_from or _inside(when, windows, now):
            continue
        if latest is None or when > latest[0]:
            latest = (when, f"a model call ({line.get('model') or 'unknown'})")
    idle = latest is None
    if idle:
        reasons.append(f"idle: no activity in the last {rule.idle_minutes} min")
    else:
        assert latest is not None
        reasons.append(
            f"idle: {latest[1]} {ago(now - latest[0])} ago (needs {rule.idle_minutes} min quiet)"
        )

    # 4. Budget
    if budget_allows:
        reasons.append("budget: allows the cheap passes")
    else:
        reasons.append(
            "budget: refuses even the cheap passes" + (f" ({budget_note})" if budget_note else "")
        )

    return Due(cooled and enough and idle and budget_allows, reasons)


def windows_of(dreams: DreamStore, ideas: IdeaStore, limit: int = 30) -> list[Window]:
    """The portal's own recent runs: every dream (dry runs too) and every ideas run."""
    out: list[Window] = []
    rows = [(d.started, d.finished) for d in dreams.recent(limit)]
    rows += [(r.started, r.finished) for r in ideas.runs(limit)]
    for started, finished in rows:
        start = parse_ts(started)
        if start is not None:
            out.append((start, parse_ts(finished)))
    return out


def dreamt_within(
    dreams: DreamStore, hours: int = 24, clock: Callable[[], dt.datetime] = utcnow
) -> bool:
    """Whether a real dream started in the last `hours` (the nightly ideas run skips then)."""
    last = dreams.last(real=True)
    start = parse_ts(last.started) if last else None
    return start is not None and clock() - start < dt.timedelta(hours=hours)


class DreamTrigger:
    """Gathers what `evaluate` needs and evaluates it (WikiUnavailable when the wiki is down)."""

    def __init__(
        self,
        rule: Rule,
        wiki: WikiClient,
        dreams: DreamStore,
        ideas: IdeaStore,
        usage: UsageLog,
        budget: Budget,
        clock: Callable[[], dt.datetime] = utcnow,
    ) -> None:
        self.rule = rule
        self.wiki = wiki
        self.dreams = dreams
        self.ideas = ideas
        self.usage = usage
        self.budget = budget
        self.clock = clock

    async def check(self) -> Due:
        now = self.clock()
        last = self.dreams.last(real=True)
        last_start = parse_ts(last.started) if last else None
        idle_from = now - dt.timedelta(minutes=self.rule.idle_minutes)
        since = None if last_start is None else format_ts(min(last_start, idle_from))
        commits = await self.wiki.recent(MAX_COMMITS, since=since)
        chats = list((await self.wiki.unprocessed(scope="new", limit=1000)).get("sources", []))
        reviewed = {
            str(d["id"])
            for d in await self.wiki.docs()
            if str(d.get("id", "")).startswith("wiki/")
            and d.get("type") not in MANAGED
            and d.get("status") in (None, "stable")
            and "from-chat-import" not in (d.get("tags") or [])
        }
        months = {format_ts(now)[:7], format_ts(idle_from)[:7]}
        records = self.usage.records()
        spend = [line for month in months for line in records.get(month, [])]
        choice = self.budget.choose(self.budget.tier("cheap"), CHEAP_CHARS, CHEAP_TOKENS)
        return evaluate(
            now,
            last_start,
            commits,
            chats,
            reviewed,
            spend,
            windows_of(self.dreams, self.ideas),
            choice.model is not None,
            self.rule,
            "; ".join(choice.notes),
        )


class Triggerable(Protocol):
    @property
    def running(self) -> bool: ...

    def trigger(self, trigger: str, *, dry_run: bool = False) -> bool: ...


class Scheduler:
    """In auto mode: every `every_minutes`, check whether a dream is due and start one."""

    def __init__(
        self,
        check: Callable[[], Awaitable[Due]],
        job: Triggerable,
        *,
        mode: str,
        every_minutes: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.check = check
        self.job = job
        self.mode = mode
        self.every_minutes = every_minutes
        self.sleep = sleep
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.mode != "auto":
            log.info("dream scheduler: DREAM_MODE=%s, dreams run only by hand", self.mode)
            return
        log.info("dream scheduler: checking every %d min", self.every_minutes)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def tick(self) -> bool:
        """One check; True when it started a dream."""
        if self.mode != "auto":
            return False
        if self.job.running:
            log.info("dream check: a dream or ideas run is going; not checked")
            return False
        try:
            due = await self.check()
        except WikiUnavailable as exc:
            log.info("dream check: not due, the wiki is unreachable (%s)", exc)
            return False
        log.info("dream check: %s; %s", "due" if due.due else "not due", "; ".join(due.reasons))
        if not due.due:
            return False
        started = self.job.trigger("auto")
        log.info("dream check: %s", "dream started" if started else "a run is going; not started")
        return started

    async def _loop(self) -> None:
        while True:
            await self.sleep(self.every_minutes * 60)
            try:
                await self.tick()
            except Exception:
                log.exception("dream check failed")
