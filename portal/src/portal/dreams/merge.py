"""D4, duplicate pages: merges, proposed only (part of the hygiene pass, after its fixes).

The candidates, the prompt, the checks and the operations are wiki-mcp's
(wiki_mcp.dream.merge has the whole step described), shared with the dream tools for
assistants; this is the portal's step: ask the cheap model about the candidate pairs, log
what the checks dropped, and propose.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from wiki_mcp.dream import prompts
from wiki_mcp.dream.deduction import Invalid
from wiki_mcp.dream.merge import MOST, NAME, Duplicates, LeftOut, candidates, material, parse
from wiki_mcp.dream.pages import Page

if TYPE_CHECKING:
    from portal.dreams.job import Context

MAX_TOKENS = 2000


async def merges(
    ctx: Context, pages: list[Page], known: set[str], private: frozenset[str] = frozenset()
) -> None:
    """Find candidate pairs among the reviewed pages, ask, check and propose merges."""
    pairs = candidates(pages)
    if not pairs:
        return
    answer = await ctx.ask(
        NAME, "cheap", prompts.system(prompts.DUPLICATES), material(pairs), MAX_TOKENS, parse
    )
    if answer is None:
        return
    if answer.notes:
        ctx.log.flag(NAME, f"duplicates: model notes: {answer.notes}")
    state = Duplicates(pairs, known, private, ctx.dream.id, dt.datetime.now(dt.UTC))
    for item in answer.pairs[:MOST]:
        try:
            merge = await state.check(item, ctx.wiki)
        except LeftOut as exc:
            ctx.log.flag(NAME, f"merge left out: {exc}")
            continue
        except Invalid as exc:
            ctx.log.flag(NAME, f"dropped: duplicates: {exc}")
            continue
        if merge is None:  # not the same: counted
            continue
        try:
            await ctx.propose(NAME, merge.summary, merge.why(ctx.dream.id), merge.ops)
        except ValueError as exc:
            ctx.log.flag(NAME, f"dropped: refused by the wiki: {merge.summary}: {str(exc)[:200]}")
            continue
        state.proposed(merge)
        ctx.log.flag(NAME, f"merge proposed: {merge.other.id} into {merge.kept.id}: {merge.reason}")
    if state.not_same:
        ctx.log.flag(NAME, f"{state.not_same} candidate duplicate pair(s) judged not the same")
