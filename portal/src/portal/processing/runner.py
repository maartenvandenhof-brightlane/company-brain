"""Batch processing of imported chats: the pipeline and its progress.

  select     the transcripts no page cites yet ("new"), also those filed before the
             playbook ("older"), or every imported one ("all"), from GET /unprocessed
  condense   each transcript without the bulk of the assistant text (condense.py)
  extract    one JSON call per transcript chunk, several transcripts at a time (extract.py)
  file       sequentially: resolve entities against the wiki, build the pages and file the
             conversation in one commit through POST /file (filing.py)
  observe    with the job's `observe` option: one more call per transcript without an
             observation page (observe.py), made with its extraction; written through POST
             /observations after the conversation is filed, in a commit of its own

Extraction runs ahead in parallel while filing stays one conversation at a time, so pages
created from one conversation are known when the next one is resolved. A conversation
that fails stays unprocessed and is simply offered again next time. A transcript filed
before is filed again in replace mode: what it contributed to pages still waiting for
review is replaced, and every page written from it is flagged for review. An observation
that fails never fails the filing: the chat is simply left for the Patterns page's backfill.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from wiki_mcp.wiki.store import markdown_section

from portal.processing import condense
from portal.processing.catalogue import Catalogue, Page
from portal.processing.extract import Extraction, extract, system_prompt, user_prompt
from portal.processing.filing import Conversation, merge, plan
from portal.processing.llm import LLM, LLMError
from portal.processing.observe import Observed, observe
from portal.processing.observe import payload as observation
from portal.wikiclient import WikiClient, WikiUnavailable

log = logging.getLogger(__name__)

PLAYBOOK = "Filing a conversation"
PERSONAL = "Personal life and private pages"  # optional: wikis from before it lack it until restart
SKIPPED_PAGE = "wiki/references/skipped-conversations"
_NOT_REVIEWED = {"source", "guide", "proposal", "maintenance-report", "observation", "inference"}
MAX_PENDING = 10_000  # transcripts listed as not observed yet, per run


SCOPES = ("new", "older", "all")


@dataclass
class ProcessJob:
    model: str
    scope: str = "new"  # new | older (filed before the playbook too) | all
    total: int = 0  # conversations selected
    done: int = 0  # filed (pages written)
    skipped: int = 0  # nothing worth filing
    failed: int = 0
    created: int = 0  # pages
    updated: int = 0
    dropped: int = 0  # assistant-only claims left out
    private: int = 0  # pages written as private
    observe: bool = False  # also observe each chat for personal patterns (one call more each)
    observed: int = 0  # observation pages written
    observe_failed: int = 0  # left for the backfill
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    started: float = field(default_factory=time.monotonic)
    seconds: int = 0
    error: str = ""
    cancelled: bool = False
    finished: bool = False

    @property
    def handled(self) -> int:
        return self.done + self.skipped + self.failed


@dataclass
class _Ready:
    conv: Conversation
    extraction: Extraction | None
    flag: list[str]  # pages already written from it: flagged for review again
    again: bool = False  # filed before: replace what it contributed
    observed: Observed | None = None  # to write once it is filed
    observe_failed: bool = False
    chunks: list[str] = field(default_factory=list)  # the condensed transcript


class Processor:
    def __init__(self, wiki: WikiClient, llm: LLM, *, concurrency: int = 4) -> None:
        self.wiki = wiki
        self.llm = llm
        self.concurrency = max(1, concurrency)

    async def run(self, job: ProcessJob, limit: int = 1000) -> None:
        try:
            await self._run(job, limit)
        except (WikiUnavailable, ValueError) as exc:
            log.warning("processing: %s", exc)
            job.error = "The wiki service stopped answering. Start processing again to go on."
        except asyncio.CancelledError:
            job.cancelled = True
            raise
        except Exception:
            log.exception("processing failed")
            job.error = "Processing failed. Start it again to go on with what is left."
        finally:
            self._count(job)
            job.finished = True

    def _count(self, job: ProcessJob) -> None:
        job.calls = self.llm.usage.calls
        job.input_tokens = self.llm.usage.input_tokens
        job.output_tokens = self.llm.usage.output_tokens
        job.seconds = round(time.monotonic() - job.started)

    async def _run(self, job: ProcessJob, limit: int) -> None:
        todo = (await self.wiki.unprocessed(scope=job.scope, limit=limit))["sources"]
        job.total = len(todo)
        if not todo:
            return
        index = await self.wiki.index()
        cat = Catalogue(index.get("docs", []), index.get("types", []))
        guide = await self.wiki.doc("AGENTS")
        playbook = markdown_section(str(guide.get("body", "")), PLAYBOOK)
        if playbook is None:
            raise ValueError("the wiki's AGENTS.md has no 'Filing a conversation' section")
        personal = markdown_section(str(guide.get("body", "")), PERSONAL)
        types = sorted(cat.folders)
        system = system_prompt(playbook, types, personal)
        unobserved: set[str] = set()
        if job.observe:
            pending = await self.wiki.observations_pending(limit=MAX_PENDING)
            unobserved = {str(s["id"]) for s in pending.get("sources", [])}
        queue: asyncio.Queue[_Ready | None] = asyncio.Queue(maxsize=self.concurrency * 2)
        gate = asyncio.Semaphore(self.concurrency)

        async def prepare(entry: dict[str, Any]) -> None:
            async with gate:
                ready = await self._prepare(entry, cat, system, set(types), job.model)
                if ready.extraction is not None and entry["parts"][0] in unobserved:
                    await self._observe(ready, job.model)
            await queue.put(ready)

        async def produce() -> None:
            await asyncio.gather(*(prepare(e) for e in todo))
            await queue.put(None)

        producer = asyncio.create_task(produce())
        try:
            while (ready := await queue.get()) is not None:
                if job.cancelled:
                    break
                await self._file(ready, cat, job)
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    async def _prepare(
        self, entry: dict[str, Any], cat: Catalogue, system: str, types: set[str], model: str
    ) -> _Ready:
        transcript = condense.Transcript()
        flag: list[str] = []
        for part in entry["parts"]:
            doc = await self.wiki.doc(part)
            condense.parse(str(doc.get("body", "")), transcript)
            if entry.get("filed_at"):
                flag += [
                    b["id"]
                    for b in doc.get("backlinks", [])
                    if b.get("type") not in _NOT_REVIEWED and b["id"] != SKIPPED_PAGE
                ]
        conv = Conversation(
            list(entry["parts"]), transcript.title or entry["title"], transcript.started
        )
        chunks = condense.condense(transcript)
        mine = "\n".join(m.text for m in transcript.messages if m.role == "User")
        candidates = cat.candidates(f"{conv.title}\n{mine}")
        results: list[Extraction] = []
        try:
            for i, chunk in enumerate(chunks, 1):
                part = f" (part {i} of {len(chunks)} of the transcript)" if len(chunks) > 1 else ""
                user = user_prompt(conv.title, conv.started, candidates, chunk, part)
                results.append(
                    await extract(self.llm, model, system, user, conv.started or "", types)
                )
        except LLMError as exc:
            log.warning("extraction of %s failed: %s", conv.sources[0], exc)
            return _Ready(conv, None, flag)
        again = bool(entry.get("filed_at"))
        ready = _Ready(conv, merge(results) if results else None, list(dict.fromkeys(flag)), again)
        ready.chunks = chunks
        return ready

    async def _observe(self, ready: _Ready, model: str) -> None:
        """The chat's observations, for writing once it is filed; a failure is only counted."""
        try:
            ready.observed = await observe(self.llm, model, ready.chunks, ready.conv.started)
        except LLMError as exc:
            log.warning("observing %s failed: %s", ready.conv.sources[0], exc)
            ready.observe_failed = True

    async def _write_observations(self, ready: _Ready, job: ProcessJob) -> None:
        """After the chat is filed: its observation page, in a commit of its own. Refused or
        unreachable, it is left for the backfill; the filing stands."""
        if ready.observe_failed:
            job.observe_failed += 1
        if ready.observed is None:
            return
        try:
            await self.wiki.observe(observation(ready.conv.sources[0], ready.observed))
        except (ValueError, WikiUnavailable) as exc:
            log.warning("observations of %s not written: %s", ready.conv.sources[0], exc)
            job.observe_failed += 1
            return
        job.observed += 1

    async def _file(self, ready: _Ready, cat: Catalogue, job: ProcessJob) -> None:
        self._count(job)
        if ready.extraction is None:
            job.failed += 1
            return
        reason = f"Filed an imported conversation by batch processing ({job.model})"
        planned = await plan(ready.conv, ready.extraction, cat, self.llm, job.model, reason)
        payload = planned.payload
        payload["replace"] = ready.again
        if ready.flag:
            # Filed before: the pages written from it then go back to review, even when
            # nothing new is filed now.
            payload["skip"] = None
            listed = {p["path"] for p in payload["pages"]}
            payload["pages"] += [
                {"path": pid, "sections": []} for pid in ready.flag if pid not in listed
            ]
        try:
            result = await self.wiki.file(payload)
        except ValueError as exc:  # refused by wiki-mcp: stays unprocessed
            log.warning("filing %s refused: %s", ready.conv.sources[0], exc)
            job.failed += 1
            return
        job.dropped += planned.dropped
        await self._write_observations(ready, job)
        if payload.get("skip") is not None:
            job.skipped += 1
            return
        job.done += 1
        job.created += len(result.get("created", []))
        job.updated += len(result.get("updated", []))
        job.private += sum(1 for d in planned.drafts if d.private)
        for d in planned.drafts:
            if d.new and d.page.id in result.get("created", []):
                page = d.page
                cat.add(Page(page.id, page.type, page.title, page.description, d.private))
            elif d.private and d.page.id in cat.pages:
                cat.pages[d.page.id].private = True  # marked private now, never un-marked
