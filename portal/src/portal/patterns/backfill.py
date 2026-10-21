"""The observations backfill: observe the imported chats that have no observation page yet
(or, on request, one from an older extractor), for personal patterns.

  select     GET /observations/pending, oldest first, at most the job's limit
  observe    per chat: its parts condensed (processing/condense.py) and observed
             (processing/observe.py) with the budget's cheap model, every call metered and
             guarded by the dreams budget (run kind "observe")
  write      POST /observations: one commit per chat, idempotent (a chat observed meanwhile by
             the same extractor answers `exists`); raw transcripts and fact pages are never
             touched

Resumable by construction: nothing is kept between runs, the wiki's observation pages are the
progress. Cancelling stops after the chat being observed; an interrupted chat simply stays
pending. When the budget refuses a call the run stops, and what was written stays.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from portal.dreams.budget import Budget
from portal.processing import condense
from portal.processing.llm import LLM, BudgetRefused, LLMError
from portal.processing.observe import EXTRACTOR, observe, payload
from portal.wikiclient import WikiClient, WikiUnavailable

log = logging.getLogger(__name__)

RUN_KIND = "observe"  # the budget's record of these calls
LIMITS = (25, 100, 500, 2000)


@dataclass
class BackfillJob:
    model: str
    limit: int = 100
    outdated: bool = False  # also chats observed by an older extractor
    total: int = 0  # chats selected
    done: int = 0  # observation pages written
    empty: int = 0  # of which with nothing personal
    existing: int = 0  # observed meanwhile
    failed: int = 0  # left pending
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_eur: float = 0.0
    started: float = field(default_factory=time.monotonic)
    seconds: int = 0
    error: str = ""
    cancelled: bool = False
    finished: bool = False

    @property
    def handled(self) -> int:
        return self.done + self.existing + self.failed


class Backfill:
    """One backfill at a time; `job` is the current or last one."""

    def __init__(self, wiki: WikiClient, llm: Callable[[], LLM], budget: Budget) -> None:
        self.wiki = wiki
        self.llm = llm
        self.budget = budget
        self.job: BackfillJob | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self.job is not None and not self.job.finished

    def start(self, limit: int, outdated: bool) -> str | None:
        """Start a backfill in the background; why not, or None when it started."""
        if self.running:
            return "A backfill is running already."
        model = self.budget.cheap_model()
        if model is None:
            return "No cheap model (MODEL_CHEAP) is served by LiteLLM: add a provider key first."
        self.job = BackfillJob(model, limit=limit, outdated=outdated)
        self._task = asyncio.create_task(self.run(self.job))
        return None

    def cancel(self) -> bool:
        if not self.running or self.job is None:
            return False
        self.job.cancelled = True
        return True

    async def run(self, job: BackfillJob) -> None:
        llm = self.llm()
        meter = self.budget.meter(RUN_KIND, None, "backfill")
        llm.meter = meter
        try:
            pending = await self.wiki.observations_pending(
                limit=job.limit, outdated=EXTRACTOR if job.outdated else None
            )
            todo = list(pending.get("sources", []))
            job.total = len(todo)
            for entry in todo:
                if job.cancelled:
                    break
                await self._one(entry, job, llm)
                self._count(job, llm, meter.cost_eur)
        except BudgetRefused as exc:
            job.error = f"Stopped by the budget ({exc}). What was written stays; start again later."
        except (WikiUnavailable, ValueError) as exc:
            log.warning("backfill: %s", exc)
            job.error = "The wiki service stopped answering. Start again to go on."
        except asyncio.CancelledError:
            job.cancelled = True
            raise
        except Exception:
            log.exception("backfill failed")
            job.error = "The backfill failed. Start it again to go on with what is left."
        finally:
            self._count(job, llm, meter.cost_eur)
            job.finished = True
            await llm.close()

    @staticmethod
    def _count(job: BackfillJob, llm: LLM, cost: float) -> None:
        job.calls = llm.usage.calls
        job.input_tokens = llm.usage.input_tokens
        job.output_tokens = llm.usage.output_tokens
        job.cost_eur = cost
        job.seconds = round(time.monotonic() - job.started)

    async def _one(self, entry: dict[str, Any], job: BackfillJob, llm: LLM) -> None:
        """Observe one chat and write its page. A model answer that stays unusable or a page
        wiki-mcp refuses leaves the chat pending (counted as failed); the budget's refusal
        and an unreachable wiki stop the run."""
        transcript = condense.Transcript()
        for part in entry.get("parts") or []:
            doc = await self.wiki.doc(str(part))
            condense.parse(str(doc.get("body", "")), transcript)
        chunks = condense.condense(transcript)
        try:
            observed = await observe(llm, job.model, chunks, transcript.started)
        except BudgetRefused:
            raise
        except LLMError as exc:
            log.warning("backfill: observing a chat failed: %s", exc)
            job.failed += 1
            return
        try:
            result = await self.wiki.observe(payload(str(entry["id"]), observed))
        except ValueError as exc:
            log.warning("backfill: observations refused: %s", exc)
            job.failed += 1
            return
        if result.get("status") == "exists":
            job.existing += 1
            return
        job.done += 1
        if not result.get("observations"):
            job.empty += 1
