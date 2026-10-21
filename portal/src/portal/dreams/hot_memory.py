"""D5, hot memory: the short briefing every conversation starts with.

The material, the prompt, the checks and the page are wiki-mcp's (wiki_mcp.dream.hot_memory
has the whole pass described), shared with the dream tools for assistants; this is the
portal's pass: ask the cheap model for the briefing and, when there are private pages, for
its private variant; log what the checks dropped, and commit both at once.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from wiki_mcp.dream import prompts
from wiki_mcp.dream.hot_memory import NAME, Made, made, material, parse, write
from wiki_mcp.dream.pages import Page, reviewed_pages

if TYPE_CHECKING:
    from portal.dreams.job import Context

MAX_TOKENS = 2000


async def _briefing(ctx: Context, user: str, known: dict[str, Page]) -> Made | None:
    """The page body and its line count, or None when the budget allows no call."""
    briefing = await ctx.ask(
        NAME,
        "cheap",
        prompts.system(prompts.HOT_MEMORY),
        user,
        MAX_TOKENS,
        lambda answer: parse(answer, known),
    )
    if briefing is None:
        return None
    out = made(
        briefing, known, ctx.dream.id, dt.datetime.now(dt.UTC), ctx.config.hot_memory_max_chars
    )
    for why in briefing.dropped:
        ctx.log.flag(NAME, f"dropped: {why}")
    return out


async def hot_memory_pass(ctx: Context) -> None:
    """D5: ask, check and write the briefing, and its private variant (one commit)."""
    reviewed = await reviewed_pages(ctx.wiki)  # read once for both variants
    user, known = await material(ctx.wiki, reviewed=reviewed)
    full_user, full_known = await material(ctx.wiki, private=True, reviewed=reviewed)
    if not full_known:
        ctx.log.skip(NAME, "nothing to summarise: no owner profile and no reviewed pages")
        return
    public = private = None
    if known:
        public = await _briefing(ctx, user, known)
        if public is None:
            return
    same = full_user == user
    if not same:
        private = await _briefing(ctx, full_user, full_known)
    commit = await write(ctx.wiki, ctx.dream.id, public, private, same)
    await ctx.commit(NAME, commit.reason, commit.ops, commit.lines)
