"""Dreaming: the background consolidation of the wiki, one dream at a time.

A dream runs its passes in this order, then an ideas run (scope "recent"):

  D1 ingestion   up to DREAM_MAX_CHATS unprocessed imported chats, filed as drafts
  D4 hygiene     small fixes on reviewed pages (wiki_mcp.dream.hygiene), before deduction
                 reads them; duplicate pages as merge proposals (merge.py)
  D2 deduction   supersedes, conflicts, implications: proposals only
  D3 induction   patterns across stated lines: inference proposals only
  D5 hot memory  the briefing every conversation starts with (direct)
  D6 log         the dream log page (wiki_mcp.dream.log) and the dreams row; always runs

A pass that fails is logged and the dream goes on with the next. Each pass that writes makes
exactly one wiki commit, with a `Dream: <id>` trailer (except D1, whose commits are batch
processing's own: one per conversation); wiki-mcp checks every change itself
(reviewed pages only, never raw/, no bullet line removed or reworded). Proposals wait for the
owner. What each pass reads, its prompt and the checks its answer must pass are wiki-mcp's
(wiki_mcp.dream), shared with the dream tools for assistants; the passes here ask the model,
within the budget, and write. A dry run runs every pass but writes nothing: what it would have written goes into
the dreams row (`dry_run_result`), and no ideas run follows.

A patterns run (reassess(), the Patterns page's "Reassess patterns") is a dream of its own with
only the patterns pass (patterns.py: personal patterns from observations, one direct commit)
and the log, and no ideas run after it; the scheduled dream never runs that pass.

Dreams and ideas runs share one lock, so only one of them runs at a time. `progress` tells
the Dreams page where the current (or last) dream is. While an assistant dream's session may
be open (dreams/assistant.py: the owner dreams in Claude.ai or ChatGPT), no dream starts here:
the button says so, and the scheduler tries again at its next check.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from wiki_mcp.audit import AuditLog
from wiki_mcp.dream import Unparsable, hygiene
from wiki_mcp.dream import log as dream_log
from wiki_mcp.dream.log import DreamLog
from wiki_mcp.dream.pages import reviewed_pages
from wiki_mcp.dream.sessions import SESSION_TTL_S

from portal.dreams import merge
from portal.dreams.budget import Budget
from portal.dreams.deduction import deduction_pass
from portal.dreams.hot_memory import hot_memory_pass
from portal.dreams.induction import induction_pass
from portal.dreams.ingestion import ingestion_pass
from portal.dreams.patterns import patterns_pass
from portal.dreams.store import PATTERNS, Dream, DreamStore
from portal.ideas.job import IdeasJob
from portal.ideas.store import now
from portal.processing.llm import LLM, LLMError
from portal.wikiclient import WikiClient, WikiUnavailable

log = logging.getLogger(__name__)

PASSES = ("ingestion", "hygiene", "deduction", "induction", "hot-memory", "log")
T = TypeVar("T")
# How long after its row started an assistant dream may still hold its session: the session's
# time, and a margin for the opening (the portal reads the session to know it is over sooner).
ASSISTANT_HOLD = dt.timedelta(seconds=SESSION_TTL_S + 300)


@dataclass(frozen=True)
class DreamConfig:
    max_chats: int = 10  # DREAM_MAX_CHATS
    hot_memory_max_chars: int = 6000  # HOT_MEMORY_MAX_CHARS
    process_concurrency: int = 4  # PORTAL_PROCESS_CONCURRENCY: D1's calls at a time


STEPS = ("Ingest chats", "Hygiene", "Deduction", "Induction", "Hot memory", "Log", "Ideas")
PATTERN_PASSES = ("patterns", "log")  # a patterns run
PATTERN_STEPS = ("Patterns", "Log")


@dataclass
class DreamProgress:
    dream_id: int
    dry_run: bool
    step: int = 1  # 1-based index into steps
    finished: bool = False
    error: str = ""
    started: float = field(default_factory=time.monotonic)
    ended: float | None = None
    steps: tuple[str, ...] = STEPS

    def to(self, step: int) -> None:
        self.step = step

    def end(self, error: str = "") -> None:
        self.error, self.finished, self.ended = error, True, time.monotonic()

    @property
    def seconds(self) -> int:
        return round((self.ended or time.monotonic()) - self.started)

    @property
    def text(self) -> str:
        what = "Dry run" if self.dry_run else "Dream"
        if self.finished:
            return (
                f"{what} {self.dream_id} stopped: {self.error}"
                if self.error
                else (f"{what} {self.dream_id} finished.")
            )
        return f"{what} {self.dream_id}: {self.steps[self.step - 1]}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "dream": self.dream_id,
            "dry_run": self.dry_run,
            "step": self.step,
            "steps": list(self.steps),
            "finished": self.finished,
            "error": self.error,
            "text": self.text,
            "seconds": self.seconds,
        }


class Context:
    """What a pass works with, and where its writes go: to wiki-mcp, or for a dry run into
    the dream's record only."""

    def __init__(
        self,
        dream: Dream,
        wiki: WikiClient,
        budget: Budget,
        dlog: DreamLog,
        llm: Callable[[], LLM],
        config: DreamConfig,
        *,
        since_base: str | None = None,
        busy: Callable[[], bool] = lambda: False,
    ) -> None:
        self.dream = dream
        self.wiki = wiki
        self.budget = budget
        self.log = dlog
        self.llm = llm
        self.config = config
        # The base commit of the last dream that could write (None: this is the first one):
        # what changed since then is what deduction reads.
        self.since_base = since_base
        self.busy = busy  # batch processing is running on the Import page
        # A dry run's would-be writes: {"commits": [...], "proposals": [...], "log": text}
        self.would: dict[str, Any] = {"commits": [], "proposals": [], "log": ""}

    async def ask(
        self,
        pass_: str,
        tier: str,
        system: str,
        user: str,
        max_tokens: int,
        parse: Callable[[str], T],
    ) -> T | None:
        """One JSON call of a pass with the model the budget guard allows for its tier (strong
        falls back to cheap), and one repair call when `parse` raises LLMError (a shared
        parse's Unparsable is one). None, and the pass logged as skipped, when the budget
        allows no model."""
        choice = self.budget.choose(self.budget.tier(tier), len(system) + len(user), max_tokens)
        for note in choice.notes:
            self.log.flag(pass_, f"model not used: {note}")
        if choice.model is None:
            self.log.skip(pass_, "skipped: budget")
            return None
        self.log.models[pass_] = choice.model
        llm = self.llm()
        llm.meter = self.budget.meter("dream", self.dream.id, pass_)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        def checked(answer: str) -> T:
            try:
                return parse(answer)
            except Unparsable as exc:
                raise LLMError(str(exc)) from exc

        try:
            answer = await llm.json_chat(choice.model, messages, max_tokens=max_tokens)
            try:
                return checked(answer)
            except LLMError as exc:
                messages += [
                    {"role": "assistant", "content": answer[:20000]},
                    {
                        "role": "user",
                        "content": f"That answer is invalid ({exc}). "
                        "Reply with only the corrected JSON object.",
                    },
                ]
                return checked(await llm.json_chat(choice.model, messages, max_tokens=max_tokens))
        finally:
            await llm.close()

    async def commit(
        self, pass_: str, reason: str, operations: list[dict[str, Any]], lines: list[str]
    ) -> str | None:
        """One commit for a pass's direct changes; its sha (None for a dry run or no change)."""
        if not operations:
            return None
        if self.dream.dry_run:
            self.would["commits"].append(
                {"pass": pass_, "reason": reason, "operations": operations}
            )
            self.log.commits.append(dream_log.Commit("", pass_, lines))
            return None
        result = await self.wiki.dream_commit(self.dream.id, pass_, reason, operations)
        sha = result.get("commit")
        if sha:
            self.dream.commits.append({"sha": sha, "pass": pass_, "pages": result.get("pages")})
            self.log.commits.append(dream_log.Commit(str(sha), pass_, lines))
        return str(sha) if sha else None

    async def propose(
        self,
        pass_: str,
        summary: str,
        reason: str,
        operations: list[dict[str, Any]],
        *,
        speculative: bool = False,
    ) -> str | None:
        """A proposal for the owner; its id (None for a dry run)."""
        mark = " (speculative)" if speculative else ""
        if self.dream.dry_run:
            self.would["proposals"].append(
                {"pass": pass_, "summary": summary, "reason": reason,
                 "operations": operations, "speculative": speculative}
            )  # fmt: skip
            self.log.proposals.append(f"(not written) {pass_}: {summary}{mark}")
            return None
        result = await self.wiki.dream_proposal(
            self.dream.id, pass_, summary, reason, operations, speculative=speculative
        )
        pid = str(result.get("proposal_id") or "")
        self.dream.proposals.append(
            {"id": pid, "pass": pass_, "summary": summary, "speculative": speculative}
        )
        self.log.proposed(pid, pass_, summary, speculative)
        return pid


