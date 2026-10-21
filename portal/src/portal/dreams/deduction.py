"""D2, deduction: what follows from the stated lines of the pages that changed. Proposals only.

The material, the prompt, the checks and the proposals are wiki-mcp's (wiki_mcp.dream.deduction
has the whole pass described), shared with the dream tools for assistants; this is the
portal's pass: ask the strong model (the cheap one when the budget says so), log what the
checks dropped, and propose.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from wiki_mcp.dream import prompts
from wiki_mcp.dream.deduction import NAME, material, parse, review

if TYPE_CHECKING:
    from portal.dreams.job import Context

MAX_TOKENS = 4000


async def deduction_pass(ctx: Context) -> None:
    """D2: ask, check and propose."""
    found = await material(ctx.wiki, ctx.since_base)
    if not found.pages:
        ctx.log.skip(NAME, "nothing changed on reviewed pages since the last dream")
        return
    answer = await ctx.ask(
        NAME, "strong", prompts.system(prompts.DEDUCTION), found.text, MAX_TOKENS, parse
    )
    if answer is None:
        return
    if answer.notes:
        ctx.log.flag(NAME, f"model notes: {answer.notes}")
    plan = await review(ctx.wiki, answer, found, ctx.dream.id, dt.datetime.now(dt.UTC))
    for why in plan.dropped:
        ctx.log.flag(NAME, f"dropped: {why}")
    for proposal in plan.proposals(ctx.dream.id):
        await _propose(ctx, proposal.summary, proposal.reason, proposal.ops)
    if not plan.pages and not plan.inferences:
        n = len(found.pages)
        ctx.log.skip(NAME, f"nothing to propose from {n} page{'' if n == 1 else 's'}")


async def _propose(ctx: Context, summary: str, reason: str, ops: list[dict[str, Any]]) -> None:
    """One proposal; wiki-mcp checks it again, and a refusal is logged, not raised."""
    try:
        await ctx.propose(NAME, summary, reason, ops)
    except ValueError as exc:
        ctx.log.flag(NAME, f"dropped: refused by the wiki: {summary}: {str(exc)[:200]}")
