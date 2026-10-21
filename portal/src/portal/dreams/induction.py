"""D3, induction: patterns that recur across stated lines. Proposals only.

The material, the prompt, the checks and the proposals are wiki-mcp's (wiki_mcp.dream.induction
has the whole pass described), shared with the dream tools for assistants; this is the
portal's pass: ask the strong model (the cheap one when the budget says so) once per window,
at most MAX_WINDOWS per dream, stopping when the budget refuses one; log what the checks
dropped and flagged, and propose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from wiki_mcp.dream import prompts
from wiki_mcp.dream.induction import (
    MAX_PATTERNS,
    MAX_WINDOWS,
    NAME,
    Proposal,
    inferences_block,
    parse,
    prepare,
    text_of,
    windows,
)
from wiki_mcp.dream.limits import MAX_CHARS
from wiki_mcp.dream.pages import reviewed_pages

if TYPE_CHECKING:
    from portal.dreams.job import Context

MAX_TOKENS = 4000


async def induction_pass(ctx: Context) -> None:
    """D3: ask, check and propose, window by window."""
    reviewed = await reviewed_pages(ctx.wiki)  # newest first
    if not reviewed:
        ctx.log.skip(NAME, "no reviewed pages")
        return
    found, too_long = windows(reviewed)
    for page in too_long:
        ctx.log.flag(NAME, f"{page.id}: too long to read (over {MAX_CHARS} characters)")
    if len(found) > MAX_WINDOWS:
        ctx.log.flag(NAME, f"read the first {MAX_WINDOWS} of {len(found)} windows of pages")
    state = await prepare(ctx.wiki, reviewed, ctx.dream.id)
    block = inferences_block(state.existing, state.by_id)
    proposed = pages = 0
    for window in found[:MAX_WINDOWS]:
        answer = await ctx.ask(
            NAME,
            "strong",
            prompts.system(prompts.INDUCTION),
            text_of(window, block),
            MAX_TOKENS,
            parse,
        )
        if answer is None:
            break
        pages += len(window.pages)
        if answer.notes:
            ctx.log.flag(NAME, f"model notes: {answer.notes}")
        if len(answer.patterns) > MAX_PATTERNS:
            ctx.log.flag(
                NAME,
                f"dropped: {len(answer.patterns) - MAX_PATTERNS} patterns over the limit "
                f"of {MAX_PATTERNS}",
            )
        for item in answer.patterns[:MAX_PATTERNS]:
            checked = state.checked(item, window)
            if checked.proposal is None:
                ctx.log.flag(NAME, f"dropped: {checked.dropped}")
            for flag in checked.flags:
                ctx.log.flag(NAME, flag)
            if checked.proposal is not None and await _propose(ctx, checked.proposal):
                proposed += 1
    if not proposed and pages:
        ctx.log.skip(NAME, f"nothing to propose from {pages} page(s)")


async def _propose(ctx: Context, p: Proposal) -> bool:
    """One proposal; wiki-mcp checks it again, and a refusal is logged, not raised."""
    try:
        await ctx.propose(NAME, p.summary, p.reason, p.ops, speculative=p.speculative)
    except ValueError as exc:
        ctx.log.flag(NAME, f"dropped: refused by the wiki: {p.summary}: {str(exc)[:200]}")
        return False
    return True
