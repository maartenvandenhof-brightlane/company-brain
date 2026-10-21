"""D1, ingestion: up to DREAM_MAX_CHATS imported chats no page cites yet, filed as drafts.

  select    the "new" scope of batch processing (GET /unprocessed), at most max_chats
  model     the cheap tier, as far as the budget guard allows one extraction call
  file      the processing pipeline unchanged (processing/runner.py): extraction, then one
            commit per conversation through POST /file, every page it writes a draft

Every model call goes through the budget guard and is recorded under the dream (pass
"ingestion"); a refused call fails that conversation, which stays unprocessed and is offered
again next time, like any conversation that fails.

The commits are the processing pipeline's own, not the dream's: they carry no `Dream:`
trailer, so reverting the dream does not undo them. What they write are drafts, which no
dream touches until the owner reviews them.

Skipped (and logged) in a dry run (it says what it would file), while batch processing runs
on the Import page, with DREAM_MAX_CHATS=0, and when nothing waits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from portal.processing.runner import ProcessJob, Processor

if TYPE_CHECKING:
    from portal.dreams.job import Context

NAME = "ingestion"
# The budget guard's estimate for one extraction call: a condensed transcript chunk, the
# playbook and the candidate pages.
EXTRACT_CHARS = 60_000


async def ingestion_pass(ctx: Context) -> None:
    """D1: file the waiting chats with the processing pipeline."""
    most = ctx.config.max_chats
    if most <= 0:
        ctx.log.skip(NAME, "DREAM_MAX_CHATS is 0")
        return
    if ctx.busy():
        ctx.log.skip(NAME, "batch processing is running on the Import page")
        return
    waiting = int((await ctx.wiki.unprocessed(scope="new", limit=most)).get("total") or 0)
    if waiting == 0:
        ctx.log.skip(NAME, "no unprocessed imported chats")
        return
    take = min(waiting, most)
    if ctx.dream.dry_run:
        ctx.log.ingestion.append(
            f"(not written) would file {take} of {waiting} unprocessed imported chats as drafts"
        )
        return
    choice = ctx.budget.choose(ctx.budget.tier("cheap"), EXTRACT_CHARS, None)
    for note in choice.notes:
        ctx.log.flag(NAME, f"model not used: {note}")
    if choice.model is None:
        ctx.log.skip(NAME, "skipped: budget")
        return
    ctx.log.models[NAME] = choice.model
    llm = ctx.llm()
    llm.meter = ctx.budget.meter("dream", ctx.dream.id, NAME)
    job = ProcessJob(model=choice.model, scope="new")
    try:
        await Processor(ctx.wiki, llm, concurrency=ctx.config.process_concurrency).run(
            job, limit=most
        )
    finally:
        await llm.close()
    ctx.log.ingestion.append(
        f"{job.done} of {job.total} chats filed, {job.skipped} skipped (nothing worth filing), "
        f"{job.failed} failed (they stay unprocessed)"
    )
    ctx.log.ingestion.append(
        f"{job.created} pages created and {job.updated} updated, as drafts (by batch "
        "processing's own commits, one per conversation: reverting the dream keeps them)"
    )
    if job.error:
        ctx.log.skip(NAME, f"error: {job.error}")
