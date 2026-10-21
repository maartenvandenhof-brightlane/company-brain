"""Running idea generation: after each dream, nightly, after batch processing, or on request.

Nightly and after processing, a run reads what was filed since the last one (scope
"recent"); the Ideate button reads the whole wiki instead (scope "wiki"). The nightly run is
skipped when a dream ran in the last 24 hours (`skip_nightly`): a dream ends with an ideas
run of its own.

  since     the start of the last "recent" run that succeeded (the first looks FIRST_DAYS back)
  wake      snoozed ideas whose date has passed are new again
  collect   the conversations filed since then and the pages they touched, or the most
            recently updated pages of the wiki; and the owner's goals, open loops, the
            hot-memory briefing (else his profile) and what the last dream changed (collect.py)
  generate  one JSON call on the strong tier with the shared prompt, for up to
            `candidates` candidates; structure checked (generate.py)
  gate      candidates citing pages the wiki does not know, or only inferences, are dropped
  score     one JSON call on the cheap tier scores each; then the thresholds, the
            near-duplicates of the last 180 days, and the top `shown` by total (score.py)
  store     the kept ideas, skipping ones like any of the last 90 days (store.py)

Every call passes the budget guard (dreams/budget.py) and is recorded under run kind
"ideas" (passes generate and score); a refused call ends the run with "skipped: budget". Ideas
never go into the wiki: a run only reads it.

After every TASTE_EVERY reactions of the owner, `reacted` rewrites the taste note in the
background (taste.py; run kind "taste"), unless a dream or ideas run holds the lock or the
budget refuses: the next reaction tries again.

One run at a time, and never during a dream (the two share a lock; a dream ends with an
ideas run of its own, run_held): a trigger while a run is going does nothing, since the
running one will be followed by the next nightly run anyway. A run that fails is recorded with its error and
the next one reads from the same point again. `progress` tells the Ideas tab where the
current (or last) run is.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from pathlib import Path

from wiki_mcp.audit import AuditLog

from portal.assistant import PromptMissing, load_prompt
from portal.dreams.budget import Budget, ModelAmbiguous
from portal.dreams.store import DreamStore
from portal.ideas import generate as gen
from portal.ideas import score, taste
from portal.ideas.collect import Material, collect, collect_wiki, owner_context
from portal.ideas.progress import SCOPES, Progress
from portal.ideas.store import TASTE_EVERY, Idea, IdeaStore, Run, ago
from portal.processing.llm import LLM, LLMError
from portal.wikiclient import WikiClient, WikiUnavailable

log = logging.getLogger(__name__)

FIRST_DAYS = 7
SHOWN_IN_PROMPT = 30  # open and snoozed ideas listed as "already seen"
USEFUL_IN_PROMPT = 10
REJECTED_IN_PROMPT = 20


def next_run(at: dt.time, now: dt.datetime) -> dt.datetime:
    """The next time of day `at` after `now` (both local, naive)."""
    today = dt.datetime.combine(now.date(), at)
    return today if today > now else today + dt.timedelta(days=1)


def parse_time(value: str) -> dt.time:
    """HH:MM (24 h), as in PORTAL_IDEAS_AT."""
    hours, _, minutes = value.strip().partition(":")
    return dt.time(int(hours), int(minutes or 0))


def _budget(notes: list[str]) -> LLMError:
    return LLMError("skipped: budget (" + "; ".join(notes) + ")")


class IdeasJob:
    def __init__(
        self,
        store: IdeaStore,
        wiki: WikiClient,
        llm: Callable[[], LLM],
        config_dir: Path,
        budget: Budget,
        audit: AuditLog | None = None,
        lock: asyncio.Lock | None = None,
        skip_nightly: Callable[[], bool] | None = None,
        *,
        dreams: DreamStore | None = None,
        candidates: int = 12,
        shown: int = 3,
    ) -> None:
        self.store = store
        self.wiki = wiki
        self.llm = llm
        self.budget = budget
        self.config_dir = config_dir
        self.audit = audit
        self._lock = lock or asyncio.Lock()  # shared with the DreamJob
        # True when tonight's run is not needed (a dream ran, with its own ideas run).
        self.skip_nightly = skip_nightly
        self.dreams = dreams  # the last dream's log is read, never written
        self.candidates = candidates  # IDEAS_CANDIDATES
        self.shown = shown  # IDEAS_MAX_SHOWN
        self._task: asyncio.Task[Run | None] | None = None
        self._schedule: asyncio.Task[None] | None = None
        self.taste_task: asyncio.Task[str | None] | None = None
        self.progress: Progress | None = None  # the current or the last run

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def trigger(self, reason: str, scope: str = "recent") -> bool:
        """Start a run in the background; False when one is already going."""
        if self.running or (self._task is not None and not self._task.done()):
            return False
        # Shown right away, before the task gets to run.
        self.progress = Progress(scope, "")
        self._task = asyncio.create_task(self.run(reason, scope, self.progress))
        return True

    async def run(
        self, reason: str = "manual", scope: str = "recent", progress: Progress | None = None
    ) -> Run | None:
        if scope not in SCOPES:
            raise ValueError(f"unknown scope {scope!r}")
        if self._lock.locked():
            return None
        async with self._lock:
            return await self.run_held(reason, scope, progress)

    async def run_held(
        self, reason: str = "manual", scope: str = "recent", progress: Progress | None = None
    ) -> Run:
        """A run by a caller that holds the lock already (a dream, at its end)."""
        if scope not in SCOPES:
            raise ValueError(f"unknown scope {scope!r}")
        self.store.wake()
        since: str | None = None
        if scope == "recent":
            last = self.store.last_success()
            since = last.started if last else ago(FIRST_DAYS)
        run = self.store.start_run(since, "", scope)
        progress = self.progress = progress or Progress(scope, run.model)
        llm = self.llm()
        dropped: list[str] = []
        try:
            material: Material = (
                await collect(self.wiki, since, progress)
                if since is not None
                else await collect_wiki(self.wiki, progress)
            )
            run.pages = len(material)
            if material:
                kept, why = await self._ideas(llm, run, progress, material, scope)
                dropped += why
                progress.to(5)
                run.kept = progress.kept = len(self.store.add(kept, run.id))
        except (LLMError, WikiUnavailable, PromptMissing, ModelAmbiguous, ValueError) as exc:
            log.warning("ideas: %s", exc)
            run.error = str(exc) or type(exc).__name__
        except Exception as exc:
            log.exception("ideas run failed")
            run.error = f"failed ({type(exc).__name__})"
        finally:
            await llm.close()
            self.store.finish_run(run)
            progress.end(run.error)
        run.dropped = dropped
        if dropped:
            log.info("ideas: left out %d: %s", len(dropped), "; ".join(dropped)[:1000])
        if self.audit is not None:
            self.audit.event(
                "ideas_run",
                client="portal",
                outcome="failed" if run.error else "ok",
                reason=reason,
                scope=scope,
                model=run.model,
                pages=run.pages,
                proposed=run.proposed,
                kept=run.kept,
            )
        return run

    async def _ideas(
        self, llm: LLM, run: Run, progress: Progress, material: Material, scope: str
    ) -> tuple[list[Idea], list[str]]:
        """Generate, gate, score and select: the ideas to store, and why others were not."""
        dropped: list[str] = []
        ctx = await owner_context(self.wiki, self.dreams)
        note = self.store.taste()
        system = gen.system_prompt(load_prompt(self.config_dir), self.candidates)
        user = gen.user_prompt(
            material,
            ctx,
            note,
            self.store.useful(USEFUL_IN_PROMPT),
            self.store.feedback(REJECTED_IN_PROMPT),
            self.store.seen(SHOWN_IN_PROMPT),
            scope,
        )
        choice = self.budget.choose(
            self.budget.tier("strong"), len(system) + len(user), gen.MAX_TOKENS
        )
        dropped += choice.notes
        if choice.model is None:
            raise _budget(choice.notes)
        run.model = progress.model = choice.model
        progress.to(2)
        llm.meter = self.budget.meter("ideas", run.id, "generate")
        ideas, why = await gen.generate(llm, run.model, system, user, ctx, self.candidates)
        dropped += why
        run.proposed = progress.proposed = len(ideas) + len(why)
        progress.to(3)
        ideas, why = gen.gate(ideas, await self.wiki.known_ids(max_age=0))
        dropped += why
        if not ideas:
            return [], dropped
        progress.to(4)
        past = self.store.past(score.PAST_DAYS)
        user = score.user_prompt(ctx, note, ideas, past)
        choice = self.budget.choose(
            self.budget.tier("cheap"), len(score.PROMPT) + len(user), score.MAX_TOKENS
        )
        dropped += choice.notes
        if choice.model is None:
            raise _budget(choice.notes)
        llm.meter = self.budget.meter("ideas", run.id, "score")
        scored, why = await gen.ask_json(
            llm,
            choice.model,
            score.PROMPT,
            user,
            score.MAX_TOKENS,
            lambda answer: score.parse(answer, ideas, past),
        )
        dropped += why
        kept, why = score.select(scored, past, self.shown)
        return kept, dropped + why

    # --- the taste note ------------------------------------------------------------

    def reacted(self) -> bool:
        """Count one reaction of the owner; at TASTE_EVERY and beyond, start rewriting the
        taste note in the background. True when a rewrite was started."""
        if self.store.react() < TASTE_EVERY:
            return False
        if self.running or (self.taste_task is not None and not self.taste_task.done()):
            return False  # the next reaction tries again
        self.taste_task = asyncio.create_task(self.rewrite_taste())
        return True

    async def rewrite_taste(self) -> str | None:
        """The note rewritten by the cheap tier, or None when it was skipped or failed (the
        old note stays, and the count goes on)."""
        if self.running:
            return None
        current = self.store.taste()
        user = taste.user_prompt(current, self.store.reactions(taste.REACTIONS_SHOWN))
        try:
            choice = self.budget.choose(
                self.budget.tier("cheap"), len(taste.PROMPT) + len(user), taste.MAX_TOKENS
            )
        except ModelAmbiguous as exc:
            log.warning("taste note: %s", exc)
            return None
        if choice.model is None:
            log.info("taste note: skipped: %s", "; ".join(choice.notes))
            return None
        llm = self.llm()
        llm.meter = self.budget.meter("taste", None, "taste")
        try:
            note = await gen.ask_json(
                llm, choice.model, taste.PROMPT, user, taste.MAX_TOKENS, taste.parse
            )
        except (LLMError, ValueError) as exc:
            log.warning("taste note: %s", exc)
            return None
        except Exception:
            log.exception("taste note failed")
            return None
        finally:
            await llm.close()
        self.store.set_taste(note)
        return note

    # --- the nightly schedule ----------------------------------------------------

    def start_schedule(self, at: dt.time) -> None:
        self._schedule = asyncio.create_task(self._nightly(at))

    async def stop(self) -> None:
        for task in (self._schedule, self._task, self.taste_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _nightly(self, at: dt.time) -> None:
        while True:
            now = dt.datetime.now()
            await asyncio.sleep((next_run(at, now) - now).total_seconds())
            await self.nightly()

    async def nightly(self) -> Run | None:
        """The nightly run, unless `skip_nightly` says a dream made ideas already."""
        if self.skip_nightly is not None and self.skip_nightly():
            log.info("ideas: nightly run skipped, a dream ran in the last 24 hours")
            return None
        return await self.run("nightly")