Pass = Callable[[Context], Awaitable[None]]


async def hygiene_pass(ctx: Context) -> None:
    """D4: the fixes of wiki_mcp.dream.hygiene on reviewed pages, as one commit; then
    duplicate pages, as merge proposals (merge.py)."""
    pages = await reviewed_pages(ctx.wiki)
    survey = await hygiene.survey(ctx.wiki, pages)
    planned = survey.planned
    for flag in planned.flags:
        ctx.log.flag("hygiene", flag)
    if not planned.fixes:
        ctx.log.skip("hygiene", f"nothing to fix on {len(pages)} reviewed pages")
    else:
        await ctx.commit(
            "hygiene",
            survey.reason(ctx.dream.id),
            [f.op for f in planned.fixes],
            [f.line for f in planned.fixes],
        )
    await merge.merges(ctx, pages, survey.known, survey.private)


class DreamJob:
    def __init__(
        self,
        store: DreamStore,
        wiki: WikiClient,
        budget: Budget,
        ideas: IdeasJob,
        lock: asyncio.Lock,
        llm: Callable[[], LLM],
        config: DreamConfig | None = None,
        audit: AuditLog | None = None,
        passes: dict[str, Pass] | None = None,
        busy: Callable[[], bool] | None = None,
    ) -> None:
        self.store = store
        self.wiki = wiki
        self.budget = budget
        self.ideas = ideas
        self.lock = lock  # shared with the IdeasJob
        self.llm = llm
        self.config = config or DreamConfig()
        self.audit = audit
        self.busy = busy or (lambda: False)
        # The passes (tests may give fewer); one not given is logged as not available.
        self.passes: dict[str, Pass] = (
            passes
            if passes is not None
            else {
                "ingestion": ingestion_pass,
                "hygiene": hygiene_pass,
                "deduction": deduction_pass,
                "induction": induction_pass,
                "hot-memory": hot_memory_pass,
                "patterns": patterns_pass,  # patterns runs only (reassess)
            }
        )
        self._task: asyncio.Task[Dream | None] | None = None
        self.progress: DreamProgress | None = None

    @property
    def running(self) -> bool:
        return self.lock.locked() or (self._task is not None and not self._task.done())

    @property
    def assistant(self) -> Dream | None:
        """The assistant dream whose session may still be open: no dream starts here then."""
        since = (dt.datetime.now(dt.UTC) - ASSISTANT_HOLD).strftime("%Y-%m-%dT%H:%M:%SZ")
        held = self.store.running_assistant(since)
        return held[0] if held else None

    def trigger(self, trigger: str, *, dry_run: bool = False) -> bool:
        """Start a dream in the background; False when a dream or ideas run is going, or an
        assistant dream's session may be open."""
        if self.running or self.assistant is not None:
            return False
        self._task = asyncio.create_task(self.run(trigger, dry_run=dry_run))
        return True

    def reassess(self) -> bool:
        """Start a patterns run in the background (the patterns pass and the log); False as
        for trigger."""
        return self.trigger(PATTERNS)

    async def begin(
        self, trigger: str, *, dry_run: bool = False, client: str | None = None
    ) -> tuple[Dream, str | None]:
        """A new dreams row from the wiki's current commit, and the base commit of the last
        dream that could write (what deduction reads changes since; None: there was none).
        WikiUnavailable when the wiki is down. The caller holds the lock."""
        recent = await self.wiki.recent(1)
        base = str(recent[0]["commit"]) if recent else ""
        last = self.store.last()  # before this one starts: the last dream that could write
        dream = self.store.start(
            "dry run" if dry_run else trigger, dry_run=dry_run, base_sha=base, client=client
        )
        return dream, last.base_sha if last is not None and last.base_sha else None

    async def run(self, trigger: str, *, dry_run: bool = False) -> Dream | None:
        if self.lock.locked():
            return None
        async with self.lock:
            try:
                dream, since_base = await self.begin(trigger, dry_run=dry_run)
            except WikiUnavailable as exc:
                log.warning("dream: %s", exc)
                return None
            patterns = trigger == PATTERNS
            passes = PATTERN_PASSES if patterns else PASSES
            progress = self.progress = DreamProgress(
                dream.id, dry_run, steps=PATTERN_STEPS if patterns else STEPS
            )
            dlog = DreamLog()
            ctx = Context(
                dream,
                self.wiki,
                self.budget,
                dlog,
                self.llm,
                self.config,
                since_base=since_base,
                busy=self.busy,
            )
            try:
                for n, name in enumerate(passes[:-1], 1):
                    progress.to(n)
                    await self._pass(ctx, name)
                progress.to(len(passes))
                await self._log(ctx)
            except Exception as exc:  # the log pass itself failed: record it, never raise
                log.exception("dream %s failed", dream.id)
                dream.error = f"failed ({type(exc).__name__}): {exc}"[:500]
            dream.cost_eur = sum(c.cost_eur for c in self.budget.calls("dream", dream.id))
            if dry_run:
                dream.dry_run_result = ctx.would
            self.store.finish(dream)
            if not dry_run and not dream.error and not patterns:
                progress.to(len(STEPS))
                await self.ideas.run_held("after dream", "recent")
            progress.end(dream.error)
            if self.audit is not None:
                self.audit.event(
                    "dream_run",
                    client="portal",
                    outcome="failed" if dream.error else "ok",
                    reason=dream.trigger,
                    dream=dream.id,
                    commits=len(dream.commits),
                    proposals=len(dream.proposals),
                )
            return dream

    async def _pass(self, ctx: Context, name: str) -> None:
        run = self.passes.get(name)
        if run is None:
            ctx.log.skip(name, "not available yet")
            return
        try:
            await run(ctx)
        except (ValueError, WikiUnavailable, LLMError) as exc:
            log.warning("dream %s, %s: %s", ctx.dream.id, name, exc)
            ctx.log.skip(name, f"error: {exc}")
        except Exception as exc:
            log.exception("dream %s, %s failed", ctx.dream.id, name)
            ctx.log.skip(name, f"error: {type(exc).__name__}")

    async def _log(self, ctx: Context) -> None:
        """D6: render the log; write it as the dream's last commit (kept in the row for a
        dry run)."""
        dream = ctx.dream
        number = self.store.number_on(dream.started[:10], dream.id)
        cost = sum(c.cost_eur for c in self.budget.calls("dream", dream.id))
        body = dream_log.render(dream, ctx.log, number, now(), cost)
        page = dream_log.page_id(dream, number)
        if dream.dry_run:
            ctx.would["log"] = body
            return
        # private while the wiki has any private document (wiki_mcp.dream.log)
        op = await dream_log.operation(self.wiki, dream, number, body)
        await ctx.commit("log", f"Dream {dream.id} log", [op], [f"{page}: the dream log"])
        dream.log_page = page
